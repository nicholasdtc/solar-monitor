"""Switching Backup mode and changing the battery's floors and ceiling: the checks, the log, the web route and the page."""

import asyncio

import httpx
import pytest

from solar_monitor import config, control, db
from solar_monitor.api import create_app
from solar_monitor.auth import COOKIE, LoginRequired
from solar_monitor.collector import Collector

# As this inverter holds them: Backup off, both floors 20%, the ceiling 90%.
HELD = {"backup_mode_enable": 0, "battery_discharge_depth": 20, "battery_discharge_depth_offline": 20, "soc_upper_limit": 90}


class SettingsInverter:
    """Holds the four settings. Everything else can't be read, which the page copes with."""

    def __init__(self, **held):
        self.held = {**HELD, **held}
        self.writes = []
        self.unreadable = set()
        self.logged_at_write = None
        self.conn = None

    async def read_setting(self, name):
        if name in self.unreadable or name not in self.held:
            raise ValueError(name)
        return self.held[name]

    async def write_setting(self, name, value):
        if self.conn is not None:
            self.logged_at_write = db.list_setting_writes(self.conn)
        self.writes.append((name, value))
        self.held[name] = value


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(control, "READ_BACK_WAIT_SECONDS", 0)


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    yield connection
    connection.close()


@pytest.fixture
def inverter(conn):
    held = SettingsInverter()
    held.conn = conn
    return held


def change(inverter, conn, name, value, expected):
    return asyncio.run(control.set_setting(inverter, conn, name, value, expected))


def log(conn):
    return [(w["setting"], w["old_value"], w["new_value"], w["outcome"]) for w in db.list_setting_writes(conn)]


# The checks and the log.


@pytest.mark.parametrize(
    "name, register, old, new",
    [
        ("backup", "backup_mode_enable", 0, 1),
        ("min_soc", "battery_discharge_depth", 20, 5),
        ("reserve_soc", "battery_discharge_depth_offline", 20, 10),
        ("max_soc", "soc_upper_limit", 90, 100),
    ],
)
def test_each_setting_goes_to_its_own_register_and_is_logged(inverter, conn, name, register, old, new):
    assert change(inverter, conn, name, new, old) == {"outcome": "confirmed", "old": old, "new": new}
    assert inverter.writes == [(register, new)]
    assert log(conn) == [(name, old, new, "confirmed")]
    assert conn.execute("SELECT held_value FROM setting_writes").fetchone() == (new,)


def test_the_attempt_is_logged_before_it_is_sent(inverter, conn):
    change(inverter, conn, "min_soc", 5, 20)
    assert [(w["old_value"], w["new_value"], w["outcome"]) for w in inverter.logged_at_write] == [(20, 5, "sent")]


def test_backup_can_be_switched_off_again(inverter, conn):
    inverter.held["backup_mode_enable"] = 1
    assert change(inverter, conn, "backup", 0, 1)["outcome"] == "confirmed"
    assert inverter.held["backup_mode_enable"] == 0


def test_the_same_value_sends_nothing(inverter, conn):
    assert change(inverter, conn, "min_soc", 20, 20) == {"outcome": "unchanged", "old": 20, "new": 20}
    assert inverter.writes == [] and log(conn) == []


def test_a_value_changed_since_the_page_showed_it_is_refused(inverter, conn):
    inverter.held["battery_discharge_depth"] = 15
    with pytest.raises(control.Refused, match="has changed on the inverter"):
        change(inverter, conn, "min_soc", 5, 20)
    assert inverter.writes == [] and log(conn) == []


@pytest.mark.parametrize("held", [None, 2, 65535, True, 1.5, "1"])
def test_a_setting_that_cant_be_read_is_never_written(inverter, conn, held):
    inverter.held["backup_mode_enable"] = held
    with pytest.raises(control.Refused, match="Couldn't read backup mode"):
        change(inverter, conn, "backup", 1, 0)
    assert inverter.writes == []


def test_a_setting_that_doesnt_answer_is_never_written(inverter, conn):
    inverter.unreadable.add("soc_upper_limit")
    with pytest.raises(control.Refused, match="Couldn't read the ceiling"):
        change(inverter, conn, "max_soc", 95, 90)
    assert inverter.writes == []


def test_only_these_settings_can_change(inverter, conn):
    with pytest.raises(KeyError):
        change(inverter, conn, "work_mode", 2, 0)
    assert inverter.writes == []


@pytest.mark.parametrize("value", [2, 100])
def test_backup_is_only_on_or_off(inverter, conn, value):
    with pytest.raises(control.Refused, match="only be on or off"):
        change(inverter, conn, "backup", value, 0)
    assert inverter.writes == []


@pytest.mark.parametrize("name", ["min_soc", "reserve_soc"])
def test_a_floor_never_goes_below_5_percent_from_here(inverter, conn, name):
    for value in (0, 4):
        with pytest.raises(control.Refused, match="below 5%"):
            change(inverter, conn, name, value, 20)
    assert inverter.writes == []
    assert change(inverter, conn, name, 5, 20)["outcome"] == "confirmed"


