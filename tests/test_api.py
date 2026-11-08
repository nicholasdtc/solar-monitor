import asyncio
import json
import re
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


def charge_period(cutoff, start="00:00", end="00:00", **fields):
    """A TOU charge period in the form the collector keeps. Start and end the same run all day."""
    next_day = end <= start
    return {"slot": 1, "state": "on", "start": start, "end": end, "next_day": next_day, "action": "charge",
            "cutoff_soc": cutoff, "day_bits": 127, "month_bits": 0x0FFF, **fields}


def charge_limit(call):
    estimate = call("GET", "/api/latest").json()["battery_estimate"]
    return estimate["limit_soc"], estimate["limit_from"]


def test_charging_counts_to_full_if_the_inverter_reports_no_limit(call, collector):
    discharging(collector, power_w=-1000)
    assert charge_limit(call) == (100, {"source": "fallback"})


def test_charging_counts_to_the_inverters_own_limit(call, collector):
    discharging(collector, power_w=-1000)
    collector.latest["battery_limits"] = {"min_soc": 20, "reserve_soc": 10, "max_soc": 95}
    assert charge_limit(call) == (95, {"source": "inverter"})


def test_a_running_charge_period_stops_at_its_cut_off(call, collector):
    discharging(collector, power_w=-1000)
    collector.latest["battery_limits"] = {"min_soc": 20, "reserve_soc": 10, "max_soc": 95}
    collector.latest["schedule"] = {"tou_on": True, "periods": [charge_period(90)]}
    assert charge_limit(call) == (90, {"source": "period", "slot": 1, "end": "00:00"})


def test_the_inverters_limit_wins_over_a_higher_cut_off(call, collector):
    discharging(collector, power_w=-1000)
    collector.latest["battery_limits"] = {"min_soc": 20, "reserve_soc": 10, "max_soc": 80}
    collector.latest["schedule"] = {"tou_on": True, "periods": [charge_period(90)]}
    assert charge_limit(call) == (80, {"source": "inverter"})


def test_charge_periods_dont_count_with_the_tou_switch_off(call, collector):
    discharging(collector, power_w=-1000)
    collector.latest["schedule"] = {"tou_on": False, "work_mode": "Self-consumption", "periods": [charge_period(90)]}
    assert charge_limit(call) == (100, {"source": "fallback"})


def test_discharging_says_nothing_about_charge_limits(call, collector):
    discharging(collector)
    collector.latest["schedule"] = {"tou_on": True, "periods": [charge_period(90)]}
    assert call("GET", "/api/latest").json()["battery_estimate"]["limit_from"] is None


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


def log_battery_only(collector, overhead_w=100, loads=(200, 700, 1200, 1700)):
    now = int(time.time())
    db.save_samples(collector.conn, [
        db.Sample(now - 7000 + i * 15, {"ppv": 0, "load_ptotal": load, "pbattery1": load + overhead_w, "active_power_total": 0})
        for i, load in enumerate(load for load in loads for _ in range(10))
    ])


def test_overhead_is_typical_until_the_log_can_measure_it(call):
    overhead = call("GET", "/api/appliances").json()["overhead"]
    assert overhead["source"] == "typical"
    assert overhead["standby_w"] == 40
    assert overhead["efficiency"] == 0.93


def test_measured_overhead_reaches_the_planners_and_the_checks_tab(call, collector):
    log_battery_only(collector)
    live(collector, backup_v1=200, battery_soc=60)
    collector.latest["battery_limits"] = {"min_soc": 30, "reserve_soc": 10}
    call("POST", "/api/appliances", json=lamp(typical_w=900, use_in_outage=True))
    overview = call("PUT", "/api/settings", json={"capacity_kwh": 10, "outage_hours": 5, "run_until_minutes": 90}).json()

    assert overview["overhead"]["source"] == "measured"
    assert overview["overhead"]["standby_w"] == 100
    assert overview["overhead"]["efficiency"] == 1
    assert overview["outage_plan"]["from_now"]["max_w"] == pytest.approx(900)  # 1 kW, less the 100 W.
    assert overview["run_plan"]["max_w"] == pytest.approx(3000 / overview["run_plan"]["hours"] - 100)
    assert overview["cases"]["selected_typical"]["hours_from_full"] == pytest.approx(9)  # 9 kWh at 1 kW.

    bands = call("GET", "/api/latest").json()["overhead"]["bands"]
    assert [band["house_w"] for band in bands] == [200, 700, 1200, 1700]
    assert bands[0]["overhead_w"] == 100
    assert bands[0]["share"] == pytest.approx(100 / 300)


