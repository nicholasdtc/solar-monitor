"""Open-Meteo: free for non-commercial use, no key, worldwide, with solar radiation and past data.

https://open-meteo.com/en/docs. Values at an instant (temperature, cloud, wind) are labelled by
that instant, and values over an hour (rain, gusts, radiation) by the end of the hour. Each Hour
here is labelled by its start, so it takes the instant from its start and the rest from the label
an hour later.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from solar_monitor.weather.base import HOUR, Credit, Fetch, Hour, Location, bad_answer, number

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
PAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"

# Its name -> the Hour field, for values at an instant.
INSTANT = {
    "temperature_2m": "temperature_c",
    "apparent_temperature": "feels_like_c",
    "relative_humidity_2m": "humidity_pct",
    "pressure_msl": "pressure_hpa",
    "cloud_cover": "cloud_pct",
    "cloud_cover_low": "cloud_low_pct",
    "cloud_cover_mid": "cloud_mid_pct",
    "cloud_cover_high": "cloud_high_pct",
    "wind_speed_10m": "wind_ms",
    "wind_direction_10m": "wind_from_deg",
}
# Values over the hour before their label.
PRECEDING = {
    "precipitation": "precipitation_mm",
    "precipitation_probability": "precipitation_chance_pct",
    "wind_gusts_10m": "gust_ms",
    "shortwave_radiation": "ghi_wm2",
    "direct_radiation": "direct_wm2",
    "diffuse_radiation": "diffuse_wm2",
    "global_tilted_irradiance": "gti_wm2",
}
OTHER = ("weather_code", "is_day")
# The past service has no probabilities.
PAST_LEAVES_OUT = ("precipitation_probability",)

# WMO weather codes -> the app's conditions.
WMO = {
    0: "clear",
    1: "mostly clear",
    2: "partly cloudy",
    3: "cloudy",
    45: "fog",
    48: "fog",
    51: "drizzle",
    53: "drizzle",
    55: "drizzle",
    56: "drizzle",
    57: "drizzle",
    61: "rain",
    63: "rain",
    65: "heavy rain",
    66: "sleet",
    67: "sleet",
    71: "snow",
    73: "snow",
    75: "snow",
    77: "snow",
    80: "showers",
    81: "showers",
    82: "heavy rain",
    85: "snow",
    86: "snow",
    95: "thunder",
    96: "thunder",
    99: "thunder",
}


def open_meteo_azimuth(facing: float | None) -> float:
    """The app's compass bearing (180 south) in Open-Meteo's terms: 0 south, -90 east, 90 west."""
    return (180.0 if facing is None else facing % 360) - 180


class OpenMeteo:
    name = "open-meteo"
    title = "Open-Meteo"
    credit = Credit("Weather data by Open-Meteo.com", "CC BY 4.0", "https://open-meteo.com/")
    has_irradiance = True
    has_past = True
    min_interval = HOUR  # Its models update hourly at most.
    forecast_days = 7

    def __init__(self, forecast_url: str = FORECAST_URL, past_url: str = PAST_URL) -> None:
        self.forecast_url = forecast_url
        self.past_url = past_url

    def _params(self, where: Location, past: bool) -> dict[str, Any]:
        hourly = [*INSTANT, *PRECEDING, *OTHER]
        if past:
            hourly = [name for name in hourly if name not in PAST_LEAVES_OUT]
        params: dict[str, Any] = {
            "latitude": round(where.latitude, 2),
            "longitude": round(where.longitude, 2),
            "timeformat": "unixtime",
            "timezone": "GMT",
            "wind_speed_unit": "ms",
        }
        if where.panel_tilt is None:
            hourly.remove("global_tilted_irradiance")
        else:
            params["tilt"] = where.panel_tilt
            params["azimuth"] = open_meteo_azimuth(where.panel_facing)
        params["hourly"] = ",".join(hourly)
        return params

    def _hours(self, url: str, where: Location, params: dict[str, Any], fetch: Fetch) -> list[Hour]:
        """Fetch and parse. With a second facing, one more request for the sunshine that way, and
        each hour's tilted sunshine becomes the mean of the two: half the panels face each way."""
        hours = hours_from(fetch(url, params))
        if where.panel_tilt is None or where.panel_facing_2 is None:
            return hours
        other = {**params, "hourly": "global_tilted_irradiance", "azimuth": open_meteo_azimuth(where.panel_facing_2)}
        second = {h.hour_start: h.gti_wm2 for h in hours_from(fetch(url, other))}
        return [replace(h, gti_wm2=_mean(h.gti_wm2, second.get(h.hour_start))) for h in hours]

    def forecast(self, where: Location, fetch: Fetch) -> list[Hour]:
        params = self._params(where, past=False)
        params["forecast_days"] = self.forecast_days
        return self._hours(self.forecast_url, where, params, fetch)

    def past(self, where: Location, start: int, end: int, fetch: Fetch) -> list[Hour]:
        """The hours that start from `start` up to `end`, as its past forecasts saw them."""
        params = self._params(where, past=True)
        params["start_date"] = _day(start)
        params["end_date"] = _day(end + HOUR)  # The hour ending at `end` is labelled the hour after.
        return [h for h in self._hours(self.past_url, where, params, fetch) if start <= h.hour_start < end]


def _mean(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else (a + b) / 2


def _day(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).date().isoformat()


def hours_from(answer: Any) -> list[Hour]:
    """Turn the answer's hourly arrays into Hours labelled by their start."""
    try:
        hourly = answer["hourly"]
        times = [int(t) for t in hourly["time"]]
    except (KeyError, TypeError, ValueError) as error:
        raise bad_answer("no hourly times") from error
    columns = {name: values for name, values in hourly.items() if isinstance(values, list) and len(values) == len(times)}
    index = {t: i for i, t in enumerate(times)}
    hours = []
    for i, start in enumerate(times):
        after = index.get(start + HOUR)
        if after is None:
            continue  # The last label, whose hour's rain and sunshine come after the end of the answer.
        values: dict[str, Any] = {}
        for theirs, ours in INSTANT.items():
            if theirs in columns:
                values[ours] = number(columns[theirs][i])
        for theirs, ours in PRECEDING.items():
            if theirs in columns:
                values[ours] = number(columns[theirs][after])
        code = number(columns["weather_code"][i]) if "weather_code" in columns else None
        values["condition"] = None if code is None else WMO.get(int(code))
        day = number(columns["is_day"][i]) if "is_day" in columns else None
        values["is_day"] = None if day is None else bool(day)
        hours.append(Hour(hour_start=start, **values))
    return hours