@pytest.mark.parametrize("name", ["min_soc", "reserve_soc"])
def test_a_floor_stays_below_the_ceiling_as_the_inverter_holds_it(inverter, conn, name):
    inverter.held["soc_upper_limit"] = 60
    for value in (60, 61):
        with pytest.raises(control.Refused, match="below the ceiling, 60%"):
            change(inverter, conn, name, value, 20)
    assert inverter.writes == []
    assert change(inverter, conn, name, 59, 20)["outcome"] == "confirmed"


def test_a_floor_isnt_changed_when_the_ceiling_cant_be_read(inverter, conn):
    inverter.held["soc_upper_limit"] = 65535
    with pytest.raises(control.Refused, match="Couldn't read the ceiling"):
        change(inverter, conn, "min_soc", 30, 20)
    assert inverter.writes == []


@pytest.mark.parametrize("floor, label", [("battery_discharge_depth", "on-grid"), ("battery_discharge_depth_offline", "power-cut")])
def test_the_ceiling_stays_above_both_floors_as_the_inverter_holds_them(inverter, conn, floor, label):
    inverter.held[floor] = 40
    for value in (30, 40):
        with pytest.raises(control.Refused, match=f"above the {label} floor, 40%"):
            change(inverter, conn, "max_soc", value, 90)
    assert inverter.writes == []
    assert change(inverter, conn, "max_soc", 41, 90)["outcome"] == "confirmed"


def test_the_ceiling_is_at_most_100_percent(inverter, conn):
    with pytest.raises(control.Refused, match="above 100%"):
        change(inverter, conn, "max_soc", 101, 90)
    assert inverter.writes == []


@pytest.mark.parametrize("floor", ["battery_discharge_depth", "battery_discharge_depth_offline"])
def test_the_ceiling_isnt_changed_when_a_floor_cant_be_read(inverter, conn, floor):
    inverter.unreadable.add(floor)
    with pytest.raises(control.Refused, match="Couldn't read the .* floor"):
        change(inverter, conn, "max_soc", 95, 90)
    assert inverter.writes == []


def test_a_write_that_did_not_arrive_is_reported_and_not_retried(inverter, conn):
    async def ignored(name, value):
        inverter.writes.append((name, value))

    inverter.write_setting = ignored
    with pytest.raises(control.Unconfirmed, match="still holds the old value"):
        change(inverter, conn, "min_soc", 5, 20)
    assert len(inverter.writes) == 1
    assert log(conn) == [("min_soc", 20, 5, "not_applied")]


def test_a_different_value_read_back_is_reported(inverter, conn):
    async def wrong(name, value):
        inverter.writes.append((name, value))
        inverter.held[name] = 7

    inverter.write_setting = wrong
    with pytest.raises(control.Unconfirmed, match="had 20, was sent 5 and now holds 7"):
        change(inverter, conn, "min_soc", 5, 20)
    assert log(conn) == [("min_soc", 20, 5, "mismatch")]
    assert conn.execute("SELECT held_value FROM setting_writes").fetchone() == (7,)


def test_a_setting_that_cant_be_read_back_is_unknown(inverter, conn):
    async def then_silent(name, value):
        inverter.writes.append((name, value))
        inverter.unreadable.add(name)

    inverter.write_setting = then_silent
    with pytest.raises(control.Unconfirmed, match="isn't known whether the change arrived"):
        change(inverter, conn, "backup", 1, 0)
    assert log(conn) == [("backup", 0, 1, "unknown")]


def test_a_lost_reply_is_settled_by_the_read_back(inverter, conn):
    async def lost_reply(name, value):
        inverter.writes.append((name, value))
        inverter.held[name] = value
        raise TimeoutError("no reply")

    inverter.write_setting = lost_reply
    assert change(inverter, conn, "backup", 1, 0)["outcome"] == "confirmed"
    assert log(conn) == [("backup", 0, 1, "confirmed")]


def test_the_log_lists_the_latest_first(inverter, conn):
    change(inverter, conn, "min_soc", 5, 20)
    change(inverter, conn, "backup", 1, 0)
    change(inverter, conn, "min_soc", 20, 5)
    assert [(w["setting"], w["new_value"]) for w in db.list_setting_writes(conn)] == [
        ("min_soc", 20), ("backup", 1), ("min_soc", 5),
    ]


# The web route.


@pytest.fixture
def collector(conn):
    app_collector = Collector("test", 0, conn)
    app_collector.inverter = SettingsInverter()
    return app_collector


@pytest.fixture
def send(collector, monkeypatch):
    """Sends a change as this machine, with no login, as the dashboard normally runs on the PC."""
    monkeypatch.setattr(config, "load_dotenv", lambda *_: None)
    for name in ("BIND_HOST", "DASHBOARD_USER", "DASHBOARD_PASSWORD"):
        monkeypatch.delenv(name, raising=False)

    def request(name="min_soc", base_url="http://127.0.0.1:8000", **body):
        body = body or {"expected": 20, "value": 5}

        async def put():
            transport = httpx.ASGITransport(app=create_app(collector))
            async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
                return await client.put(f"/api/inverter-settings/{name}", json=body)
        return asyncio.run(put())

    return request