def test_latest_says_what_the_overhead_is_now(call, collector):
    live(collector, ppv=0, load_ptotal=900, pbattery1=1000, active_power_total=0)
    now = call("GET", "/api/latest").json()["overhead"]["now"]
    assert (now["house_w"], now["battery_w"], now["overhead_w"], now["share"]) == (900, 1000, 100, 0.1)


def test_overhead_now_is_empty_unless_the_battery_alone_is_supplying_the_house(call, collector):
    assert call("GET", "/api/latest").json()["overhead"]["now"] is None
    live(collector, ppv=500, load_ptotal=900, pbattery1=1000, active_power_total=0)
    assert call("GET", "/api/latest").json()["overhead"]["now"] is None


def test_overhead_now_is_withdrawn_when_the_inverter_stops_answering(call, collector):
    live(collector, ppv=0, load_ptotal=900, pbattery1=1000, active_power_total=0)
    collector.latest["error"] = "TimeoutError: "
    assert call("GET", "/api/latest").json()["overhead"]["now"] is None


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


@pytest.mark.parametrize("body", [
    {"limit_amps": 0},
    {"capacity_kwh": -1},
    {"surge_limit_w": 0},
    {"outage_hours": 0},
    {"outage_hours": 8761},
    {"run_until_minutes": -1},
    {"run_until_minutes": 1440},
    {"run_until_minutes": 1.5},
    {"tariff_day": -1},
    {"tariff_export": "lots"},
])
def test_invalid_settings_are_refused(call, body):
    assert call("PUT", "/api/settings", json=body).status_code == 422


def test_saving_one_setting_leaves_the_others_and_null_clears_it(call):
    call("PUT", "/api/settings", json={"limit_amps": 10, "tariff_day": 5})
    settings = call("PUT", "/api/settings", json={"tariff_day": None}).json()["settings"]
    assert settings["limit_amps"] == 10
    assert settings["tariff_day"] is None


def test_the_planners_stay_empty_until_asked(call):
    overview = call("GET", "/api/appliances").json()
    assert overview["outage_plan"] is None
    assert overview["run_plan"] is None


def test_outage_plan_uses_the_ticked_load_the_reserve_and_the_limit(call, collector):
    live(collector, backup_v1=200, battery_soc=60)
    collector.latest["battery_limits"] = {"min_soc": 30, "reserve_soc": 10}
    call("POST", "/api/appliances", json=lamp(typical_w=500, use_in_outage=True))
    call("POST", "/api/appliances", json=lamp(name="Unticked", typical_w=9000))
    call("PUT", "/api/settings", json={"capacity_kwh": 10, "outage_hours": 5})
    plan = call("GET", "/api/appliances").json()["outage_plan"]
    assert plan["selected_w"] == 500
    assert plan["from_now"]["max_w"] == pytest.approx(892.8)  # 5 kWh above the 10% reserve.
    assert plan["from_now"]["spare_w"] == pytest.approx(392.8)

    plan = call("PUT", "/api/settings", json={"limit_amps": 3}).json()["outage_plan"]
    assert plan["limit_w"] == 600  # 3 A at the live 200 V.
    assert plan["from_now"]["max_w"] == 600
    assert plan["from_now"]["capped"] is True


def test_run_plan_uses_the_minimum_and_the_smoothed_battery_power(call, collector):
    discharging(collector, power_w=700, soc=60)
    collector.latest["battery_limits"] = {"min_soc": 30, "reserve_soc": 10}
    plan = call("PUT", "/api/settings", json={"capacity_kwh": 10, "run_until_minutes": 90}).json()["run_plan"]
    assert plan["min_soc"] == 30
    assert plan["energy_kwh"] == pytest.approx(3)  # Not the 5 kWh above the reserve.
    assert plan["until"][11:16] == "01:30"
    assert 0 < plan["hours"] <= 24
    assert plan["max_battery_w"] == pytest.approx(3000 / plan["hours"])
    assert plan["battery_w"] == 700


