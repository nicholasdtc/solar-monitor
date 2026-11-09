"""Automation's sunny morning rule: what it would decide from made-up inputs, when it decides, and
that it only ever suggests. All values here are made up."""

import ast
import asyncio
import pathlib
from datetime import datetime, timedelta

import httpx
import pytest

from solar_monitor import automation, db, importer
from solar_monitor.api import create_app
from solar_monitor.collector import Collector
from solar_monitor.schedule import ALL_MONTHS, next_start
from solar_monitor.weather.base import Hour

from logins import Browser, add_account, real_app

HOUR = 3600
EVENING = datetime(2030, 6, 12, 20, 0)  # A Wednesday, the evening before the charge.
CHARGE_STARTS = datetime(2030, 6, 13, 2, 0)
SUNRISE = datetime(2030, 6, 13, 6, 0)
MIDDAY = datetime(2030, 6, 13, 12, 0)


def ts(when: datetime) -> int:
    return int(when.timestamp())


def period(slot=1, start="02:00", end="05:00", cutoff=90, action="charge", state="on", day_bits=-1, month_bits=ALL_MONTHS, next_day=False):
    return {
        "slot": slot, "state": state, "start": start, "end": end, "next_day": next_day, "action": action,
        "power_pct": 50, "cutoff_soc": cutoff, "day_bits": day_bits, "month_bits": month_bits, "raw": [1, 2, 3],
    }


def schedule(*periods, tou_on=True):
    return {"tou_on": tou_on, "work_mode": "Eco", "periods": list(periods) or [period()]}


def morning_forecast(watts):
    """Each hour of the morning after the charge, expecting `watts` of solar."""
    return [{"hour_start": ts(SUNRISE) + k * HOUR, "expected_w": watts} for k in range(-2, 8)]


def state(**changes):
    values = {
        "now": ts(EVENING),
        "settings": {"auto_sunny_sunny_soc": 60.0, "auto_sunny_margin_pct": 20.0},
        "schedule": schedule(),
        "capacity_kwh": 10.0,
        "capacity_source": "entered",
        "load_by_hour": {hour: 300.0 for hour in range(24)},
        "error_pct": 15.0,
        "fit_status": "ready",
        "sunrises": [ts(SUNRISE)],
    }
    return automation.State(**(values | changes))


RULE = automation.rules["sunny_morning"]


def test_a_sunny_morning_lowers_the_cut_off():
    rec = automation.evaluate(RULE, morning_forecast(2000), state())
    # 6 h of 2 kW less 20% is 9.6 kWh, less 1.8 kWh of load: 7.8 kWh spare, beside 10 kWh × 30% of room.
    assert rec.would == 60
    assert rec.current == 90
    assert rec.setting == "TOU 1 cut-off"
    assert rec.reason == "Sunny morning: 7.8 kWh spare, 3.0 room"
    figures = rec.figures
    assert figures["for_ts"] == ts(CHARGE_STARTS)
    assert (figures["morning_start"], figures["morning_end"]) == (ts(SUNRISE), ts(MIDDAY))
    assert (figures["pv_kwh"], figures["load_kwh"], figures["spare_kwh"], figures["room_kwh"]) == (12.0, 1.8, 7.8, 3.0)
    assert len(figures["hours"]) == 6


def test_a_dull_morning_keeps_the_usual_cut_off():
    rec = automation.evaluate(RULE, morning_forecast(500), state())
    assert rec.would == 90
    assert rec.reason == "Not sunny enough: 0.6 of 3.0 kWh"


def test_the_margin_comes_off_the_forecast():
    # 4.2 kWh of solar less 1.8 kWh of load is 2.4 kWh spare, under 3.0 room; 6 kWh leaves 4.2 spare, unless half comes off.
    assert automation.evaluate(RULE, morning_forecast(700), state(settings={"auto_sunny_sunny_soc": 60.0, "auto_sunny_margin_pct": 0.0})).would == 90
    assert automation.evaluate(RULE, morning_forecast(1000), state(settings={"auto_sunny_sunny_soc": 60.0, "auto_sunny_margin_pct": 0.0})).would == 60
    assert automation.evaluate(RULE, morning_forecast(1000), state(settings={"auto_sunny_sunny_soc": 60.0, "auto_sunny_margin_pct": 50.0})).would == 90


