"""The weather providers, each run against a saved, trimmed answer at made-up coordinates."""

import io
import json
import urllib.error
from pathlib import Path

import pytest

from solar_monitor import weather
from solar_monitor.weather import http, met_eireann, met_norway, open_meteo
from solar_monitor.weather.base import (
    CONDITIONS,
    HOUR,
    HOUR_FIELDS,
    LEVELS,
    FetchError,
    Hour,
    Location,
    NotModified,
    RateLimited,
)

SAMPLES = Path(__file__).parent / "weather_samples"
WHERE = Location(10.0, 20.0)


def sample(name):
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


class Saved:
    """A fetch that answers with a saved sample and remembers what it was asked."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, url, params):
        self.calls.append((url, params))
        return self.answer


def test_registry_names_match_their_keys():
    assert weather.DEFAULT_FORECAST in weather.FORECASTS
    for registry in (weather.FORECASTS, weather.WARNINGS):
        for name, provider in registry.items():
            assert provider.name == name
            assert provider.title and provider.credit.text and provider.credit.link


def test_hour_fields_follow_the_dataclass():
    assert HOUR_FIELDS[0] == "condition"
    assert "hour_start" not in HOUR_FIELDS
    assert HOUR_FIELDS[-1] == "is_day"


# Open-Meteo


def test_open_meteo_takes_instants_from_the_start_and_the_rest_from_an_hour_later():
    answer = sample("open_meteo_forecast.json")
    h = answer["hourly"]
    hours = open_meteo.hours_from(answer)
    assert [x.hour_start for x in hours] == h["time"][:-1]  # The last label's hour runs past the answer.
    for i, hour in enumerate(hours):
        assert hour.temperature_c == h["temperature_2m"][i]
        assert hour.cloud_pct == h["cloud_cover"][i]
        assert hour.wind_from_deg == h["wind_direction_10m"][i]
        assert hour.is_day == bool(h["is_day"][i])
        assert hour.ghi_wm2 == h["shortwave_radiation"][i + 1]
        assert hour.gti_wm2 == h["global_tilted_irradiance"][i + 1]
        assert hour.precipitation_mm == h["precipitation"][i + 1]
        assert hour.gust_ms == h["wind_gusts_10m"][i + 1]
        assert hour.condition == "cloudy"
    # The first hour starts in the dark, and its sunshine is the little before its end.
    assert hours[0].is_day is False and hours[0].ghi_wm2 == 7.0


def test_open_meteo_gaps_and_missing_columns_are_none():
    answer = {"hourly": {"time": [0, HOUR], "temperature_2m": [None, 1.0], "weather_code": [None, 0]}}
    (hour,) = open_meteo.hours_from(answer)
    assert hour == Hour(hour_start=0)


def test_open_meteo_rejects_an_answer_without_times():
    with pytest.raises(FetchError):
        open_meteo.hours_from({"error": True, "reason": "bad"})


@pytest.mark.parametrize(
    ("facing", "azimuth"), [(None, 0), (180, 0), (90, -90), (270, 90), (135, -45), (0, -180), (360, -180)]
)
def test_open_meteo_azimuth(facing, azimuth):
    assert open_meteo.open_meteo_azimuth(facing) == azimuth


def test_open_meteo_wmo_codes_are_all_conditions():
    assert set(open_meteo.WMO.values()) <= set(CONDITIONS)


def test_open_meteo_asks_for_the_panels_sunshine_only_with_a_tilt():
    fetch = Saved(sample("open_meteo_forecast.json"))
    provider = open_meteo.OpenMeteo()
    provider.forecast(WHERE, fetch)
    provider.forecast(Location(10.123, 20.456, panel_tilt=30, panel_facing=200), fetch)
    (url, flat), (_, tilted) = fetch.calls
    assert url == open_meteo.FORECAST_URL
    assert "global_tilted_irradiance" not in flat["hourly"] and "tilt" not in flat
    assert "global_tilted_irradiance" in tilted["hourly"]
    assert (tilted["tilt"], tilted["azimuth"]) == (30, 20)
    assert (tilted["latitude"], tilted["longitude"]) == (10.12, 20.46)
    assert flat["timezone"] == "GMT" and flat["timeformat"] == "unixtime" and flat["forecast_days"] == 7


def test_open_meteo_past_keeps_only_the_hours_asked_for():
    answer = sample("open_meteo_forecast.json")
    times = answer["hourly"]["time"]
    fetch = Saved(answer)
    hours = open_meteo.OpenMeteo().past(WHERE, times[1], times[3], fetch)
    assert [h.hour_start for h in hours] == times[1:3]
    ((url, params),) = fetch.calls
    assert url == open_meteo.PAST_URL
    assert "precipitation_probability" not in params["hourly"]
    assert params["start_date"] <= params["end_date"]


# MET Norway


def test_met_norway_hours():
    answer = sample("met_norway.json")
    hours = met_norway.hours_from(answer)
    series = answer["properties"]["timeseries"]
    assert len(hours) == len(series)
    first = hours[0]
    details = series[0]["data"]["instant"]["details"]
    assert first.temperature_c == details["air_temperature"]
    assert first.pressure_hpa == details["air_pressure_at_sea_level"]
    assert first.wind_ms == details["wind_speed"]
    assert first.condition == "cloudy" and first.is_day is None
    assert first.precipitation_mm == 0.0
    assert first.ghi_wm2 is None  # It has no sunshine figures.
    # Later entries are 6 hours apart: a condition, but no hour's rain.
    six = hours[3]
    assert six.condition is not None and six.precipitation_mm is None
    # The very last has only instant values.
    assert hours[-1].condition is None and hours[-1].temperature_c is not None


@pytest.mark.parametrize(
    ("symbol", "condition"),
    [
        ("clearsky_day", "clear"),
        ("fair_night", "mostly clear"),
        ("partlycloudy_polartwilight", "partly cloudy"),
        ("cloudy", "cloudy"),
        ("fog", "fog"),
        ("lightrain", "rain"),
        ("rain", "rain"),
        ("heavyrain", "heavy rain"),
        ("rainshowers_day", "showers"),
        ("heavysleetshowers_night", "sleet"),
        ("lightsnow", "snow"),
        ("lightrainshowersandthunder_day", "thunder"),
        ("something new", None),
        (None, None),
    ],
)
def test_met_norway_symbols(symbol, condition):
    assert met_norway.condition(symbol) == condition


def test_met_norway_asks_with_four_decimals_at_most():
    fetch = Saved(sample("met_norway.json"))
    provider = met_norway.MetNorway()
    provider.forecast(Location(10.123456, 20.987654), fetch)
    assert fetch.calls[0][1] == {"lat": 10.1235, "lon": 20.9877}
    assert provider.past(WHERE, 0, HOUR, fetch) == []


# Met Éireann


def test_met_eireann_keeps_its_regions_warnings_as_issued():
    answer = sample("met_eireann.json")
    everywhere = met_eireann.warnings_from(answer, None)
    assert [w.id for w in everywhere] == ["5", "7", "8"]
    kerry = met_eireann.warnings_from(answer, "EI11")
    assert [(w.id, w.level) for w in kerry] == [("5", "moderate"), ("8", "severe")]
    first = kerry[0]
    assert first.headline == answer[0]["headline"] and first.description == answer[0]["description"]
    assert first.kind == "Rain" and first.provider == "met-eireann"
    assert first.onset == 1792810800  # 04:00 Irish summer time, 03:00 UTC
    assert first.link.startswith("https://")
    assert met_eireann.warnings_from(answer, "EI31") == []


def test_met_eireann_levels():
    item = {"id": 1, "regions": ["EI07"]}
    levels = [
        met_eireann.warnings_from([{**item, **extra}], None)[0].level
        for extra in ({"level": "Red"}, {"severity": "Minor"}, {"level": "purple", "severity": "Extreme"}, {})
    ]
    assert levels == ["extreme", "minor", "extreme", "moderate"]
    assert set(met_eireann.COLOURS.values()) <= set(LEVELS)


def test_met_eireann_rejects_something_other_than_a_list():
    with pytest.raises(FetchError):
        met_eireann.warnings_from({"warnings": []}, None)


def test_met_eireann_regions_are_unique():
    codes = [r.code for r in met_eireann.COUNTIES]
    assert len(codes) == len(set(codes)) == 26


# The real fetch, with urlopen replaced


class Response:
    def __init__(self, body, headers):
        self.body = body
        self.headers = headers

    def read(self, limit):
        return self.body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_http_fetch_names_the_app_and_honours_expires_and_last_modified(monkeypatch):
    seen = []
    headers = {"Expires": "Mon, 09 Nov 2026 14:47:16 GMT", "Last-Modified": "Mon, 09 Nov 2026 14:17:04 GMT"}

    def urlopen(request, timeout):
        seen.append(request)
        return Response(b'{"ok": 1}', headers)

    monkeypatch.setattr(http.urllib.request, "urlopen", urlopen)
    now = [1794233000.0]  # 14:03:20, before it expires
    fetch = http.HttpFetch(clock=lambda: now[0])
    assert fetch("https://example.test/a", {"lat": 1}) == {"ok": 1}
    assert seen[0].full_url == "https://example.test/a?lat=1"
    assert seen[0].get_header("User-agent") == http.USER_AGENT
    assert "@" not in http.USER_AGENT
    with pytest.raises(NotModified):
        fetch("https://example.test/a", {"lat": 1})
    assert len(seen) == 1
    now[0] += 3600
    fetch("https://example.test/a", {"lat": 1})
    assert seen[1].get_header("If-modified-since") == headers["Last-Modified"]


@pytest.mark.parametrize(("status", "error"), [(304, NotModified), (429, RateLimited), (503, FetchError)])
def test_http_fetch_statuses(monkeypatch, status, error):
    def urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, status, "no", {}, io.BytesIO())

    monkeypatch.setattr(http.urllib.request, "urlopen", urlopen)
    with pytest.raises(error):
        http.HttpFetch()("https://example.test/a", {})


def test_http_fetch_no_answer_and_not_json(monkeypatch):
    def unreachable(request, timeout):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(http.urllib.request, "urlopen", unreachable)
    with pytest.raises(FetchError, match="no answer"):
        http.HttpFetch()("https://example.test/a", {})
    monkeypatch.setattr(http.urllib.request, "urlopen", lambda r, timeout: Response(b"<html>", {}))
    with pytest.raises(FetchError, match="JSON"):
        http.HttpFetch()("https://example.test/a", {})