def test_run_plan_has_no_live_power_when_the_inverter_stops_answering(call, collector):
    discharging(collector)
    collector.latest["error"] = "TimeoutError: "
    assert call("PUT", "/api/settings", json={"run_until_minutes": 90}).json()["run_plan"]["battery_w"] is None


def test_overview_works_out_the_tariffs(call):
    tariffs = call("GET", "/api/appliances").json()["tariffs"]
    assert tariffs["saving_peak"] is None
    tariffs = call("PUT", "/api/settings", json={"tariff_peak": 30, "tariff_night": 9}).json()["tariffs"]
    assert tariffs["night_kwh_cost"] == pytest.approx(10)
    assert tariffs["saving_peak"] == pytest.approx(20)
    assert tariffs["saving_day"] is None


def test_the_page_has_an_input_for_every_setting(call):
    page = call("GET", "/").text
    for name in db.SETTINGS:
        assert f'name="{name}"' in page


def test_the_flow_test_covers_every_path(call):
    page = call("GET", "/?flowtest").text
    assert 'id="flow-test"' in page
    routes = set(re.findall(r'"(\w+-\w+)": (?:elbow\(|`M)', page))
    assert routes == {
        "solar-load", "solar-battery", "solar-grid",
        "battery-load", "battery-grid",
        "grid-load", "grid-battery",
    }
    scenarios = re.findall(r"expect: \[(.*?)\]", page)
    assert '"solar-load", "solar-battery", "solar-grid"' in scenarios
    assert '"solar-battery", "solar-grid"' in scenarios
    assert routes == set(re.findall(r'"([\w-]+)"', " ".join(scenarios)))


def test_the_page_gets_every_font_from_this_app(call):
    page = call("GET", "/").text
    fonts = re.findall(r'url\("(/fonts/[^"]+)"\)', page)
    assert fonts
    for path in fonts:
        response = call("GET", path)
        assert response.status_code == 200
        assert response.content.startswith(b"wOF2")


def test_the_page_loads_nothing_from_the_internet(call):
    """It has to keep working on the home network when the internet is down."""
    page = call("GET", "/").text
    assert not re.findall(r'(?:src|href)="(?:https?:)?//|url\(["\']?(?:https?:)?//|@import', page)


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
    inverter = FakeInverter(battery_discharge_depth=20, battery_discharge_depth_offline=15, soc_upper_limit=95)
    assert asyncio.run(read_battery_limits(inverter)) == {"min_soc": 20, "reserve_soc": 15, "max_soc": 95}


def test_an_unreadable_limit_is_none_rather_than_a_failed_poll():
    assert asyncio.run(read_battery_limits(FakeInverter())) == {"min_soc": None, "reserve_soc": None, "max_soc": None}


@pytest.mark.parametrize("value", [0, 101, 65535])
def test_a_charge_limit_that_cant_be_a_percentage_is_none(value):
    assert asyncio.run(read_battery_limits(FakeInverter(soc_upper_limit=value)))["max_soc"] is None


def test_daily_adds_where_the_energy_went_to_days_with_readings(call, collector):
    now = int(time.time())
    collector.buffer.extend(db.Sample(now - age, {"ppv": 3600, "load_ptotal": 3600}) for age in (1, 0))
    days = call("GET", "/api/daily?days=2").json()["days"]
    assert days[-1]["solar_home"] == 0.001
    assert "solar_home" not in days[0]


def test_daily_works_out_a_finished_day_once(call, collector):
    from datetime import date, datetime, timedelta
    noon = int(datetime.combine(date.today() - timedelta(days=1), datetime.min.time()).timestamp()) + 12 * 3600
    db.save_samples(collector.conn, [db.Sample(noon + age, {"ppv": 3600, "load_ptotal": 3600}) for age in (0, 1)])
    assert call("GET", "/api/daily?days=2").json()["days"][0]["solar_home"] == 0.001
    db.save_samples(collector.conn, [db.Sample(noon + 2, {"ppv": 3600, "load_ptotal": 3600})])
    assert call("GET", "/api/daily?days=2").json()["days"][0]["solar_home"] == 0.001