def test_an_unset_margin_is_the_forecasts_own_error():
    rec = automation.evaluate(RULE, morning_forecast(2000), state(settings={"auto_sunny_sunny_soc": 60.0}, error_pct=35.0))
    assert rec.figures["margin_pct"] == 35.0
    assert "auto_sunny_margin_pct" in rec.figures["defaults"]
    rec = automation.evaluate(RULE, morning_forecast(2000), state(settings={"auto_sunny_sunny_soc": 60.0}, error_pct=None))
    assert (rec.would, rec.reason) == (None, "Forecast error not known yet")


def test_a_forecast_still_learning_still_decides():
    rec = automation.evaluate(RULE, morning_forecast(2000), state(fit_status="learning"))
    assert rec.would == 60
    assert rec.figures["fit_status"] == "learning"


USUAL_SET = {"auto_sunny_usual_soc": 90.0, "auto_sunny_sunny_soc": 60.0, "auto_sunny_margin_pct": 20.0}


def test_the_sunny_cut_off_counts_as_its_own():
    # Already lowered, as V2 would, it's still the rule's to change. Unset, the usual cut-off is the
    # period's own, so it's set here.
    rec = automation.evaluate(RULE, morning_forecast(500), state(settings=USUAL_SET, schedule=schedule(period(cutoff=60))))
    assert (rec.current, rec.would) == (60, 90)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"settings": USUAL_SET, "schedule": schedule(period(cutoff=75))}, "Changed by hand"),
        ({"schedule": schedule(period(), tou_on=False)}, "TOU is off"),
        ({"settings": {"auto_sunny_margin_pct": 20.0}}, "Sunny cut-off not set"),
        ({"settings": {"auto_sunny_sunny_soc": 95.0}}, "Sunny cut-off not below usual"),
        ({"settings": {"auto_sunny_sunny_soc": 60.0, "auto_sunny_slot": 2.0}}, "Not a charge period that's on"),
        ({"schedule": schedule(period(month_bits=0b10))}, "Changed by hand"),  # Not mid-June, but still a period.
        ({"capacity_kwh": None}, "Battery capacity unknown"),
        ({"fit_status": None}, "No solar forecast yet"),
        ({"load_by_hour": {}}, "Typical load not known"),
    ],
)
def test_when_it_cant_say(changes, reason):
    if changes.get("schedule", {}).get("periods", [{}])[0].get("month_bits") == 0b10:
        # A period set only for February next runs then; its cut-off differs from the usual one set here.
        changes["settings"] = {"auto_sunny_sunny_soc": 60.0, "auto_sunny_usual_soc": 80.0}
    rec = automation.evaluate(RULE, morning_forecast(2000), state(**changes))
    assert rec.would is None
    assert rec.reason == reason


def test_no_charge_period_means_nothing_to_say():
    discharge = schedule(period(action="discharge"))
    assert automation.evaluate(RULE, morning_forecast(2000), state(schedule=discharge)) is None
    assert RULE.decision(state(schedule=discharge)) is None
    assert automation.evaluate(RULE, morning_forecast(2000), state(schedule=None)) is None


def test_a_forecast_that_stops_short_says_so():
    rec = automation.evaluate(RULE, morning_forecast(2000)[:4], state())
    assert (rec.would, rec.reason) == (None, "Forecast doesn't reach the morning")


def test_without_a_sunrise_the_morning_starts_with_the_forecast_sun():
    forecast = [{"hour_start": ts(SUNRISE) + k * HOUR, "expected_w": 0 if k < 1 else 2000} for k in range(-2, 8)]
    rec = automation.evaluate(RULE, forecast, state(sunrises=[]))
    assert rec.figures["morning_start"] == ts(SUNRISE) + HOUR
    dark = [{"hour_start": h["hour_start"], "expected_w": 0} for h in forecast]
    assert automation.evaluate(RULE, dark, state(sunrises=[])).reason == "No sun before midday"


