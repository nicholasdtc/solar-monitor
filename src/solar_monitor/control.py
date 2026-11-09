"""The only code that writes to the inverter, and all it can change is one TOU period.

A change can set a period's times, days, months, cut-off, power and whether it's on. Never its type
or whether it charges or discharges: those are kept from what the inverter holds.

Protecting the equipment comes first, so every change goes the same way:

1. Read the period again, and stop if it isn't what the person was looking at.
2. Make the new bytes from exactly what was read, with only what was asked for different.
3. Decode them separately, and stop unless every field says what was intended.
4. Read the other periods, and stop if this one would newly overlap one that's on.
5. Log the attempt before sending it, so even a crash leaves a record and the old value for undo.
6. Send the whole period, which is how the library writes one. Not set_operation_mode, which
   overwrites the first period and disables the others.
7. Read it back and compare every byte. A difference is reported, and never retried by itself.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass

import goodwe
from goodwe.sensor import EcoModeV2

from solar_monitor import db
from solar_monitor.schedule import (
    ALL_MONTHS,
    EVERY_DAY,
    PERIOD_LAYOUT,
    TIME_PERIODS,
    days_meant,
    decode,
    describe_changes,
    describe_period,
    is_empty,
    months_meant,
    period_bytes,
    read_setting,
)

READ_BACK_TRIES = 4
READ_BACK_WAIT_SECONDS = 1  # The inverter may take a moment to show a change.
LOWEST_PERCENT, HIGHEST_PERCENT = 1, 100
# A new cut-off, either way. The inverter's own on-grid limit still stops a discharge before this.
LOWEST_CUTOFF, HIGHEST_CUTOFF = 10, 100
MINUTES_A_DAY = 24 * 60
MINUTES_A_WEEK = 7 * MINUTES_A_DAY


class Refused(Exception):
    """Nothing was sent to the inverter. The message is for the person who asked."""


class Unconfirmed(Exception):
    """Something was sent, and the read-back doesn't show it arrived. The message is for the person who asked."""


@dataclass(frozen=True)
class Wanted:
    """A TOU period as someone asked for it. Its type and direction aren't here, because they never change."""

    on: bool
    start: tuple[int, int]  # Hour and minute.
    end: tuple[int, int]  # The same as the start or earlier means the next day.
    day_bits: int  # Bit 0 is Sunday.
    month_bits: int  # Bit 0 is January.
    cutoff_soc: int
    percent: int


def _out_of_range(wanted: Wanted, held_cutoff: int) -> str | None:
    """What's wrong with the request, if anything. A cut-off that isn't being changed may stay as it is."""
    for hour, minute in (wanted.start, wanted.end):
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return "A time has to be from 00:00 to 23:59"
    if wanted.start == wanted.end:
        return "The start and end can't be the same"
    if not 1 <= wanted.day_bits <= EVERY_DAY:
        return "It has to run on at least one day"
    if not 1 <= wanted.month_bits <= ALL_MONTHS:
        return "It has to run in at least one month"
    if not LOWEST_PERCENT <= wanted.percent <= HIGHEST_PERCENT:
        return f"The power has to be from {LOWEST_PERCENT} to {HIGHEST_PERCENT}%"
    if wanted.cutoff_soc != held_cutoff and not LOWEST_CUTOFF <= wanted.cutoff_soc <= HIGHEST_CUTOFF:
        return f"The cut-off has to be from {LOWEST_CUTOFF} to {HIGHEST_CUTOFF}%"
    return None


def _build(group: EcoModeV2, wanted: Wanted) -> bytes:
    """The period's new bytes. Anything that means the same as before keeps the bytes it had."""
    kind = group.get_schedule_type()
    was_on = group.on_off < 0
    on_off = group.on_off if wanted.on == was_on else (-1 - kind.value if wanted.on else kind.value)
    day_bits = group.day_bits if days_meant(group.day_bits) == wanted.day_bits else wanted.day_bits
    month_bits = group.month_bits if months_meant(group.month_bits) == wanted.month_bits else wanted.month_bits
    if wanted.percent == abs(group.get_power()):
        power = group.power
    else:
        power = kind.encode_power(wanted.percent)  # In the units this type uses.
        power = -power if group.power < 0 else power
    return PERIOD_LAYOUT.pack(*wanted.start, *wanted.end, on_off, day_bits, power, wanted.cutoff_soc, month_bits)


def _double_check(old: bytes, new: bytes, wanted: Wanted) -> None:
    try:
        before, after = decode(old), decode(new)
        good = (
            len(new) == len(old)
            and after.get_schedule_type() == before.get_schedule_type()
            and before.power != 0
            and after.power != 0
            and (after.power < 0) == (before.power < 0)
            and (after.on_off < 0) == wanted.on
            and (after.start_h, after.start_m) == wanted.start
            and (after.end_h, after.end_m) == wanted.end
            and days_meant(after.day_bits) == wanted.day_bits
            and months_meant(after.month_bits) == wanted.month_bits
            and after.soc == wanted.cutoff_soc
            and abs(after.get_power()) == wanted.percent
            and not is_empty(after)
        )
    except ValueError:  # The library won't decode values outside what a period can hold.
        good = False
    if not good:
        raise Refused(f"The new value didn't check out ({old.hex()} to {new.hex()}), so nothing was sent.")