def get(collector, path):
    async def send():
        transport = httpx.ASGITransport(app=create_app(collector))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)
    return asyncio.run(send())


def test_a_change_is_reported_logged_and_shown_straight_away(send, collector):
    response = send()
    assert response.status_code == 200
    assert response.json() == {"outcome": "confirmed", "old": 20, "new": 5}
    assert collector.latest["battery_limits"] == {"min_soc": 5, "reserve_soc": 20, "max_soc": 90}
    write = get(collector, "/api/inverter-settings/writes").json()["writes"][0]
    assert (write["setting"], write["old_value"], write["new_value"], write["outcome"]) == ("min_soc", 20, 5, "confirmed")


def test_switching_backup_shows_straight_away(send, collector):
    assert send("backup", expected=0, value=1).status_code == 200
    assert collector.inverter.writes == [("backup_mode_enable", 1)]
    assert collector.latest["schedule"]["backup_on"] is True


def test_a_stale_page_is_refused_with_a_reason(send, collector):
    response = send(expected=15, value=5)
    assert response.status_code == 409
    assert "changed on the inverter" in response.json()["detail"]
    assert collector.inverter.writes == []


def test_a_value_out_of_range_is_refused_with_a_reason(send, collector):
    response = send("max_soc", expected=90, value=15)
    assert response.status_code == 409
    assert "above the on-grid floor, 20%" in response.json()["detail"]
    assert collector.inverter.writes == []


def test_a_write_that_did_not_arrive_is_an_error_with_a_reason(send, collector):
    async def ignored(name, value):
        collector.inverter.writes.append((name, value))

    collector.inverter.write_setting = ignored
    response = send()
    assert response.status_code == 502
    assert "still holds the old value" in response.json()["detail"]
    assert collector.latest["battery_limits"]["min_soc"] == 20


def test_a_change_with_no_connection_is_refused(send, collector):
    collector.inverter = None
    response = send()
    assert response.status_code == 409
    assert "isn't connected" in response.json()["detail"]


@pytest.mark.parametrize("name", ["work_mode", "battery_discharge_depth", "eco_mode_1", ""])
def test_there_is_no_such_setting(send, collector, name):
    assert send(name).status_code in (404, 405)
    assert collector.inverter.writes == []


@pytest.mark.parametrize(
    "body",
    [
        {"expected": 20, "value": "5"},
        {"expected": 20, "value": 5.5},
        {"expected": 20, "value": True},
        {"expected": 20, "value": -1},
        {"expected": 20, "value": 101},
        {"expected": 20, "value": None},
        {"expected": "20", "value": 5},
        {"value": 5},
        {"expected": 20},
    ],
)
def test_a_badly_formed_change_never_reaches_the_inverter(send, collector, body):
    assert send(**body).status_code == 422
    assert collector.inverter.writes == []


def test_a_page_reached_by_another_name_cannot_change_the_inverter(send, collector):
    assert send(base_url="http://elsewhere.example:8000").status_code == 403
    assert collector.inverter.writes == []


def test_the_home_network_cannot_change_it_without_a_login(send, collector, monkeypatch):
    monkeypatch.setenv("BIND_HOST", "0.0.0.0")
    assert send().status_code == 403
    assert collector.inverter.writes == []


def test_on_the_network_only_a_logged_in_request_reaches_the_inverter(collector, monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda *_: None)
    monkeypatch.setenv("BIND_HOST", "0.0.0.0")
    monkeypatch.setenv("DASHBOARD_USER", "user")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "password")
    app = create_app(collector)
    app.add_middleware(LoginRequired, username="user", password="password")
    body = {"expected": 0, "value": 1}

    async def send():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://solar.lan:8000") as client:
            no_session = await client.put("/api/inverter-settings/backup", json=body)
            made_up = await client.put("/api/inverter-settings/backup", json=body, headers={"Cookie": f"{COOKIE}=made-up"})
            sent_before_login = list(collector.inverter.writes)
            await client.post("/login", data={"username": "user", "password": "password"})
            sent = await client.put("/api/inverter-settings/backup", json=body)
        return no_session, made_up, sent_before_login, sent

    no_session, made_up, sent_before_login, sent = asyncio.run(send())
    assert (no_session.status_code, made_up.status_code) == (401, 401)
    assert sent_before_login == []
    assert sent.status_code == 200
    assert collector.inverter.writes == [("backup_mode_enable", 1)]


def test_a_refused_change_still_refreshes_what_the_page_shows(collector):
    collector.inverter.held["battery_discharge_depth"] = 15
    with pytest.raises(control.Refused):
        asyncio.run(collector.set_setting("min_soc", 5, 20))
    assert collector.inverter.writes == []
    assert collector.latest["battery_limits"]["min_soc"] == 15


def test_the_modes_and_limits_can_change_from_the_page(collector):
    page = get(collector, "/").text
    tab = page.split('id="view-schedule"', 1)[1].split("</section>", 1)[0]
    for part in ("schedule-modes", "limit-change", "limit-value", "limit-send", "limit-result"):
        assert f'id="{part}"' in tab
