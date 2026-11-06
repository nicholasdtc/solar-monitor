"""The battery schedule as the inverter holds it. This only reads: nothing here writes to the inverter."""

from __future__ import annotations

import goodwe
from goodwe.sensor import EcoModeV1, ScheduleType

TIME_PERIODS = 4  # What the library exposes. SEMS+ shows them in this same order.
ALL_DAYS = (127, -1)
TOU_TYPES = (ScheduleType.ECO_MODE, ScheduleType.ECO_MODE_745)

# Value 0 is what SEMS+ calls Self-consumption. The library calls it General.
WORK_MODES = {0: "Self-consumption", 1: "Off-grid", 2: "Backup", 3: "Eco", 4: "Peak shaving", 5: "Self-use"}


def _clock(hours: int, minutes: int) -> str:
    return f"{hours:02}:{minutes:02}"


def _is_empty(group) -> bool:
    # A slot that was never used is all zeros, a cleared one starts at hour 48 or -1, and 85 means not set.
    return group.day_bits == 0 or group.start_h in (48, -1) or group.on_off == ScheduleType.NOT_SET


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
    }


async def _read(inverter: goodwe.Inverter, setting: str):
    try:
        return await inverter.read_setting(setting)
    except Exception:  # One unreadable setting shows as unknown, and shouldn't cost the rest of the page.
        return None


async def _switch(inverter: goodwe.Inverter, setting: str) -> bool | None:
    value = await _read(inverter, setting)
    return None if value is None else bool(value)


async def read_schedule(inverter: goodwe.Inverter) -> dict[str, object]:
    """The working mode, the mode switches and the time periods. Anything unreadable comes back as None."""
    work_mode = await _read(inverter, "work_mode")
    return {
        "work_mode": None if work_mode is None else WORK_MODES.get(work_mode, f"Unknown ({work_mode})"),
        "tou_on": await _switch(inverter, "eco_mode_enable"),  # Separate from the working mode, as SEMS+ shows it.
        "backup_on": await _switch(inverter, "backup_mode_enable"),
        "smart_charge_on": await _switch(inverter, "smart_charging_enable"),
        "periods": [
            describe_period(slot, await _read(inverter, f"eco_mode_{slot}"))
            for slot in range(1, TIME_PERIODS + 1)
        ],
    }