def test_the_usual_cut_off_and_period_default_to_the_inverters():
    chosen, unset = RULE.resolve(state(schedule=schedule(period(slot=1, action="discharge"), period(slot=3, cutoff=85))))
    assert chosen["auto_sunny_slot"] == 3
    assert chosen["auto_sunny_usual_soc"] == 85
    assert chosen["auto_sunny_lead_min"] == automation.DEFAULT_DECIDE_MINUTES
    assert {"auto_sunny_slot", "auto_sunny_usual_soc", "auto_sunny_lead_min"} <= set(unset)
    assert "auto_sunny_sunny_soc" not in unset


def test_it_decides_ahead_of_the_period():
    assert RULE.decision(state()) == (ts(CHARGE_STARTS), ts(CHARGE_STARTS) - 60 * 60)
    lead = state(settings={"auto_sunny_sunny_soc": 60.0, "auto_sunny_lead_min": 15.0})
    assert RULE.decision(lead) == (ts(CHARGE_STARTS), ts(CHARGE_STARTS) - 15 * 60)


def test_the_next_start_follows_the_days_and_months():
    wednesday_noon = datetime(2030, 6, 12, 12, 0)
    assert next_start(period(start="13:00"), wednesday_noon) == datetime(2030, 6, 12, 13, 0)
    assert next_start(period(start="11:00"), wednesday_noon) == datetime(2030, 6, 13, 11, 0)
    saturday = 1 << 6  # Bit 0 is Sunday.
    assert next_start(period(start="11:00", day_bits=saturday), wednesday_noon) == datetime(2030, 6, 15, 11, 0)
    august = 1 << 7
    assert next_start(period(start="11:00", month_bits=august), wednesday_noon) == datetime(2030, 8, 1, 11, 0)
    assert next_start(period(start="11:00", day_bits=0), wednesday_noon) is None
    assert next_start(period(start="soon"), wednesday_noon) is None
    # A period past midnight starts on the day it starts.
    assert next_start(period(start="23:00", end="02:00", next_day=True), wednesday_noon) == datetime(2030, 6, 12, 23, 0)


def test_problem_checks_the_bounds():
    assert automation.problem("auto_sunny_sunny_soc", 60) is None
    assert automation.problem("auto_sunny_sunny_soc", None) is None
    assert automation.problem("auto_sunny_sunny_soc", 5) == "Sunny cut-off (%): 10 to 100"
    assert automation.problem("auto_sunny_margin_pct", 91) == "Margin (%): 0 to 90"
    assert automation.problem("auto_sunny_slot", 1.5) == "TOU period: a whole number"
    assert automation.problem("auto_sunny_slot", True) == "TOU period: a whole number"
    assert automation.problem("limit_amps", 20) == "limit_amps isn't an automation setting"


def test_the_import_checks_the_bounds_too():
    assert importer._setting({"key": "auto_sunny_margin_pct", "value": 20}) == ("auto_sunny_margin_pct", 20)
    with pytest.raises(ValueError, match="0 to 90"):
        importer._setting({"key": "auto_sunny_margin_pct", "value": 95})


# --- Logging decisions ------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    yield conn
    conn.close()


def test_tick_logs_one_decision_for_each_start(conn):
    calls = []

    def state_of(full):
        calls.append(full)
        return morning_forecast(2000), state()

    assert automation.tick(conn, ts(CHARGE_STARTS) - 2 * HOUR, state_of) == 0  # Not due yet.
    assert calls == [False]
    assert automation.tick(conn, ts(CHARGE_STARTS) - HOUR, state_of) == 1
    assert automation.tick(conn, ts(CHARGE_STARTS) - HOUR + 60, state_of) == 0
    assert calls.count(True) == 1
    [run] = db.list_automation_runs(conn, "sunny_morning")
    assert run["for_ts"] == ts(CHARGE_STARTS)
    assert run["recommendation"]["would"] == 60
    assert run["inputs"]["capacity_kwh"] == 10.0
    assert all("raw" not in p for p in run["inputs"]["periods"])
    assert run["inputs"]["load_by_hour"]["7"] == 300


