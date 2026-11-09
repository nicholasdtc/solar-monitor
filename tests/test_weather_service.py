"""The weather fetch loop, with stub providers and a made-up clock, so nothing touches the network."""

import asyncio
import json

import pytest

from solar_monitor import db, weather
from solar_monitor.weather import service
from solar_monitor.weather.base import HOUR, Credit, FetchError, Hour, Location, NotModified, RateLimited, Region
from solar_monitor.weather.base import Warning as WeatherWarning

START = 1_000_000 * HOUR  # On the hour, so the slots are easy to read.


class StubForecast:
    name = "stub"
    title = "Stub"
    credit = Credit("Stub", "none", "https://example.test")
    has_irradiance = True
    has_past = False
    min_interval = HOUR

    def __init__(self):
        self.calls = []
        self.raises = None

    def forecast(self, where, fetch):
        self.calls.append(where)
        if self.raises:
            raise self.raises
        return [Hour(hour_start=START + i * HOUR, temperature_c=10.0) for i in range(3)]

    def past(self, where, start, end, fetch):
        return []


class StubWarnings:
    name = "stub-warnings"
    title = "Stub warnings"
    credit = Credit("Stub", "none", "https://example.test")
    min_interval = 20 * 60
    regions = (Region("R1", "One"), Region("R2", "Two"))

    def __init__(self):
        self.calls = []

    def warnings(self, where, region, fetch):
        self.calls.append(region)
        return [WeatherWarning(self.name, "1", "moderate", "Rain", "Rain", "Rain", None, None, ("R1",), "https://x")]


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    yield connection
    connection.close()


@pytest.fixture
def stub(monkeypatch):
    provider = StubForecast()
    monkeypatch.setitem(weather.FORECASTS, provider.name, provider)
    return provider


@pytest.fixture
def stub_warnings(monkeypatch):
    provider = StubWarnings()
    monkeypatch.setitem(weather.WARNINGS, provider.name, provider)
    return provider


def no_fetch(url, params):
    raise AssertionError("the stub providers don't fetch")


def run(conn, now, state):
    return asyncio.run(service.run_once(conn, now, no_fetch, state))


def placed(conn, **more):
    db.save_settings(conn, {"latitude": 10.0, "longitude": 20.0, "forecast_provider": "stub", **more})


def outcomes(conn):
    return [row for row in conn.execute("SELECT kind, outcome, items, error FROM weather_fetches ORDER BY id")]


def test_nothing_is_fetched_without_a_location(conn, stub):
    db.save_settings(conn, {"latitude": 10.0, "forecast_provider": "stub"})
    assert run(conn, START, service.State(minute=17)) == START + service.CHECK_SECONDS
    assert stub.calls == [] and outcomes(conn) == []


def test_a_forecast_is_fetched_then_not_again_until_its_slot(conn, stub):
    placed(conn, panel_tilt_deg=30.0, panel_azimuth_deg=200.0, panel_azimuth_2_deg=20.0)
    state = service.State(minute=17)
    assert run(conn, START + 5 * 60, state) == START + 5 * 60 + service.CHECK_SECONDS
    assert stub.calls == [Location(10.0, 20.0, panel_tilt=30.0, panel_facing=200.0, panel_facing_2=20.0)]
    assert outcomes(conn) == [("forecast", "ok", 3, None)]
    assert db.latest_forecast(conn, START)["provider"] == "stub"
    # Next at 17 past, an hour or more on: from 05 past, that's 17 past the next hour.
    assert state.forecast.due == START + HOUR + 17 * 60
    run(conn, START + HOUR + 10 * 60, state)
    assert len(stub.calls) == 1
    run(conn, state.forecast.due, state)
    assert len(stub.calls) == 2


@pytest.mark.parametrize(
    ("now", "interval", "minute", "slot"),
    [(0, HOUR, 17, HOUR + 17 * 60), (17 * 60, HOUR, 17, HOUR + 17 * 60), (18 * 60, HOUR, 17, 2 * HOUR + 17 * 60)],
)
def test_next_slot(now, interval, minute, slot):
    assert service.next_slot(now, interval, minute) == slot


def test_short_intervals_keep_the_random_minute_within_them():
    slot = service.next_slot(0, 20 * 60, 47)
    assert slot >= 20 * 60 and slot % (20 * 60) == 7 * 60


