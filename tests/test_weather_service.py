"""The weather fetch loop, with stub providers and a made-up clock, so nothing touches the network."""

import asyncio

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
    placed(conn, panel_tilt_deg=30.0, panel_azimuth_deg=200.0)
    state = service.State(minute=17)
    assert run(conn, START + 5 * 60, state) == START + 5 * 60 + service.CHECK_SECONDS
    assert stub.calls == [Location(10.0, 20.0, panel_tilt=30.0, panel_facing=200.0)]
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
