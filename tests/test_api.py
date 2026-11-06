import asyncio
import json
import time

import httpx
import pytest

from solar_monitor import db
from solar_monitor.api import create_app
from solar_monitor.collector import Collector, read_battery_limits


@pytest.fixture
def collector(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    yield Collector("test", 0, conn)
    conn.close()


@pytest.fixture
def call(collector):
    """Make one request, on this thread because the database connection belongs to it."""
    app = create_app(collector)

    def request(method, path, **kwargs):
        async def send():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.request(method, path, **kwargs)
        return asyncio.run(send())

    return request


def live(collector, **values):
    collector.latest["readings"] = [{"id": k, "name": "", "unit": "", "value": v} for k, v in values.items()]


def discharging(collector, power_w=1000, soc=50):
    live(collector, pbattery1=power_w, battery_soc=soc)
    now = int(time.time())
    collector.buffer.extend(db.Sample(now - age, {"pbattery1": power_w}) for age in (45, 30, 15, 0))


def lamp(**fields):
    return {"name": "Lamp", "typical_w": 100, **fields}


def test_estimate_is_empty_before_any_readings(call):
    assert call("GET", "/api/latest").json()["battery_estimate"] is None


def test_estimate_reports_direction_and_limit_without_a_capacity(call, collector):
    discharging(collector)
    collector.latest["battery_limits"] = {"min_soc": 30, "reserve_soc": 25}
    estimate = call("GET", "/api/latest").json()["battery_estimate"]
    assert estimate["state"] == "discharging"
    assert estimate["limit_soc"] == 30
    assert estimate["hours"] is None
    assert estimate["capacity"] == {"kwh": None, "source": None, "points": 0}


def test_entered_capacity_gives_a_time_and_allows_for_battery_health(call, collector):
    discharging(collector)
    live(collector, pbattery1=1000, battery_soc=50, battery_soh=50)
    call("PUT", "/api/settings", json={"capacity_kwh": 20})
    estimate = call("GET", "/api/latest").json()["battery_estimate"]
    assert estimate["capacity"]["source"] == "entered"
    assert estimate["capacity"]["kwh"] == 10
    assert estimate["hours"] == pytest.approx(3)  # 30 points of 10 kWh at 1 kW.


def test_estimate_falls_back_to_a_typical_minimum_if_the_inverter_reports_none(call, collector):
    discharging(collector)
    collector.latest["battery_limits"] = {"min_soc": None, "reserve_soc": None}
    assert call("GET", "/api/latest").json()["battery_estimate"]["limit_soc"] == 20


def test_estimate_charges_toward_the_chosen_target(call, collector):
    discharging(collector, power_w=-1000)
    assert call("GET", "/api/latest").json()["battery_estimate"]["limit_soc"] == 100
    call("PUT", "/api/settings", json={"target_soc": 90})
    assert call("GET", "/api/latest").json()["battery_estimate"]["limit_soc"] == 90


def test_estimate_is_withdrawn_when_the_inverter_stops_answering(call, collector):
    discharging(collector)
    collector.latest["error"] = "TimeoutError: "
    assert call("GET", "/api/latest").json()["battery_estimate"] is None


def test_measured_capacity_takes_over_from_an_entered_one(collector):
    # 1000 W for 6 minutes per percentage point: 10 kWh, whatever was typed in.
    now = int(time.time())
    db.save_samples(collector.conn, [
        db.Sample(now - 7000 + i * 60, {"pbattery1": 1000, "battery_soc": 80 - i // 6}) for i in range(100)
    ])
    db.save_settings(collector.conn, {"capacity_kwh": 99})
    live(collector, pbattery1=1000, battery_soc=50)

    async def overview():
        transport = httpx.ASGITransport(app=create_app(collector))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return (await client.get("/api/appliances")).json()

    capacity = asyncio.run(overview())["capacity"]
    assert capacity["source"] == "measured"
    assert capacity["kwh"] == pytest.approx(10, abs=0.1)


def test_add_edit_and_delete_an_appliance(call):
    response = call("POST", "/api/appliances", json=lamp(notes="Hall"))
    assert response.status_code == 201
    row = response.json()["appliances"][0]
    assert (row["name"], row["typical_w"], row["notes"]) == ("Lamp", 100, "Hall")

    edited = call("PUT", f"/api/appliances/{row['id']}", json=lamp(name="Heater", typical_w=500, use_in_outage=True))
    assert edited.json()["appliances"][0]["name"] == "Heater"
    assert edited.json()["cases"]["selected_typical"]["watts"] == 500

    assert call("DELETE", f"/api/appliances/{row['id']}").json()["appliances"] == []


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
def test_changing_an_appliance_that_does_not_exist_is_a_404(call, method):
    assert call(method, "/api/appliances/404", **({"json": lamp()} if method == "PUT" else {})).status_code == 404


@pytest.mark.parametrize("body", [
    {"name": "", "typical_w": 1},
    {"name": "   ", "typical_w": 1},
    {"name": "x" * 101, "typical_w": 1},
    {"name": "Lamp"},
    {"name": "Lamp", "typical_w": -1},
    {"name": "Lamp", "typical_w": 1, "peak_w": -1},
    {"name": "Lamp", "typical_w": 1_000_001},
])
def test_invalid_appliances_are_refused(call, body):
    assert call("POST", "/api/appliances", json=body).status_code == 422
    assert call("GET", "/api/appliances").json()["appliances"] == []


@pytest.mark.parametrize("body", [{"limit_amps": 0}, {"target_soc": 101}, {"capacity_kwh": -1}, {"surge_limit_w": 0}])
def test_invalid_settings_are_refused(call, body):
    assert call("PUT", "/api/settings", json=body).status_code == 422


def test_a_cross_site_form_post_cannot_add_an_appliance(call):
    # A form or a "simple" fetch from another site can't set a JSON content type.
    response = call("POST", "/api/appliances", content=json.dumps(lamp()), headers={"content-type": "text/plain"})
    assert response.status_code == 422
    assert call("GET", "/api/appliances").json()["appliances"] == []


def test_overview_uses_live_voltage_and_load(call, collector):
    live(collector, backup_v1=240, vgrid=235, load_ptotal=480, battery_soc=60)
    call("PUT", "/api/settings", json={"limit_amps": 10})
    overview = call("GET", "/api/appliances").json()
    assert overview["volts"] == 240
    assert overview["cases"]["now"]["amps"] == 2
    assert overview["cases"]["now"]["headroom_amps"] == 8
    assert overview["settings"]["limit_amps"] == 10


def test_overview_falls_back_on_grid_voltage_then_a_nominal_one(call, collector):
    live(collector, backup_v1=0, vgrid=235)
    assert call("GET", "/api/appliances").json()["volts"] == 235
    live(collector)
    call("POST", "/api/appliances", json={"name": "Lamp", "typical_w": 230})
    overview = call("GET", "/api/appliances").json()
    assert overview["volts"] is None
    assert overview["appliances"][0]["amps"] == 1


def test_overview_does_not_send_the_serial_number(call, collector):
    live(collector, serial_number="SECRET")
    assert "SECRET" not in call("GET", "/api/appliances").text


class FakeInverter:
    def __init__(self, **settings):
        self.settings = settings

    async def read_setting(self, name):
        if name not in self.settings:
            raise ValueError(name)
        return self.settings[name]


def test_battery_limits_come_from_the_inverter_settings():
    inverter = FakeInverter(battery_discharge_depth=20, battery_discharge_depth_offline=15)
    assert asyncio.run(read_battery_limits(inverter)) == {"min_soc": 20, "reserve_soc": 15}


def test_an_unreadable_limit_is_none_rather_than_a_failed_poll():
    assert asyncio.run(read_battery_limits(FakeInverter())) == {"min_soc": None, "reserve_soc": None}
