"""The only code that writes to the inverter, and all it can change is the power of one TOU period.

Protecting the equipment comes first, so every change goes the same way:

1. Read the period again, and stop if it isn't what the person was looking at.
2. Make the new bytes from exactly what was read, with only the power different and its direction kept.
3. Decode them separately, and stop unless they say what was intended.
4. Log the attempt before sending it, so even a crash leaves a record and the old value for undo.
5. Send the whole period, which is how the library writes one. Not set_operation_mode, which
   overwrites the first period and disables the others.
6. Read it back and compare every byte. A difference is reported, and never retried by itself.
"""

from __future__ import annotations

import asyncio
import sqlite3

import goodwe
from goodwe.protocol import ProtocolResponse
from goodwe.sensor import EcoModeV2

from solar_monitor import db
from solar_monitor.schedule import PERIOD_LAYOUT, describe_period, period_bytes, read_setting

READ_BACK_TRIES = 4
READ_BACK_WAIT_SECONDS = 1  # The inverter may take a moment to show a change.
POWER_START, POWER_END = 6, 8  # Where the power sits in a period's bytes.
LOWEST_PERCENT, HIGHEST_PERCENT = 1, 100


class Refused(Exception):
    """Nothing was sent to the inverter. The message is for the person who asked."""


class Unconfirmed(Exception):
    """Something was sent, and the read-back doesn't show it arrived. The message is for the person who asked."""


def _decode(data: bytes) -> EcoModeV2:
    """A new object each time, because the library reuses its own for every read."""
    return EcoModeV2("check", 0, "").read_value(ProtocolResponse(data, None))


def _with_power(group: EcoModeV2, percent: int) -> bytes:
    """The period's bytes with a new power, in the units its type uses and in the same direction."""
    power = group.get_schedule_type().encode_power(percent)
    return PERIOD_LAYOUT.pack(
        group.start_h, group.start_m, group.end_h, group.end_m,
        group.on_off, group.day_bits, -power if group.power < 0 else power, group.soc, group.month_bits,
    )


def _double_check(old: bytes, new: bytes, percent: int) -> None:
    try:
        before, after = _decode(old), _decode(new)
        good = (
            len(new) == len(old)
            and new[:POWER_START] == old[:POWER_START]
            and new[POWER_END:] == old[POWER_END:]
            and (after.power < 0) == (before.power < 0)
            and abs(after.get_power()) == percent
            and after.get_schedule_type() == before.get_schedule_type()
        )
    except ValueError:  # The library won't decode values outside what a period can hold.
        good = False
    if not good:
        raise Refused(f"The new value didn't check out ({old.hex()} to {new.hex()}), so nothing was sent.")


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
        conn, slot, old.hex(), new.hex(), abs(_decode(old).get_power()), abs(_decode(new).get_power())
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


async def set_period_power(
    inverter: goodwe.Inverter, conn: sqlite3.Connection, slot: int, percent: int, expected: str
) -> dict[str, object]:
    """Change one period's power. `expected` is its bytes in hex, as the person saw them."""
    if not LOWEST_PERCENT <= percent <= HIGHEST_PERCENT:
        raise Refused(f"The power has to be from {LOWEST_PERCENT} to {HIGHEST_PERCENT}%, so nothing was sent.")
    group = await read_setting(inverter, f"eco_mode_{slot}")
    state = describe_period(slot, group)["state"]
    if state == "unreadable":
        raise Refused("Couldn't read this programme from the inverter, so nothing was sent.")
    if state not in ("on", "off") or not isinstance(group, EcoModeV2):
        raise Refused("This one isn't a TOU programme that can be changed from here, so nothing was sent.")
    old = period_bytes(group)
    if old.hex() != expected:
        raise Refused("This programme has changed on the inverter since the page showed it, so nothing was sent. Check it and try again.")
    if group.power == 0:
        raise Refused("This programme's power is 0, which doesn't say whether it charges or discharges, so nothing was sent.")
    new = _with_power(group, percent)
    old_percent = abs(group.get_power())  # Before sending: the library reuses `group`.
    if new == old:
        return {"outcome": "unchanged", "old_percent": old_percent, "new_percent": percent}
    _double_check(old, new, percent)
    await send_period(inverter, conn, slot, old, new)
    return {"outcome": "confirmed", "old_percent": old_percent, "new_percent": percent}
