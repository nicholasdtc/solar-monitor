"""Fetches the forecast and warnings for the saved location, on their own schedule.

Only the providers' calls run on a worker thread, with the fetch's short timeouts, so a slow
service never holds up the collector. Everything that touches the database stays on the event
loop's thread, which owns the one connection.

Each kind is fetched as often as its provider allows, at most hourly for forecasts, at a minute
picked at random once per run of the app so that copies of it don't all ask at once. A change of
location, panels, provider or region fetches at once. Errors back off, doubling up to an hour, and
HTTP 429 waits for the next hour.

With a provider that has a record of past weather, weather_hours is filled from it for every hour
the app has a log for: a month at a time, newest first, then once a night for the day before.

The expected solar output is refitted to the log once a night, and at once when the panels or their
rating change, and each forecast saved keeps what it expects for tomorrow (see solar_forecast.py).
"""

from __future__ import annotations

import asyncio
import math
import random
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from solar_monitor import db, solar_forecast, weather
from solar_monitor.collector import log
from solar_monitor.weather.base import HOUR, Fetch, FetchError, Location, NotModified, RateLimited

CHECK_SECONDS = 60  # How soon a change of settings is noticed.
FIRST_BACKOFF = 5 * 60
MAX_BACKOFF = HOUR
DAY = 24 * HOUR
PAST_CHUNK_DAYS = 31  # Days of past weather asked for at once.
PAST_PAUSE = 10  # Seconds between those asks.
NIGHT_HOUR = 3  # UTC: the day before is filled in after this, plus this run's minute.


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
    past: Job = field(default_factory=Job)
    solar: Job = field(default_factory=Job)  # refitting the expected output
    # Hours of past weather already asked for since the last night, so ones the provider doesn't
    # have aren't asked for again and again.
    past_tried: set[int] = field(default_factory=set)


def location(settings: dict[str, object]) -> Location | None:
    """The saved location and panels, or None until both coordinates are set."""
    lat, lon = settings.get("latitude"), settings.get("longitude")
    if lat is None or lon is None:
        return None
    tilt, facing = settings.get("panel_tilt_deg"), settings.get("panel_azimuth_deg")
    return Location(
        float(lat),
        float(lon),
        panel_tilt=tilt,  # type: ignore[arg-type]
        panel_facing=facing,  # type: ignore[arg-type]
        panel_facing_2=settings.get("panel_azimuth_2_deg"),  # type: ignore[arg-type]
    )


def panels_of(where: Location) -> str | None:
    """What the sunshine on the panels is for, such as "30/180" or "30/90/270", or None without a tilt."""
    if where.panel_tilt is None:
        return None
    facings = [where.panel_facing] + ([] if where.panel_facing_2 is None else [where.panel_facing_2])
    return "/".join([f"{float(where.panel_tilt):g}", *("-" if f is None else f"{float(f):g}" for f in facings)])


def next_night(now: float, minute: int) -> float:
    """The next NIGHT_HOUR UTC, at this run's minute."""
    offset = NIGHT_HOUR * HOUR + minute * 60
    return (math.floor((now - offset) / DAY) + 1) * DAY + offset


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
) -> Exception | None:
    """Run one fetch on a worker thread, note how it went, and set when it's next due. Returns the
    error, if it failed."""
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
        return error
    except Exception as error:  # A provider's bug mustn't stop the loop, so it counts as an error too.
        message = str(error) if isinstance(error, FetchError) else f"{type(error).__name__}: {error}"
        db.add_weather_fetch(conn, at, provider_name, kind, "error", message)
        if job.failures == 0:
            log(f"Weather {kind} from {provider_name} failing, will keep trying: {message}")
        job.failures += 1
        job.due = now + _backoff(job.failures)
        return error
    else:
        save(at, found)
    if job.failures:
        log(f"Weather {kind} from {provider_name} recovered")
    job.failures = 0
    job.due = next_slot(now, interval, minute)
    return None


def _is_due(job: Job, key: tuple[object, ...], now: float) -> bool:
    """A change of what it's fetched for makes it due at once, and forgets earlier failures."""
    if key != job.key:
        job.key, job.due, job.failures = key, now, 0
    return now >= job.due


