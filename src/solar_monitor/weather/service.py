"""Fetches the forecast and warnings for the saved location, on their own schedule.

Only the providers' calls run on a worker thread, with the fetch's short timeouts, so a slow
service never holds up the collector. Everything that touches the database stays on the event
loop's thread, which owns the one connection.

Each kind is fetched as often as its provider allows, at most hourly for forecasts, at a minute
picked at random once per run of the app so that copies of it don't all ask at once. A change of
location, panels, provider or region fetches at once. Errors back off, doubling up to an hour, and
HTTP 429 waits for the next hour.
"""

from __future__ import annotations

import asyncio
import math
import random
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from solar_monitor import db, weather
from solar_monitor.collector import log
from solar_monitor.weather.base import HOUR, Fetch, FetchError, Location, NotModified, RateLimited

CHECK_SECONDS = 60  # How soon a change of settings is noticed.
FIRST_BACKOFF = 5 * 60
MAX_BACKOFF = HOUR


@dataclass
class Job:
    """One kind of fetch: when it's next due, how many have failed in a row, and what it was for."""

    due: float = 0.0
    failures: int = 0
    key: tuple[object, ...] | None = None


@dataclass
class State:
    minute: int = field(default_factory=lambda: random.randrange(60))
    forecast: Job = field(default_factory=Job)
    warnings: Job = field(default_factory=Job)


def location(settings: dict[str, object]) -> Location | None:
    """The saved location and panels, or None until both coordinates are set."""
    lat, lon = settings.get("latitude"), settings.get("longitude")
    if lat is None or lon is None:
        return None
    tilt, facing = settings.get("panel_tilt_deg"), settings.get("panel_azimuth_deg")
    return Location(float(lat), float(lon), panel_tilt=tilt, panel_facing=facing)  # type: ignore[arg-type]


def next_slot(now: float, interval: int, minute: int) -> float:
    """The first time at least `interval` from now that falls on this run's minute, as near as the
    interval allows: for an hour, that minute past the hour."""
    offset = (minute * 60) % interval
    return math.ceil((now + interval - offset) / interval) * interval + offset


def next_hour(now: float) -> float:
    return (math.floor(now / HOUR) + 1) * HOUR


def _backoff(failures: int) -> int:
    return min(FIRST_BACKOFF * 2 ** (failures - 1), MAX_BACKOFF)


async def _attempt(
    conn: sqlite3.Connection,
    now: float,
    job: Job,
    kind: str,
    provider_name: str,
    interval: int,
    minute: int,
    call: Callable[[], list[object]],
    save: Callable[[int, list[object]], None],
) -> None:
    """Run one fetch on a worker thread, note how it went, and set when it's next due."""
    at = int(now)
    try:
        found = await asyncio.to_thread(call)
    except NotModified:
        db.add_weather_fetch(conn, at, provider_name, kind, "not modified")
    except RateLimited as error:
        db.add_weather_fetch(conn, at, provider_name, kind, "rate limited", str(error))
        if job.failures == 0:
            log(f"Weather {kind} from {provider_name} rate limited, waiting for the next hour")
        job.failures += 1
        job.due = next_hour(now)
        return
    except Exception as error:  # A provider's bug mustn't stop the loop, so it counts as an error too.
        message = str(error) if isinstance(error, FetchError) else f"{type(error).__name__}: {error}"
        db.add_weather_fetch(conn, at, provider_name, kind, "error", message)
        if job.failures == 0:
            log(f"Weather {kind} from {provider_name} failing, will keep trying: {message}")
        job.failures += 1
        job.due = now + _backoff(job.failures)
        return
    else:
        save(at, found)
    if job.failures:
        log(f"Weather {kind} from {provider_name} recovered")
    job.failures = 0
    job.due = next_slot(now, interval, minute)


def _is_due(job: Job, key: tuple[object, ...], now: float) -> bool:
    """A change of what it's fetched for makes it due at once, and forgets earlier failures."""
    if key != job.key:
        job.key, job.due, job.failures = key, now, 0
    return now >= job.due


async def run_once(conn: sqlite3.Connection, now: float, fetch: Fetch, state: State) -> float:
    """Fetch whatever is due, and return when to look again."""
    settings = db.get_settings(conn)
    where = location(settings)
    if where is None:  # Nothing is fetched until there's a location.
        state.forecast.key = state.warnings.key = None
        return now + CHECK_SECONDS
    wakes = [now + CHECK_SECONDS]

    name = str(settings.get("forecast_provider") or weather.DEFAULT_FORECAST)
    forecaster = weather.FORECASTS.get(name)
    if forecaster is not None:
        if _is_due(state.forecast, (where, name), now):
            await _attempt(
                conn,
                now,
                state.forecast,
                "forecast",
                name,
                forecaster.min_interval,
                state.minute,
                lambda: forecaster.forecast(where, fetch),
                lambda at, hours: db.save_forecast(conn, at, name, hours),
            )
        wakes.append(state.forecast.due)

    warner_name = settings.get("warnings_provider")
    warner = weather.WARNINGS.get(str(warner_name)) if warner_name else None
    if warner is not None:
        region = settings.get("warnings_region")
        # A region another provider uses, kept from before a change of provider, means all of them.
        if region not in {r.code for r in warner.regions}:
            region = None
        if _is_due(state.warnings, (where, warner.name, region), now):
            await _attempt(
                conn,
                now,
                state.warnings,
                "warnings",
                warner.name,
                warner.min_interval,
                state.minute,
                lambda: warner.warnings(where, region, fetch),  # type: ignore[arg-type]
                lambda at, found: db.save_warnings(conn, at, warner.name, found),
            )
        wakes.append(state.warnings.due)

    return min(wakes)


async def run(conn: sqlite3.Connection, fetch: Fetch, clock: Callable[[], float] = time.time) -> None:
    """Fetch for as long as the app runs. Cancelled on shutdown."""
    state = State()
    while True:
        now = clock()
        try:
            wake = await run_once(conn, now, fetch, state)
        except Exception as error:  # Such as the database being busy: try again shortly.
            log(f"Weather fetch loop error: {type(error).__name__}: {error}")
            wake = now + CHECK_SECONDS
        await asyncio.sleep(max(1.0, min(wake, now + CHECK_SECONDS) - clock()))