def test_rate_limited_waits_for_the_next_hour(conn, stub):
    placed(conn)
    stub.raises = RateLimited("HTTP 429", 429)
    state = service.State(minute=17)
    run(conn, START + 5 * 60, state)
    assert state.forecast.due == START + HOUR
    assert outcomes(conn) == [("forecast", "rate limited", 0, "HTTP 429")]
    run(conn, START + 50 * 60, state)
    assert len(stub.calls) == 1


def test_not_modified_keeps_the_last_forecast(conn, stub):
    placed(conn)
    stub.raises = NotModified()
    state = service.State(minute=17)
    run(conn, START, state)
    assert outcomes(conn) == [("forecast", "not modified", 0, None)]
    assert db.latest_forecast(conn, 0) is None
    assert state.forecast.due == service.next_slot(START, HOUR, 17)


def test_errors_back_off_doubling_up_to_an_hour(conn, stub):
    placed(conn)
    stub.raises = FetchError("HTTP 503", 503)
    state = service.State(minute=17)
    now = START
    waits = []
    for _ in range(6):
        run(conn, now, state)
        waits.append(state.forecast.due - now)
        now = state.forecast.due
    assert waits == [300, 600, 1200, 2400, 3600, 3600]
    assert outcomes(conn)[0] == ("forecast", "error", 0, "HTTP 503")
    stub.raises = None
    run(conn, now, state)
    assert state.forecast.failures == 0 and outcomes(conn)[-1][1] == "ok"


def test_a_provider_bug_counts_as_an_error(conn, stub):
    placed(conn)
    stub.raises = KeyError("hourly")
    run(conn, START, service.State(minute=17))
    assert outcomes(conn) == [("forecast", "error", 0, "KeyError: 'hourly'")]


def test_a_change_of_location_or_provider_fetches_at_once(conn, stub, monkeypatch):
    placed(conn)
    state = service.State(minute=17)
    run(conn, START, state)
    db.save_settings(conn, {"latitude": 11.0})
    run(conn, START + 60, state)
    assert [w.latitude for w in stub.calls] == [10.0, 11.0]
    other = StubForecast()
    other.name = "other"
    monkeypatch.setitem(weather.FORECASTS, "other", other)
    db.save_settings(conn, {"forecast_provider": "other"})
    run(conn, START + 120, state)
    assert len(other.calls) == 1


def test_an_unknown_forecast_provider_is_skipped(conn, stub):
    placed(conn, forecast_provider=None)
    conn.execute("INSERT INTO text_settings (key, value) VALUES ('forecast_provider', 'gone')")
    conn.commit()
    run(conn, START, service.State(minute=17))
    assert stub.calls == [] and outcomes(conn) == []


def test_warnings_only_with_a_provider_and_its_own_region(conn, stub, stub_warnings):
    placed(conn)
    state = service.State(minute=17)
    run(conn, START, state)
    assert stub_warnings.calls == []
    db.save_settings(conn, {"warnings_provider": "stub-warnings", "warnings_region": "R2"})
    run(conn, START + 60, state)
    assert stub_warnings.calls == ["R2"]
    assert state.warnings.due == service.next_slot(START + 60, 20 * 60, 17)
    assert [w["id"] for w in db.current_warnings(conn, "stub-warnings", START)] == ["1"]
    # A region the provider doesn't have, kept from another, means all of them.
    db.save_settings(conn, {"warnings_region": "EI07"})
    run(conn, START + 120, state)
    assert stub_warnings.calls == ["R2", None]


def test_the_real_providers_with_saved_answers(conn):
    import json
    from pathlib import Path

    samples = Path(__file__).parent / "weather_samples"
    answers = {
        "open-meteo": json.loads((samples / "open_meteo_forecast.json").read_text(encoding="utf-8")),
        "met.ie": json.loads((samples / "met_eireann.json").read_text(encoding="utf-8")),
    }

    def fetch(url, params):
        return answers["met.ie" if "met.ie" in url else "open-meteo"]

    db.save_settings(conn, {"latitude": 10.0, "longitude": 20.0, "warnings_provider": "met-eireann"})
    asyncio.run(service.run_once(conn, START, fetch, service.State(minute=0)))
    assert [row[:2] for row in outcomes(conn)] == [("forecast", "ok"), ("warnings", "ok")]
    assert db.latest_forecast(conn, 0)["provider"] == weather.DEFAULT_FORECAST