def test_tick_logs_why_it_couldnt_decide(conn):
    automation.tick(conn, ts(CHARGE_STARTS) - HOUR, lambda full: ([], state()))
    [run] = db.list_automation_runs(conn)
    assert run["recommendation"]["would"] is None
    assert run["recommendation"]["reason"] == "Forecast doesn't reach the morning"


def test_runs_list_newest_first(conn):
    for day in range(3):
        assert db.add_automation_run(conn, 100 + day, "sunny_morning", 1000 + day, {}, {"would": day})
    assert not db.add_automation_run(conn, 200, "sunny_morning", 1001, {}, {"would": 9})
    assert [r["for_ts"] for r in db.list_automation_runs(conn, limit=2)] == [1002, 1001]
    assert db.list_automation_runs(conn, "another") == []


# --- What the log says ------------------------------------------------------------------------


def minutes(conn, start, end, pv_w, load_w, step=60):
    db.save_samples(conn, [db.Sample(t, {"ppv": pv_w, "load_ptotal": load_w}) for t in range(start, end, step)])


def test_window_made_from_the_log(conn):
    start = ts(SUNRISE)
    minutes(conn, start - HOUR, start + 7 * HOUR, 1000, 250)
    assert db.window_made(conn, start, start + 6 * HOUR) == {"pv_kwh": 6.0, "load_kwh": 1.5}
    assert db.window_made(conn, start, start + 8 * HOUR) is None  # Not over yet.
    assert db.window_made(conn, start, start) is None


def test_window_made_needs_enough_readings(conn):
    start = ts(SUNRISE)
    minutes(conn, start, start + 3 * HOUR, 1000, 250, step=300)
    minutes(conn, start + 3 * HOUR, start + 3 * HOUR + 60, 0, 0)
    assert db.window_made(conn, start, start + 2 * HOUR) is None


def test_typical_load_by_local_hour(conn):
    day = datetime(2030, 6, 10)
    for d in range(2):
        minutes(conn, ts(day + timedelta(days=d, hours=7)), ts(day + timedelta(days=d, hours=8)), 0, 200 + 200 * d)
        minutes(conn, ts(day + timedelta(days=d, hours=19)), ts(day + timedelta(days=d, hours=20)), 0, 1000)
    assert db.typical_load_by_hour(conn, ts(day)) == {7: 300.0, 19: 1000.0}
    assert db.typical_load_by_hour(conn, ts(day + timedelta(days=1))) == {7: 400.0, 19: 1000.0}


def test_one_forecast_a_day_is_kept_after_two_weeks(conn):
    day = datetime(2030, 5, 1)

    def fetch(when):
        return db.save_forecast(conn, ts(when), "made-up", [Hour(hour_start=ts(when) + k * HOUR) for k in range(1, 7)])

    morning, evening = fetch(day + timedelta(hours=8)), fetch(day + timedelta(hours=20))
    recent = fetch(day + timedelta(days=10, hours=8))
    with_hours = lambda: {row[0] for row in conn.execute("SELECT DISTINCT fetch_id FROM forecast_hours")}
    assert {morning, evening, recent} <= with_hours()
    latest = fetch(day + timedelta(days=20, hours=8))
    assert morning not in with_hours()
    assert {evening, recent, latest} <= with_hours()


# --- The API ----------------------------------------------------------------------------------


@pytest.fixture
def collector(conn):
    return Collector("test", 0, conn)


@pytest.fixture
def call(collector):
    app = create_app(collector)

    def request(method, path, **kwargs):
        async def send():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.request(method, path, **kwargs)
        return asyncio.run(send())

    return request


def test_the_api_shows_the_rule(call, collector, conn):
    collector.latest["schedule"] = schedule(period(slot=2, cutoff=80))
    db.add_automation_run(conn, 1, "sunny_morning", 2, {}, {"would": 60, "reason": "Made up", "figures": {}})
    [rule] = call("GET", "/api/automation").json()["rules"]
    assert rule["name"] == "sunny_morning"
    settings = {s["key"]: s for s in rule["settings"]}
    assert settings["auto_sunny_slot"]["effective"] == 2 and settings["auto_sunny_slot"]["default"]
    assert settings["auto_sunny_usual_soc"]["effective"] == 80
    assert rule["periods"] == [{"slot": 2, "start": "02:00", "end": "05:00", "next_day": False, "cutoff_soc": 80}]
    assert rule["suggestion"]["reason"] == "Sunny cut-off not set"
    assert rule["decision"]["for_ts"] > rule["decision"]["decide_ts"]
    assert [r["outcome"] for r in rule["runs"]] == [None]


