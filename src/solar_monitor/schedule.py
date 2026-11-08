"""The battery schedule as the inverter holds it. This only reads: writes are all in control.py."""

from __future__ import annotations

import struct
from datetime import datetime, timedelta

import goodwe
from goodwe.sensor import EcoModeV1, EcoModeV2, ScheduleType

TIME_PERIODS = 4  # What the library exposes. SEMS+ shows them in this same order.
ALL_DAYS = (127, -1)
ALL_MONTHS = 0x0FFF
TOU_TYPES = (ScheduleType.ECO_MODE, ScheduleType.ECO_MODE_745)

# A period's 12 bytes: start hour and minute, end hour and minute, on/off, days, power, cut-off SOC, months.
# All signed, as the library reads them, so packing what it read gives back the same bytes.
PERIOD_LAYOUT = struct.Struct(">bbbbbbhhh")

# Value 0 is what SEMS+ calls Self-consumption. The library calls it General.
WORK_MODES = {0: "Self-consumption", 1: "Off-grid", 2: "Backup", 3: "Eco", 4: "Peak shaving", 5: "Self-use"}


def _clock(hours: int, minutes: int) -> str:
    return f"{hours:02}:{minutes:02}"


def _is_empty(group) -> bool:
    # A slot that was never used is all zeros, a cleared one starts at hour 48 or -1, and 85 means not set.
    return group.day_bits == 0 or group.start_h in (48, -1) or group.on_off == ScheduleType.NOT_SET


def period_bytes(group: EcoModeV2) -> bytes:
    """The 12 bytes the inverter holds for this period."""
    return PERIOD_LAYOUT.pack(
        group.start_h, group.start_m, group.end_h, group.end_m,
        group.on_off, group.day_bits, group.power, group.soc, group.month_bits,
    )


def describe_period(slot: int, group) -> dict[str, object]:
    """A plain copy of one time period. It has to be a copy: the library reuses this object on every read."""
    if group is None:
        return {"slot": slot, "state": "unreadable"}
    if _is_empty(group):
        return {"slot": slot, "state": "empty"}
    if group.get_schedule_type() not in TOU_TYPES:
        return {"slot": slot, "state": "other"}
    power = group.get_power()  # Negative charges and positive discharges. Scaled to a percentage whatever the type.
    return {
        "slot": slot,
        "state": "on" if group.on_off < 0 else "off",
        "start": _clock(group.start_h, group.start_m),
        "end": _clock(group.end_h, group.end_m),
        "next_day": (group.end_h, group.end_m) <= (group.start_h, group.start_m),
        "action": "charge" if power < 0 else "discharge" if power > 0 else "hold",
        "power_pct": abs(power),
        "cutoff_soc": None if isinstance(group, EcoModeV1) else group.soc,  # The older format has no cut-off.
        "days": "Every day" if group.day_bits in ALL_DAYS else group.days.replace(",", ", "),
        "months": getattr(group, "months", None) or "All year",
        # The same as the inverter holds them, to tell whether the period is running at a given time.
        "day_bits": group.day_bits,
        "month_bits": getattr(group, "month_bits", 0),
        # What a change has to be based on. The older format can't be changed from here, so has none.
        "raw": period_bytes(group).hex() if isinstance(group, EcoModeV2) else None,
    }


def _runs_on(period: dict[str, object], day: datetime) -> bool:
    """Whether the period is set for this day. Bit 0 of the days is Sunday, and bit 0 of the months is January."""
    on_day = period["day_bits"] & (1 << ((day.weekday() + 1) % 7))  # -1, every day, has every bit set.
    months = period["month_bits"]
    return bool(on_day) and (months <= 0 or months == ALL_MONTHS or bool(months & (1 << (day.month - 1))))


def _is_running(period: dict[str, object], now: datetime) -> bool:
    """A period that ends the next day belongs to the day it started on."""
    clock = now.strftime("%H:%M")
    if not period["next_day"]:
        return period["start"] <= clock < period["end"] and _runs_on(period, now)
    if clock >= period["start"]:
        return _runs_on(period, now)
    return clock < period["end"] and _runs_on(period, now - timedelta(days=1))


def running_charge_period(schedule: dict[str, object] | None, now: datetime) -> dict[str, object] | None:
    """The charge period running now that has a cut-off, or None.

    The periods count when the TOU switch is on. Where that switch can't be read, as on some
    older firmware, they count in the Eco working mode, which is where they used to live.
    If two overlap, the first one in the inverter's order is taken.
    """
    if not schedule:
        return None
    if not (schedule.get("tou_on") or (schedule.get("tou_on") is None and schedule.get("work_mode") == "Eco")):
        return None
    for period in schedule.get("periods", []):
        if (
            period.get("state") == "on"
            and period.get("action") == "charge"
            and isinstance(period.get("cutoff_soc"), int)
            and 1 <= period["cutoff_soc"] <= 100
            and _is_running(period, now)
        ):
            return period
    return None


async def read_setting(inverter: goodwe.Inverter, setting: str):
    try:
        return await inverter.read_setting(setting)
    except Exception:  # One unreadable setting shows as unknown, and shouldn't cost the rest of the page.
        return None


async def _switch(inverter: goodwe.Inverter, setting: str) -> bool | None:
    value = await read_setting(inverter, setting)
    return None if value is None else bool(value)


async def read_schedule(inverter: goodwe.Inverter) -> dict[str, object]:
    """The working mode, the mode switches and the time periods. Anything unreadable comes back as None."""
    work_mode = await read_setting(inverter, "work_mode")
    return {
        "work_mode": None if work_mode is None else WORK_MODES.get(work_mode, f"Unknown ({work_mode})"),
        "tou_on": await _switch(inverter, "eco_mode_enable"),  # Separate from the working mode, as SEMS+ shows it.
        "backup_on": await _switch(inverter, "backup_mode_enable"),
        "smart_charge_on": await _switch(inverter, "smart_charging_enable"),
        "periods": [
            describe_period(slot, await read_setting(inverter, f"eco_mode_{slot}"))
            for slot in range(1, TIME_PERIODS + 1)
        ],
    }
