"""Changing a TOU programme from the Battery schedule tab: the web route, who may use it, and how it fits around polling."""

import asyncio

import httpx
import pytest
from goodwe.protocol import ProtocolResponse
from goodwe.sensor import EcoModeV2

from solar_monitor import collector as collector_module
from solar_monitor import config, control, db
from solar_monitor.api import create_app
from solar_monitor.auth import COOKIE, LoginRequired
from solar_monitor.collector import Collector

TOU_40 = "17010500f97ffe70005a0fff"
TOU_30 = "17010500f97ffed4005a0fff"


class TouInverter:
    """Holds period 1's bytes. The rest of the schedule can't be read, which the page copes with."""

    def __init__(self, held=TOU_40):
        self.held = bytes.fromhex(held)
        self.writes = []

    async def read_setting(self, name):
        if name == "eco_mode_1":
            return EcoModeV2(name, 0, "test").read_value(ProtocolResponse(self.held, None))
        raise ValueError(name)

    async def write_setting(self, name, value):
        self.writes.append((name, value))
        self.held = value


class PolledInverter(TouInverter):
    """Notices if polling and a write ever overlap."""

    model_name = "test"
    rated_power = 5000
    firmware = "1"
    arm_firmware = "2"
    modbus_version = 3

    def __init__(self):
        super().__init__()
        self.reads = 0
        self.busy = False
        self.overlapped = False

    async def _talk(self):
        self.overlapped = self.overlapped or self.busy
        self.busy = True
        await asyncio.sleep(0)
        self.busy = False

    async def read_runtime_data(self):
        await self._talk()
        self.reads += 1
        return {"ppv": 700, "e_total": 5.0}

    def sensors(self):
        return []

    async def read_setting(self, name):
        await self._talk()
        return await super().read_setting(name)

    async def write_setting(self, name, value):
        await self._talk()
        await super().write_setting(name, value)


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(control, "READ_BACK_WAIT_SECONDS", 0)


@pytest.fixture
def collector(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    app_collector = Collector("test", 0, conn)
    app_collector.inverter = TouInverter()
    yield app_collector
    conn.close()


@pytest.fixture
def tou(collector, monkeypatch):
    """Sends a change as this machine, with no login, as the dashboard normally runs on the PC."""
    monkeypatch.setattr(config, "load_dotenv", lambda *_: None)
    for name in ("BIND_HOST", "DASHBOARD_USER", "DASHBOARD_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    app = create_app(collector)

    def request(percent=30, expected=TOU_40, slot=1, base_url="http://127.0.0.1:8000"):
        async def send():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
                return await client.put(f"/api/tou/{slot}/power", json={"percent": percent, "expected": expected})
        return asyncio.run(send())

    return request


def get(collector, path):
    async def send():
        transport = httpx.ASGITransport(app=create_app(collector))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)
    return asyncio.run(send())


def test_a_change_is_reported_logged_and_shown_straight_away(tou, collector):
    response = tou()
    assert response.status_code == 200
    assert response.json() == {"outcome": "confirmed", "old_percent": 40, "new_percent": 30}
    assert collector.inverter.writes == [("eco_mode_1", bytes.fromhex(TOU_30))]
    period = collector.latest["schedule"]["periods"][0]
    assert (period["raw"], period["power_pct"]) == (TOU_30, 30)
    write = get(collector, "/api/tou/writes").json()["writes"][0]
    assert (write["slot"], write["old_percent"], write["new_percent"], write["outcome"]) == (1, 40, 30, "confirmed")


def test_a_stale_page_is_refused_with_a_reason(tou, collector):
    response = tou(expected=TOU_30)
    assert response.status_code == 409
    assert "changed on the inverter" in response.json()["detail"]
    assert collector.inverter.writes == []


def test_a_write_that_did_not_arrive_is_an_error_with_a_reason(tou, collector):
    async def ignored(name, value):
        collector.inverter.writes.append((name, value))

    collector.inverter.write_setting = ignored
    response = tou()
    assert response.status_code == 502
    assert "still holds the old value" in response.json()["detail"]
    assert len(collector.inverter.writes) == 1


def test_a_change_with_no_connection_is_refused(tou, collector):
    collector.inverter = None
    response = tou()
    assert response.status_code == 409
    assert "isn't connected" in response.json()["detail"]


@pytest.mark.parametrize("slot", [0, 5])
def test_there_is_no_such_programme(tou, collector, slot):
    assert tou(slot=slot).status_code == 404
    assert collector.inverter.writes == []


@pytest.mark.parametrize(
    ("percent", "expected"),
    [(0, TOU_40), (101, TOU_40), (30.5, TOU_40), ("30", TOU_40), (True, TOU_40), (30, "nope"), (30, TOU_40.upper()), (30, TOU_40 + "00")],
)
def test_a_badly_formed_change_never_reaches_the_inverter(tou, collector, percent, expected):
    assert tou(percent=percent, expected=expected).status_code == 422
    assert collector.inverter.writes == []


def test_a_page_reached_by_another_name_cannot_change_the_inverter(tou, collector):
    # A site that points its own domain at this machine would count as the same site in the browser.
    assert tou(base_url="http://elsewhere.example:8000").status_code == 403
    assert collector.inverter.writes == []


@pytest.mark.parametrize("base_url", ["http://localhost:8000", "http://[::1]:8000"])
def test_any_name_for_this_machine_can_change_it(tou, base_url):
    assert tou(base_url=base_url).status_code == 200


def test_the_home_network_cannot_change_it_without_a_login(tou, collector, monkeypatch):
    monkeypatch.setenv("BIND_HOST", "0.0.0.0")
    assert tou().status_code == 403
    assert collector.inverter.writes == []


def test_a_login_lets_the_home_network_change_it(tou, collector, monkeypatch):
    monkeypatch.setenv("BIND_HOST", "0.0.0.0")
    monkeypatch.setenv("DASHBOARD_USER", "user")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "password")
    assert tou(base_url="http://solar.lan:8000").status_code == 200
    assert len(collector.inverter.writes) == 1