def test_the_api_changes_the_settings(call, conn):
    answer = call("PUT", "/api/automation/sunny_morning", json={"auto_sunny_sunny_soc": 55, "auto_sunny_lead_min": 30})
    assert answer.status_code == 200
    assert db.get_settings(conn)["auto_sunny_sunny_soc"] == 55
    settings = {s["key"]: s for s in answer.json()["rules"][0]["settings"]}
    assert settings["auto_sunny_lead_min"]["value"] == 30
    assert call("PUT", "/api/automation/sunny_morning", json={"auto_sunny_sunny_soc": None}).status_code == 200
    assert db.get_settings(conn)["auto_sunny_sunny_soc"] is None
    assert "Sunny morning: sunny cut-off" in call("GET", "/api/actions").text


@pytest.mark.parametrize(
    ("path", "body", "status"),
    [
        ("/api/automation/sunny_morning", {"auto_sunny_sunny_soc": 5}, 400),
        ("/api/automation/sunny_morning", {"auto_sunny_slot": 2.5}, 400),
        ("/api/automation/sunny_morning", {"limit_amps": 20}, 400),
        ("/api/automation/another", {"auto_sunny_sunny_soc": 50}, 404),
    ],
)
def test_the_api_refuses_bad_settings(call, conn, path, body, status):
    assert call("PUT", path, json=body).status_code == status
    assert db.get_settings(conn)["auto_sunny_sunny_soc"] is None


def test_changing_the_rule_needs_schedule_and_edit_data(conn):
    add_account(conn, "owner", admin=True)
    add_account(conn, "viewer", permissions=["schedule"])
    add_account(conn, "editor", permissions=["schedule", "edit"])
    add_account(conn, "elsewhere", permissions=[p for p in ("statistics", "edit")])
    app = real_app(conn)
    body = {"json": {"auto_sunny_sunny_soc": 60}}
    assert Browser(app).log_in("elsewhere").get("/api/automation").status_code == 403
    assert Browser(app).log_in("viewer").get("/api/automation").status_code == 200
    assert Browser(app).log_in("viewer").request("PUT", "/api/automation/sunny_morning", **body).status_code == 403
    assert Browser(app).log_in("editor").request("PUT", "/api/automation/sunny_morning", **body).status_code == 200


# --- Suggestions only ---------------------------------------------------------------------------


def test_automation_never_reaches_the_inverter_code():
    """Nothing automation imports, however far down, leads to control.py or the collector, which
    write to the inverter. V2 is where a rule first acts, and only through control's checked path."""
    src = pathlib.Path(automation.__file__).parent.parent

    def file_of(module):
        path = src.joinpath(*module.split("."))
        return path / "__init__.py" if path.is_dir() else path.with_suffix(".py")

    def imports(module):
        tree = ast.parse(file_of(module).read_text(encoding="utf-8"))
        package = module if file_of(module).name == "__init__.py" else module.rpartition(".")[0]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                yield from (alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    base = ".".join(package.split(".")[: len(package.split(".")) - node.level + 1] + ([base] if base else []))
                yield base
                yield from (f"{base}.{alias.name}" for alias in node.names)

    seen, todo = set(), ["solar_monitor.automation"]
    while todo:
        module = todo.pop()
        if module in seen or not module.startswith("solar_monitor") or not file_of(module).exists():
            continue
        seen.add(module)
        parts = module.split(".")
        todo.extend(".".join(parts[:i]) for i in range(1, len(parts)))  # Its packages run first.
        todo.extend(imports(module))
    assert "solar_monitor.db" in seen and "solar_monitor.schedule" in seen
    assert "solar_monitor.control" not in seen
    assert "solar_monitor.collector" not in seen
