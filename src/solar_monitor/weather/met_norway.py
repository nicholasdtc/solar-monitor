"""MET Norway's Locationforecast 2.0, the forecast behind Yr: free, no key, worldwide.

https://api.met.no/weatherapi/locationforecast/2.0/documentation. It has no solar radiation and no
past data. Its terms ask for a User-Agent that names the app, at most 4 decimals in coordinates,
and no new request before the Expires it sent, which the app's fetch looks after.

Each entry's time is the start of what it describes: the instant values are at that time, and
next_1_hours covers the hour after. After about two and a half days the entries are 6 hours apart,
with only next_6_hours, so those hours have a condition but no rain amount.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from solar_monitor.weather.base import HOUR, Credit, Fetch, Hour, Location, bad_answer, number

URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"

INSTANT = {
    "air_temperature": "temperature_c",
    "relative_humidity": "humidity_pct",
    "air_pressure_at_sea_level": "pressure_hpa",
    "cloud_area_fraction": "cloud_pct",
    "wind_speed": "wind_ms",
    "wind_from_direction": "wind_from_deg",
}


def condition(symbol: object) -> str | None:
    """Its symbol_code, such as partlycloudy_day or lightrainshowersandthunder_night, as a condition."""
    if not isinstance(symbol, str):
        return None
    code = symbol.split("_")[0]
    if "thunder" in code:
        return "thunder"
    if "sleet" in code:
        return "sleet"
    if "snow" in code:
        return "snow"
    if "showers" in code:
        return "showers"
    if code == "heavyrain":
        return "heavy rain"
    if code.endswith("rain"):
        return "rain"
    return {
        "clearsky": "clear",
        "fair": "mostly clear",
        "partlycloudy": "partly cloudy",
        "cloudy": "cloudy",
        "fog": "fog",
    }.get(code)


def _daylight(symbol: object) -> bool | None:
    if isinstance(symbol, str) and "_" in symbol:
        return symbol.endswith("_day")
    return None


class MetNorway:
    name = "met-norway"
    title = "MET Norway (Yr)"
    credit = Credit(
        "Weather data from MET Norway", "CC BY 4.0", "https://api.met.no/doc/License"
    )
    has_irradiance = False
    has_past = False
    min_interval = HOUR

    def __init__(self, url: str = URL) -> None:
        self.url = url

    def forecast(self, where: Location, fetch: Fetch) -> list[Hour]:
        params = {"lat": round(where.latitude, 4), "lon": round(where.longitude, 4)}
        return hours_from(fetch(self.url, params))

    def past(self, where: Location, start: int, end: int, fetch: Fetch) -> list[Hour]:
        return []


def hours_from(answer: Any) -> list[Hour]:
    try:
        series = answer["properties"]["timeseries"]
    except (KeyError, TypeError) as error:
        raise bad_answer("no timeseries") from error
    hours = []
    for entry in series:
        try:
            start = int(datetime.fromisoformat(entry["time"].replace("Z", "+00:00")).timestamp())
            data = entry["data"]
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if start % HOUR:
            continue
        details = (data.get("instant") or {}).get("details") or {}
        values: dict[str, Any] = {ours: number(details.get(theirs)) for theirs, ours in INSTANT.items()}
        next_hour = data.get("next_1_hours") or {}
        summary = (next_hour or data.get("next_6_hours") or {}).get("summary") or {}
        symbol = summary.get("symbol_code")
        values["condition"] = condition(symbol)
        values["is_day"] = _daylight(symbol)
        values["precipitation_mm"] = number((next_hour.get("details") or {}).get("precipitation_amount"))
        hours.append(Hour(hour_start=start, **values))
    return hours
