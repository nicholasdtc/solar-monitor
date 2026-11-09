"""The battery schedule as the inverter holds it. This only reads: writes are all in control.py."""

from __future__ import annotations

import struct
from datetime import datetime, timedelta

import goodwe
from goodwe.protocol import ProtocolResponse
from goodwe.sensor import EcoModeV1, EcoModeV2, ScheduleType

TIME_PERIODS = 4  # What the library exposes. SEMS+ shows them in this same order.
ALL_DAYS = (127, -1)
EVERY_DAY = 0x7F  # Bit 0 is Sunday.
ALL_MONTHS = 0x0FFF  # Bit 0 is January.
TOU_TYPES = (ScheduleType.ECO_MODE, ScheduleType.ECO_MODE_745)
# A new cut-off, either way. The inverter's own on-grid limit still stops a discharge before this.
LOWEST_CUTOFF, HIGHEST_CUTOFF = 10, 100
# Each name by its bit, in the order a change lists them: the week from Monday.
DAY_NAMES = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 0: "Sun"}
MONTH_NAMES = dict(enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]))

# A period's 12 bytes: start hour and minute, end hour and minute, on/off, days, power, cut-off SOC, months.
# All signed, as the library reads them, so packing what it read gives back the same bytes.
PERIOD_LAYOUT = struct.Struct(">bbbbbbhhh")

# Value 0 is what SEMS+ calls Self-consumption. The library calls it General.
WORK_MODES = {0: "Self-consumption", 1: "Off-grid", 2: "Backup", 3: "Eco", 4: "Peak shaving", 5: "Self-use"}


def _clock(hours: int, minutes: int) -> str:
    return f"{hours:02}:{minutes:02}"


def is_empty(group) -> bool:
    # A slot that was never used is all zeros, a cleared one starts at hour 48 or -1, and 85 means not set.
    return group.day_bits == 0 or group.start_h in (48, -1) or group.on_off == ScheduleType.NOT_SET


def days_meant(day_bits: int) -> int:
    """The days a period runs on. Every day can be held as 127 or -1, and this gives 127 for both."""
    return day_bits & EVERY_DAY


def months_meant(month_bits: int) -> int:
    """The months a period runs in. The library reads 0 and below as all year, as well as 0x0fff."""
    return ALL_MONTHS if month_bits <= 0 else month_bits & ALL_MONTHS


def decode(data: bytes) -> EcoModeV2:
    """A new object each time, because the library reuses its own for every read. Raises ValueError for nonsense."""
    return EcoModeV2("check", 0, "").read_value(ProtocolResponse(data, None))


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
    if is_empty(group):
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


def _named(bits: int, names: dict[int, str], every: int, all_text: str) -> str:
    """Three or more in a row as a range: "Mon–Fri", "Jan–May, Sep–Dec"."""
    if bits == every:
        return all_text
    runs: list[list[str]] = []
    last = None
    for index, (bit, name) in enumerate(names.items()):
        if not bits & (1 << bit):
            continue
        if runs and last == index - 1:
            runs[-1].append(name)
        else:
            runs.append([name])
        last = index
    return ", ".join(f"{run[0]}–{run[-1]}" if len(run) > 2 else ", ".join(run) for run in runs)


def _fields(group: EcoModeV2) -> dict[str, str]:
    """What a person would compare between two versions of a period, as the page shows each one."""
    return {
        "state": "on" if group.on_off < 0 else "off",
        "start": _clock(group.start_h, group.start_m),
        "end": _clock(group.end_h, group.end_m),
        "days": _named(days_meant(group.day_bits), DAY_NAMES, EVERY_DAY, "every day"),
        "months": _named(months_meant(group.month_bits), MONTH_NAMES, ALL_MONTHS, "all year"),
        "cutoff_soc": f"{group.soc}%",
        "power": f"{abs(group.get_power())}%",
    }


def describe_changes(old: bytes, new: bytes) -> list[dict[str, str]] | None:
    """What differs between two versions of a period, in the page's order. None if either can't be decoded."""
    try:
        before, after = _fields(decode(old)), _fields(decode(new))
    except ValueError:
        return None
    return [{"field": name, "old": before[name], "new": after[name]} for name in before if before[name] != after[name]]


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


def next_start(period: dict[str, object], now: datetime) -> datetime | None:
    """When the period next starts, at or after now, or None if it never runs or its start can't be read."""
    try:
        hours, minutes = (int(x) for x in str(period["start"]).split(":"))
        first = now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
    except (KeyError, ValueError):
        return None
    for days in range(0, 367):  # A period set for a single month may be most of a year away.
        start = first + timedelta(days=days)
        if start >= now and _runs_on(period, start):
            return start
    return None


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