def test_settings_take_only_registered_providers_and_regions():
    from pydantic import ValidationError

    from solar_monitor.inputs import SettingsIn

    ok = SettingsIn(forecast_provider="met-norway", warnings_provider="met-eireann", warnings_region="EI07")
    assert ok.model_dump(exclude_unset=True) == {
        "forecast_provider": "met-norway",
        "warnings_provider": "met-eireann",
        "warnings_region": "EI07",
    }
    assert SettingsIn(forecast_provider=None).forecast_provider is None
    for bad in ({"forecast_provider": "nope"}, {"warnings_provider": "open-meteo"}, {"warnings_region": "XX"}):
        with pytest.raises(ValidationError):
            SettingsIn(**bad)


# The app, with the weather loop started by its lifespan and answers made up around now


def canned(now):
    hour = now - now % HOUR
    answer = {
        "hourly": {
            "time": [hour + i * HOUR for i in range(4)],
            "temperature_2m": [8.0, 9.0, 10.0, 11.0],
            "weather_code": [0, 1, 2, 3],
            "is_day": [1, 1, 1, 1],
        }
    }
    issued = [
        {
            "id": 9,
            "level": "Yellow",
            "type": "Wind",
            "headline": "Wind warning",
            "description": "<p>Gusts</p>",
            "onset": "2026-01-01T00:00:00+00:00",
            "expiry": "2099-01-01T00:00:00+00:00",
            "regions": ["EI07"],
        }
    ]

    def fetch(url, params):
        return issued if "met.ie" in url else answer

    return fetch


def test_the_app_fetches_on_start_and_serves_the_weather(conn, monkeypatch):
    import time

    import httpx

    from solar_monitor.api import create_app
    from solar_monitor.collector import Collector

    db.save_settings(conn, {"latitude": 10.0, "longitude": 20.0, "warnings_provider": "met-eireann"})
    app_collector = Collector("test", 0, conn)

    async def idle():
        await asyncio.Event().wait()

    monkeypatch.setattr(app_collector, "run", idle)
    monkeypatch.setattr(app_collector, "flush", lambda: None)
    app = create_app(app_collector, fetch=canned(int(time.time())))

    async def scenario():
        async with app.router.lifespan_context(app):
            for _ in range(200):
                if len(db.last_weather_fetches(conn)) == 2:
                    break
                await asyncio.sleep(0.01)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return (await client.get("/api/weather")).json(), (await client.get("/api/weather/providers")).json()

    found, providers = asyncio.run(scenario())
    assert found["located"] is True
    assert [h["temperature_c"] for h in found["forecast"]["hours"]] == [8.0, 9.0, 10.0]
    assert found["forecast"]["hours"][0]["condition"] == "clear"
    assert [(w["id"], w["level"], w["description"]) for w in found["warnings"]] == [("9", "moderate", "<p>Gusts</p>")]
    assert [c["title"] for c in found["credits"]] == ["Open-Meteo", "Met Éireann"]
    assert {k: v["outcome"] for k, v in found["fetches"].items()} == {"forecast": "ok", "warnings": "ok"}
    assert "latitude" not in json.dumps(found) and "10.0" not in json.dumps(found["fetches"])
    assert providers["default_forecast"] == "open-meteo"
    assert {p["name"] for p in providers["forecasts"]} == set(weather.FORECASTS)
    assert len(providers["warnings"][0]["regions"]) == 26


# The record of past weather, with a stub that has one

MIDNIGHT = START // service.DAY * service.DAY + service.DAY  # Today's, UTC.
DAY = service.DAY


class StubPast(StubForecast):
    has_past = True

    def __init__(self):
        super().__init__()
        self.asked = []
        self.gives = True
        self.past_raises = None

    def past(self, where, start, end, fetch):
        self.asked.append((start, end))
        if self.past_raises and self.past_raises[0](start):
            raise self.past_raises[1]
        if not self.gives:
            return []
        return [Hour(hour_start=hour, temperature_c=5.0, gti_wm2=100.0) for hour in range(start, end, HOUR)]


@pytest.fixture
def past(monkeypatch):
    provider = StubPast()
    monkeypatch.setitem(weather.FORECASTS, provider.name, provider)
    return provider


def logged(conn, *times):
    db.save_samples(conn, [db.Sample(ts, {"ppv": 100}) for ts in times])


def fill(conn, now, state, rounds=10):
    """Run whenever the past job is due, until it waits for the night. Returns the last run's time."""
    for _ in range(rounds):
        run(conn, now, state)
        if state.past.due == service.next_night(now, state.minute):
            return now
        now = state.past.due
    raise AssertionError("still filling")


