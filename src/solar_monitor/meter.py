"""The electricity meter's half-hours, as imported from ESB Networks, shaped for the Grid meter tab.

They're kept on the meter's own labels, in Irish clock time. Each label is when its half-hour
ends, so the one at 00:00 ends the day before. The page shows each half-hour from its start
instead, so a day is slots 0 to 47 from midnight, and a rate band takes the half-hours that start
in it. The day the clocks go forward has no 01:00 or 01:30, and the day they go back has both
twice, as labels and as starts. Comparing with this app's own readings is done by whole days,
which both count on the clock.
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


def _in_time_order(when: date) -> list[tuple[int, int]]:
    """A day's half-hours in time order, as (slot, occurrence): the labels, and the starts too.

    Occurrence 1 is the earlier of a half-hour that comes twice.
    """
    if when == last_sunday(when.year, 3):
        return [(slot, 0) for slot in range(SLOTS) if slot not in SKIPPED_SLOTS]
    order = [(slot, 0) for slot in range(SLOTS)]
    if when == last_sunday(when.year, 10):
        order[REPEATED_SLOTS[0]:REPEATED_SLOTS[0]] = [(slot, 1) for slot in REPEATED_SLOTS]
    return order


def _by_start(labelled: dict[str, dict[tuple[int, int], float]]) -> dict[str, dict[int, list[float]]]:
    """The readings by the day and slot each half-hour starts in, from the labels where they end.

    A half-hour starts where the one before it in the day's time order ends, and the first of a day
    starts at 23:30 the day before. A slot that comes twice has the earlier reading first.
    """
    held: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    for day, readings_here in labelled.items():
        when = date.fromisoformat(day)
        before = (when - timedelta(days=1)).isoformat()
        order = _in_time_order(when)
        starts = [(before, SLOTS - 1), *((day, slot) for slot, _ in order[:-1])]
        left = dict(readings_here)
        for (start_day, start_slot), label in zip(starts, order):
            if label in left:
                held[start_day][start_slot].append(left.pop(label))
        # A label the clock doesn't have, like 01:00 the day the clocks go forward: half an hour back.
        for (slot, _), kw in left.items():
            start_day, start_slot = (day, slot - 1) if slot else (before, SLOTS - 1)
            held[start_day][start_slot].append(kw)
    return held


def readings(conn: sqlite3.Connection) -> dict[str, object]:
    """Every day from the first reading to the last, and what the imported files covered.

    Each day is [day, flags, slots, twice], by when the half-hours start. slots has the 48
    half-hours' average draw in whole watts, None where the file has a gap, and NOT_A_HALF_HOUR
    where there's nothing to have. twice is [slot, earlier W, later W] for each half-hour that came
    twice, whose slot holds their average. flags has "s" for the day the clocks go forward, "a" for
    one with half-hours twice, "b" if the readings start partway through and "p" if they stop
    partway through.
    """
    labelled: dict[str, dict[tuple[int, int], float]] = defaultdict(dict)
    count = zeros = 0
    for day, slot, occurrence, kw in conn.execute("SELECT day, slot, occurrence, kw FROM meter_readings"):
        labelled[day][(slot, occurrence)] = kw
        count += 1
        zeros += kw == 0
    if not labelled:
        return {"days": [], "file": None, "export": None}
    held = _by_start(labelled)

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
        "export": exports(conn),
    }


def exports(conn: sqlite3.Connection) -> dict[str, object] | None:
    """What went out to the grid, which ESB Networks has from November 2026, or None before any.

    days has [slots, twice] for each day from the first export half-hour to the last, by when they
    start, as in readings(). Outside that span a slot is NOT_A_HALF_HOUR, so the half-hours from
    before the meter recorded export aren't taken for gaps.
    """
    labelled: dict[str, dict[tuple[int, int], float]] = defaultdict(dict)
    count = 0
    for day, slot, occurrence, kw in conn.execute("SELECT day, slot, occurrence, kw FROM meter_exports"):
        labelled[day][(slot, occurrence)] = kw
        count += 1
    if not labelled:
        return None
    held = _by_start(labelled)
    first_day, last_day = min(held), max(held)
    first_slot, last_slot = min(held[first_day]), max(held[last_day])
    days: dict[str, list[object]] = {}
    when = date.fromisoformat(first_day)
    while (day := when.isoformat()) <= last_day:
        spring = when == last_sunday(when.year, 3)
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
        days[day] = [slots, twice]
        when += timedelta(days=1)
    return {"days": days, "first": _label(first_day, first_slot), "last": _label(last_day, last_slot), "readings": count}


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