def _past_chunk(missing: list[int], tried: set[int], first: int, end: int) -> tuple[int, int] | None:
    """The next days to ask for: up to a month ending with the newest day still missing an hour,
    from the oldest day missing one within that month."""
    untried = [hour for hour in missing if hour not in tried]
    if not untried:
        return None
    chunk_end = min(untried[-1] // DAY * DAY + DAY, end)
    earliest = max(chunk_end - PAST_CHUNK_DAYS * DAY, first)
    return max(next(hour for hour in untried if hour >= earliest) // DAY * DAY, first), chunk_end


async def _fill_past(conn: sqlite3.Connection, now: float, fetch: Fetch, state: State, where: Location, name: str) -> float:
    """Ask for the next month of past weather that's missing, or wait for the night if none is."""
    job, provider = state.past, weather.FORECASTS[name]
    key = (where, name)
    if key != job.key:
        state.past_tried.clear()
    if not _is_due(job, key, now):
        return job.due
    first, end = db.log_start(conn), int(now) // DAY * DAY  # Only whole days that are over.
    panels = panels_of(where)
    chunk = None
    if first is not None and first < end:
        chunk = _past_chunk(db.weather_hours_missing(conn, first, end, panels), state.past_tried, first, end)
    if chunk is None:
        state.past_tried.clear()  # So tonight's look tries any gaps again.
        job.due = next_night(now, state.minute)
        return job.due
    start, stop = chunk
    error = await _attempt(
        conn,
        now,
        job,
        "past",
        name,
        DAY,
        state.minute,
        lambda: provider.past(where, start, stop, fetch),
        lambda at, hours: db.save_past(conn, at, name, hours, panels),
    )
    if error is None:
        state.past_tried.update(range(start, stop, HOUR))
        job.due = now + PAST_PAUSE
    elif isinstance(error, FetchError) and not isinstance(error, RateLimited) and 400 <= (error.status or 0) < 500:
        # It won't give these days. Older ones are most likely from before its records begin, so
        # none earlier are asked for either; the newest are most likely too recent, so only those.
        state.past_tried.update(range(start if stop == end else first, stop, HOUR))
        job.failures = 0
        job.due = now + PAST_PAUSE
    return job.due


def _refit_solar(conn: sqlite3.Connection, now: float, state: State, settings: dict[str, object], where: Location) -> float:
    """Refit the expected output if it's due, and say when it's next due."""
    panels = panels_of(where)
    key = (panels, settings.get("pv_capacity_kwp"))
    job = state.solar
    if _is_due(job, key, now):  # Due at the start too.
        try:
            solar_forecast.refit(conn, int(now), panels)
            job.failures = 0
        except sqlite3.Error as error:
            log(f"Solar forecast fit failed, will try again: {error}")
            job.failures += 1
            job.due = now + _backoff(job.failures)
            return job.due
        job.due = next_night(now, state.minute)
    return job.due


def _save_forecast(conn: sqlite3.Connection, at: int, name: str, hours: list[object], panels: str | None) -> None:
    fetch_id = db.save_forecast(conn, at, name, hours, panels)
    solar_forecast.record_day_ahead(conn, fetch_id, at, hours, solar_forecast.load_fit(conn))


async def run_once(conn: sqlite3.Connection, now: float, fetch: Fetch, state: State) -> float:
    """Fetch whatever is due, and return when to look again."""
    settings = db.get_settings(conn)
    where = location(settings)
    if where is None:  # Nothing is fetched until there's a location.
        state.forecast.key = state.warnings.key = state.past.key = state.solar.key = None
        return now + CHECK_SECONDS
    wakes = [now + CHECK_SECONDS, _refit_solar(conn, now, state, settings, where)]

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
                lambda at, hours: _save_forecast(conn, at, name, hours, panels_of(where)),
            )
        wakes.append(state.forecast.due)
        if forecaster.has_past:
            wakes.append(await _fill_past(conn, now, fetch, state, where, name))

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