def test_the_past_is_filled_a_month_at_a_time_newest_first(conn, past):
    placed(conn)
    logged(conn, MIDNIGHT - 40 * DAY + 12 * HOUR)
    state = service.State(minute=17)
    fill(conn, MIDNIGHT + 10 * HOUR, state)
    assert past.asked == [(MIDNIGHT - 31 * DAY, MIDNIGHT), (MIDNIGHT - 40 * DAY, MIDNIGHT - 31 * DAY)]
    assert db.weather_hours_missing(conn, MIDNIGHT - 40 * DAY, MIDNIGHT, None) == []
    assert state.past.due == MIDNIGHT + DAY + 3 * HOUR + 17 * 60
    assert [row[:3] for row in outcomes(conn) if row[0] == "past"] == [("past", "ok", 31 * 24), ("past", "ok", 9 * 24)]
    # That night, only the day just over is asked for.
    run(conn, state.past.due, state)
    assert past.asked[-1] == (MIDNIGHT, MIDNIGHT + DAY)


def test_days_the_provider_doesnt_have_arent_asked_for_again_until_night(conn, past):
    placed(conn)
    logged(conn, MIDNIGHT - 3 * DAY)
    past.gives = False
    state = service.State(minute=17)
    now = fill(conn, MIDNIGHT + HOUR, state)
    assert past.asked == [(MIDNIGHT - 3 * DAY, MIDNIGHT)]
    assert state.past.due == service.next_night(now, 17)
    run(conn, state.past.due, state)
    assert len(past.asked) == 2


def test_a_change_of_panels_fills_the_past_again_for_them(conn, past):
    placed(conn)
    logged(conn, MIDNIGHT - 2 * DAY)
    state = service.State(minute=17)
    now = fill(conn, MIDNIGHT + HOUR, state)
    db.save_settings(conn, {"panel_tilt_deg": 30.0, "panel_azimuth_deg": 180.0})
    fill(conn, now + 60, state)
    assert past.asked == [(MIDNIGHT - 2 * DAY, MIDNIGHT)] * 2
    assert conn.execute("SELECT DISTINCT panels FROM weather_hours WHERE source = 'past:stub'").fetchall() == [("30/180",)]


def test_a_refusal_stops_the_older_days_until_night(conn, past):
    placed(conn)
    logged(conn, MIDNIGHT - 40 * DAY)
    past.past_raises = (lambda start: start < MIDNIGHT - 31 * DAY, FetchError("HTTP 400", 400))
    state = service.State(minute=17)
    now = fill(conn, MIDNIGHT + HOUR, state)
    assert past.asked == [(MIDNIGHT - 31 * DAY, MIDNIGHT), (MIDNIGHT - 40 * DAY, MIDNIGHT - 31 * DAY)]
    assert state.past.due == service.next_night(now, 17) and state.past.failures == 0
    assert ("past", "error", 0, "HTTP 400") in outcomes(conn)


def test_a_refusal_of_the_newest_days_still_asks_for_older_ones(conn, past):
    placed(conn)
    logged(conn, MIDNIGHT - 40 * DAY)
    past.past_raises = (lambda start: start == MIDNIGHT - 31 * DAY, FetchError("HTTP 400", 400))
    state = service.State(minute=17)
    fill(conn, MIDNIGHT + HOUR, state)
    assert past.asked == [(MIDNIGHT - 31 * DAY, MIDNIGHT), (MIDNIGHT - 40 * DAY, MIDNIGHT - 31 * DAY)]


def test_empty_hours_from_the_past_are_left_to_ask_for_again(conn):
    db.save_past(conn, START, "pa", [Hour(hour_start=START), Hour(hour_start=START + HOUR, cloud_pct=0.0)], None)
    assert sources(conn) == {START + HOUR: "past:pa"}
    assert outcomes(conn) == [("past", "ok", 1, None)]


def test_a_server_error_backs_off_and_asks_for_the_same_days(conn, past):
    placed(conn)
    logged(conn, MIDNIGHT - 2 * DAY)
    past.past_raises = (lambda start: True, FetchError("HTTP 503", 503))
    state = service.State(minute=17)
    run(conn, MIDNIGHT + HOUR, state)
    assert state.past.due == MIDNIGHT + HOUR + service.FIRST_BACKOFF
    past.past_raises = None
    run(conn, state.past.due, state)
    assert past.asked == [(MIDNIGHT - 2 * DAY, MIDNIGHT)] * 2


def test_no_past_is_asked_for_without_a_log(conn, past):
    placed(conn)
    state = service.State(minute=17)
    run(conn, MIDNIGHT + HOUR, state)
    assert past.asked == [] and state.past.due == service.next_night(MIDNIGHT + HOUR, 17)


