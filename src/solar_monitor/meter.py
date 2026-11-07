"""The electricity meter's half-hours, as imported from ESB Networks, shaped for the Grid meter tab.

They stay on the meter's own labels, in Irish clock time, so a day is always slots 0 to 47 from
midnight. The day the clocks go forward has no 01:00 or 01:30, and the day they go back has both
twice. Comparing with this app's own readings is done by whole days, which both count on the clock.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta

from solar_monitor import db

SLOTS = 48
# The half-hours at 01:00 and 01:30, which don't happen when the clocks go forward in spring,
# and happen twice when they go back in autumn.
REPEATED_SLOTS = (2, 3)
SKIPPED_SLOTS = REPEATED_SLOTS

# The usual hours of an Irish smart tariff, used until the household's own are entered.
DEFAULT_BANDS = {
    "band_night_start_minutes": 23 * 60,
    "band_night_end_minutes": 8 * 60,
    "band_peak_start_minutes": 17 * 60,
    "band_peak_end_minutes": 19 * 60,
}
TARIFFS = {"day": "tariff_day", "peak": "tariff_peak", "night": "tariff_night", "export": "tariff_export"}

MAX_GAPS = 50  # Listed one by one. Beyond this only the count goes to the page.
# A day of this app's readings only counts as whole if there are readings this close to the
# midnight it starts at and the one it ends at.
MIDNIGHT_SLACK_SECONDS = 600

# In the page's slots: a half-hour that didn't happen on the clock or isn't in the file's span yet.
NOT_A_HALF_HOUR = -1


def slot_of(when: datetime) -> int:
    return when.hour * 2 + when.minute // 30


def last_sunday(year: int, month: int) -> date:
    last = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() + 1) % 7)


def _label(day: str, slot: int) -> str:
    return f"{day} {slot // 2:02d}:{slot % 2 * 30:02d}"


def readings(conn: sqlite3.Connection) -> dict[str, object]:
    """Every day from the first reading to the last, and what the imported files covered.

    Each day is [day, flags, slots, twice]. slots has the 48 half-hours' average draw in whole
    watts, None where the file has a gap, and NOT_A_HALF_HOUR where there's nothing to have.
    twice is [slot, earlier W, later W] for each half-hour that came twice, whose slot holds their
    average. flags has "s" for the day the clocks go forward, "a" for one with half-hours twice,
    "b" if the readings start partway through and "p" if they stop partway through.
    """
    held: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    count = zeros = 0
    # Occurrence 1 is the earlier of a repeated half-hour, so descending puts them in time order.
    for day, slot, _, kw in conn.execute(
        "SELECT day, slot, occurrence, kw FROM meter_readings ORDER BY day, slot, occurrence DESC"
    ):
        held[day][slot].append(kw)
        count += 1
        zeros += kw == 0
    if not held:
        return {"days": [], "file": None}

    first_day, last_day = min(held), max(held)
    first_slot, last_slot = min(held[first_day]), max(held[last_day])
    days = []
    gaps: list[list[object]] = []
    gap_count = missing = spring_days = doubled_days = 0
    run_start: str | None = None
    run_length = 0
    when = date.fromisoformat(first_day)
    while (day := when.isoformat()) <= last_day:
        flags = ""
        spring = when == last_sunday(when.year, 3)
        if spring:
            flags += "s"
            spring_days += 1
        if day == first_day and first_slot > 0:
            flags += "b"
        if day == last_day and last_slot < SLOTS - 1:
            flags += "p"
        slots: list[int | None] = []
        twice = []
        for slot in range(SLOTS):
            values = held.get(day, {}).get(slot)
            outside = (day == first_day and slot < first_slot) or (day == last_day and slot > last_slot)
            if values:
                slots.append(round(sum(values) / len(values) * 1000))
                if len(values) > 1:
                    twice.append([slot, *(round(kw * 1000) for kw in values)])
            elif outside or (spring and slot in SKIPPED_SLOTS):
                slots.append(NOT_A_HALF_HOUR)
            else:
                slots.append(None)
            if slots[-1] is None:
                missing += 1
                if run_length == 0:
                    run_start = _label(day, slot)
                run_length += 1
            elif slots[-1] != NOT_A_HALF_HOUR and run_length:
                gap_count += 1
                if len(gaps) < MAX_GAPS:
                    gaps.append([run_start, run_length])
                run_length = 0
        if twice:
            flags += "a"
            doubled_days += 1
        days.append([day, flags, slots, twice])
        when += timedelta(days=1)

    return {
        "days": days,
        "file": {
            "first": _label(first_day, first_slot),
            "last": _label(last_day, last_slot),
            "readings": count,
            "zeros": zeros,
            "missing": missing,
            "springDays": spring_days,
            "doubledDays": doubled_days,
            "gaps": gaps,
            "gapCount": gap_count,
        },
    }


def tariff(settings: dict[str, float | None]) -> dict[str, object]:
    """The rates entered, and the band hours, the household's own or else the usual ones."""
    entered = {key: settings.get(key) for key in DEFAULT_BANDS}
    return {
        "rates": {name: settings.get(key) for name, key in TARIFFS.items()},
        "bands": {
            key.removeprefix("band_").removesuffix("_minutes"): int(value) if value is not None else DEFAULT_BANDS[key]
            for key, value in entered.items()
        },
        "bandsSet": any(value is not None for value in entered.values()),
    }


def _near_midnight(conn: sqlite3.Connection, midnight: datetime, cache: dict[datetime, bool]) -> bool:
    if midnight not in cache:
        end = int(midnight.timestamp())
        cache[midnight] = (
            conn.execute(
                "SELECT 1 FROM samples WHERE ts BETWEEN ? AND ? LIMIT 1", (end - MIDNIGHT_SLACK_SECONDS, end)
            ).fetchone()
            is not None
        )
    return cache[midnight]


def app_days(conn: sqlite3.Connection, today: date | None = None) -> dict[str, object]:
    """This app's own record of the grid: the span of its readings, and each whole day's import in kWh.

    A day's import runs from the previous day's latest reading to its own, so it's only a whole
    day when both of those were taken just before midnight. Gaps during the day don't matter,
    because the counter is a lifetime one and catches up.
    """
    first_ts, last_ts = conn.execute("SELECT (SELECT MIN(ts) FROM samples), (SELECT MAX(ts) FROM samples)").fetchone()
    today = today or date.today()
    imports = {day: kwh for day, counter, kwh in db.daily_totals(conn) if counter == "grid_import"}
    near: dict[datetime, bool] = {}
    whole = []
    for day, kwh in imports.items():
        when = date.fromisoformat(day)
        start = datetime.combine(when, datetime.min.time())
        if (
            when < today
            and (when - timedelta(days=1)).isoformat() in imports
            and _near_midnight(conn, start, near)
            and _near_midnight(conn, start + timedelta(days=1), near)
        ):
            whole.append([day, kwh])

    def local(ts: int | None) -> str | None:
        return None if ts is None else datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

    return {"first": local(first_ts), "last": local(last_ts), "days": whole}