def test_on_the_network_only_a_logged_in_request_reaches_the_inverter(collector, monkeypatch):
    # Built as main.py builds it. The login is the only thing guarding this route when BIND_HOST opens the app up.
    monkeypatch.setattr(config, "load_dotenv", lambda *_: None)
    monkeypatch.setenv("BIND_HOST", "0.0.0.0")
    monkeypatch.setenv("DASHBOARD_USER", "user")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "password")
    app = create_app(collector)
    app.add_middleware(LoginRequired, username="user", password="password")
    change = {"percent": 30, "expected": TOU_40}

    async def send():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://solar.lan:8000") as client:
            no_session = await client.put("/api/tou/1/power", json=change)
            made_up = await client.put("/api/tou/1/power", json=change, headers={"Cookie": f"{COOKIE}=made-up"})
            sent_before_login = list(collector.inverter.writes)
            logged_in = await client.post("/login", data={"username": "user", "password": "password"})
            sent = await client.put("/api/tou/1/power", json=change)
        return no_session, made_up, sent_before_login, logged_in, sent

    no_session, made_up, sent_before_login, logged_in, sent = asyncio.run(send())
    assert (no_session.status_code, made_up.status_code) == (401, 401)
    assert sent_before_login == []
    assert logged_in.status_code == 303
    assert sent.status_code == 200
    assert collector.inverter.writes == [("eco_mode_1", bytes.fromhex(TOU_30))]


def test_the_battery_schedule_tab_is_where_programmes_change(collector):
    page = get(collector, "/").text
    assert 'href="#tou"' not in page and 'id="view-tou"' not in page
    assert page.count('href="#schedule"') == 1
    tab = page.split('id="view-schedule"', 1)[1].split("</section>", 1)[0]
    for part in ("tou-periods", "tou-change", "tou-writes", "schedule-modes"):
        assert f'id="{part}"' in tab


def test_a_write_never_overlaps_polling_and_polling_carries_on(monkeypatch, collector):
    monkeypatch.setattr(collector_module, "POLL_SECONDS", 0)
    monkeypatch.setattr(collector_module, "FLUSH_SECONDS", 3600)
    inverter = PolledInverter()

    async def connect(*_, **__):
        return inverter

    monkeypatch.setattr(collector_module.goodwe, "connect", connect)
    collector.inverter = None

    async def wait_for(condition):
        for _ in range(10_000):
            if condition():
                return
            await asyncio.sleep(0)
        pytest.fail("condition never became true")

    async def scenario():
        async with create_app(collector).router.lifespan_context(None):
            await wait_for(lambda: inverter.reads >= 1)
            result = await collector.set_period_power(1, 30, TOU_40)
            polled = inverter.reads
            await wait_for(lambda: inverter.reads > polled)
            return result

    assert asyncio.run(scenario())["outcome"] == "confirmed"
    assert inverter.writes == [("eco_mode_1", bytes.fromhex(TOU_30))]
    assert not inverter.overlapped
    assert collector.latest["schedule"]["periods"][0]["power_pct"] == 30


def test_a_refused_change_still_refreshes_what_the_page_shows(collector):
    collector.inverter = TouInverter(TOU_30)
    with pytest.raises(control.Refused):
        asyncio.run(collector.set_period_power(1, 20, TOU_40))
    assert collector.inverter.writes == []
    assert collector.latest["schedule"]["periods"][0]["power_pct"] == 30