def test_no_past_is_asked_for_from_a_provider_without_a_record(conn, stub):
    placed(conn)
    logged(conn, MIDNIGHT - DAY)
    state = service.State(minute=17)
    run(conn, MIDNIGHT + HOUR, state)
    assert state.past.key is None and all(row[0] != "past" for row in outcomes(conn))


@pytest.mark.parametrize(
    ("now", "night"),
    [
        (MIDNIGHT, MIDNIGHT + 3 * HOUR + 17 * 60),
        (MIDNIGHT + 3 * HOUR + 17 * 60, MIDNIGHT + DAY + 3 * HOUR + 17 * 60),
        (MIDNIGHT + 20 * HOUR, MIDNIGHT + DAY + 3 * HOUR + 17 * 60),
    ],
)
def test_next_night(now, night):
    assert service.next_night(now, 17) == night


def test_panels_of():
    assert service.panels_of(Location(1.0, 2.0)) is None
    assert service.panels_of(Location(1.0, 2.0, panel_tilt=30.0, panel_facing=180.0)) == "30/180"
    assert service.panels_of(Location(1.0, 2.0, panel_tilt=30.5, panel_facing=90.0, panel_facing_2=270.0)) == "30.5/90/270"
    assert service.panels_of(Location(1.0, 2.0, panel_tilt=30.0)) == "30/-"


def sources(conn):
    return dict(conn.execute("SELECT hour_start, source FROM weather_hours"))


def test_a_record_of_past_weather_beats_a_forecast_and_only_begun_hours_are_kept(conn):
    db.save_forecast(conn, START + HOUR, "fc", [Hour(hour_start=START + i * HOUR, temperature_c=1.0) for i in range(3)])
    assert sources(conn) == {START: "forecast:fc", START + HOUR: "forecast:fc"}
    db.save_past(conn, START + DAY, "pa", [Hour(hour_start=START, temperature_c=2.0)], "30/180")
    db.save_forecast(conn, START + 2 * HOUR, "fc2", [Hour(hour_start=START + i * HOUR, temperature_c=3.0) for i in range(3)])
    assert sources(conn) == {START: "past:pa", START + HOUR: "forecast:fc2", START + 2 * HOUR: "forecast:fc2"}
    db.save_past(conn, START + DAY, "pb", [Hour(hour_start=START, temperature_c=4.0)], None)
    assert sources(conn)[START] == "past:pb"
    assert [h["temperature_c"] for h in db.weather_history(conn, START, START + 3 * HOUR)] == [4.0, 3.0, 3.0]


def test_the_log_starts_at_the_midnight_before_its_first_entry(conn):
    assert db.log_start(conn) is None
    logged(conn, MIDNIGHT + 5 * HOUR)
    assert db.log_start(conn) == MIDNIGHT
    # A day on a local clock might have begun the UTC day before.
    conn.execute("INSERT INTO meter_readings (day, slot, occurrence, kw) VALUES ('2001-01-02', 0, 0, 1.0)")
    conn.commit()
    assert db.log_start(conn) == 978_307_200  # 2001-01-01, UTC


def test_the_history_routes(conn, past, monkeypatch):
    import time

    import httpx

    from solar_monitor.api import create_app
    from solar_monitor.collector import Collector

    app = create_app(Collector("test", 0, conn))
    monkeypatch.setattr(time, "time", lambda: MIDNIGHT + HOUR)

    def get(path):
        async def send():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                return await client.get(path)

        return asyncio.run(send())

    assert get("/api/weather").json()["history"] is None
    placed(conn)
    logged(conn, MIDNIGHT - 2 * DAY)
    db.save_past(conn, MIDNIGHT, "stub", [Hour(hour_start=MIDNIGHT - DAY + i * HOUR, is_day=True) for i in range(3)], None)
    found = get(f"/api/weather/history?start={MIDNIGHT - DAY}&end={MIDNIGHT}").json()
    assert [h["hour_start"] for h in found["hours"]] == [MIDNIGHT - DAY + i * HOUR for i in range(3)]
    assert found["hours"][0]["is_day"] is True and found["hours"][0]["source"] == "past:stub"
    status = get("/api/weather").json()["history"]
    assert status["from_past"] is True and status["start"] == MIDNIGHT - 2 * DAY and status["filled"] == 3
    assert status["hours"] == (status["end"] - status["start"]) // HOUR
    assert get(f"/api/weather/history?start={MIDNIGHT}&end={MIDNIGHT}").status_code == 400
    assert get(f"/api/weather/history?start=0&end={367 * DAY}").status_code == 400