def _minutes(period: EcoModeV2) -> set[int]:
    """The minutes of the week a period covers. One that ends the next day belongs to the day it starts on."""
    begin = period.start_h * 60 + period.start_m
    finish = period.end_h * 60 + period.end_m
    if finish <= begin:
        finish += MINUTES_A_DAY
    days = days_meant(period.day_bits)
    return {
        (day * MINUTES_A_DAY + minute) % MINUTES_A_WEEK
        for day in range(7)
        if days & (1 << day)
        for minute in range(begin, finish)
    }


def _when(period: EcoModeV2) -> tuple[object, ...]:
    """Whether a period is on, and when it runs."""
    return (
        period.on_off < 0,
        period.start_h,
        period.start_m,
        period.end_h,
        period.end_m,
        days_meant(period.day_bits),
        months_meant(period.month_bits),
    )


def _overlaps(period: EcoModeV2, other: EcoModeV2) -> bool:
    shared_months = months_meant(period.month_bits) & months_meant(other.month_bits)
    return bool(shared_months) and bool(_minutes(period) & _minutes(other))


async def _check_overlaps(inverter: goodwe.Inverter, slot: int, old: bytes, new: bytes) -> None:
    """Refuse a change that makes this period overlap another that's on. One that already did may stay so.

    Which of two overlapping periods the inverter follows isn't known, so it's kept from happening.
    """
    before, after = decode(old), decode(new)
    if after.on_off >= 0 or _when(after) == _when(before):  # Off, or on at the same times as before.
        return
    for other_slot in range(1, TIME_PERIODS + 1):
        if other_slot == slot:
            continue
        group = await read_setting(inverter, f"eco_mode_{other_slot}")
        shown = describe_period(other_slot, group)
        if shown["state"] == "unreadable":
            raise Refused(f"Couldn't read programme {other_slot} to check this doesn't overlap it, so nothing was sent.")
        if shown["state"] != "on" or not isinstance(group, EcoModeV2):
            continue
        other = decode(period_bytes(group))  # A copy: the library reuses its object.
        if _overlaps(after, other) and not (before.on_off < 0 and _overlaps(before, other)):
            raise Refused(
                f"This would overlap programme {other_slot} ({shown['start']} to {shown['end']}), which is on, "
                "so nothing was sent."
            )


async def _read_back(inverter: goodwe.Inverter, slot: int, wanted: bytes) -> bytes | None:
    """What the period holds at the last look, or None if that look couldn't read it."""
    held = None
    for _ in range(READ_BACK_TRIES):
        await asyncio.sleep(READ_BACK_WAIT_SECONDS)
        group = await read_setting(inverter, f"eco_mode_{slot}")
        held = period_bytes(group) if isinstance(group, EcoModeV2) else None
        if held == wanted:
            break
    return held


async def send_period(inverter: goodwe.Inverter, conn: sqlite3.Connection, slot: int, old: bytes, new: bytes) -> None:
    """Log, send and read back. Raises Unconfirmed unless the inverter ends up holding `new`."""
    write_id = db.add_inverter_write(
        conn, slot, old.hex(), new.hex(), abs(decode(old).get_power()), abs(decode(new).get_power())
    )
    failure = ""
    try:
        await inverter.write_setting(f"eco_mode_{slot}", new)
    except Exception as error:  # Only the read-back can tell: a reply can be lost after the write arrived.
        failure = f" Sending it failed with {type(error).__name__}: {error}."

    held = await _read_back(inverter, slot, new)
    if held == new:
        db.finish_inverter_write(conn, write_id, "confirmed", held.hex())
        return
    if held is None:
        db.finish_inverter_write(conn, write_id, "unknown", None)
        raise Unconfirmed(
            f"Couldn't read the programme back, so it isn't known whether the change arrived.{failure} "
            "Check it in SEMS+ before relying on it."
        )
    if held == old:
        db.finish_inverter_write(conn, write_id, "not_applied", held.hex())
        raise Unconfirmed(f"The inverter still holds the old value, so nothing changed.{failure}")
    db.finish_inverter_write(conn, write_id, "mismatch", held.hex())
    raise Unconfirmed(
        f"The inverter holds something other than what was sent: it had {old.hex()}, was sent {new.hex()} "
        f"and now holds {held.hex()}.{failure} Check this programme in SEMS+ before relying on it."
    )


async def set_period(
    inverter: goodwe.Inverter, conn: sqlite3.Connection, slot: int, wanted: Wanted, expected: str
) -> dict[str, object]:
    """Change one period to what's wanted. `expected` is its bytes in hex, as the person saw them."""
    group = await read_setting(inverter, f"eco_mode_{slot}")
    state = describe_period(slot, group)["state"]
    if state == "unreadable":
        raise Refused("Couldn't read this programme from the inverter, so nothing was sent.")
    if state not in ("on", "off") or not isinstance(group, EcoModeV2):
        raise Refused("This one isn't a TOU programme that can be changed from here, so nothing was sent.")
    old = period_bytes(group)
    if old.hex() != expected:
        raise Refused(
            "This programme has changed on the inverter since the page showed it, so nothing was sent. "
            "Check it and try again."
        )
    if group.power == 0:
        raise Refused("This programme's power is 0, which doesn't say whether it charges or discharges, so nothing was sent.")
    problem = _out_of_range(wanted, group.soc)
    if problem:
        raise Refused(f"{problem}, so nothing was sent.")
    new = _build(group, wanted)  # Before any other read: the library reuses `group`.
    if new == old:
        return {"outcome": "unchanged", "changes": []}
    _double_check(old, new, wanted)
    await _check_overlaps(inverter, slot, old, new)
    await send_period(inverter, conn, slot, old, new)
    return {"outcome": "confirmed", "changes": describe_changes(old, new)}
