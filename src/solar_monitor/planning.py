"""Battery time remaining and appliance load sums. Plain arithmetic, with no I/O.

Ratings printed on the hardware aren't used where the inverter or the log says better:
voltage, state of health and minimum charge are read live, and capacity is measured.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

# Below this a power reading is mostly noise. The same gate as the dashboard's power flow.
IDLE_W = 50
# Long enough that a kettle doesn't swing the estimate, short enough to follow a real change.
SMOOTHING_SECONDS = 300

# Used only if the inverter doesn't report its own limit.
FALLBACK_MIN_SOC = 20
FALLBACK_VOLTS = 230
DEFAULT_TARGET_SOC = 100

# A measurement needs a clean stretch of samples: one reading of a gap or a reversal ends it.
MAX_SAMPLE_GAP_SECONDS = 120
# SOC is a whole percentage, so short stretches are mostly rounding.
MIN_STRETCH_POINTS = 3
MIN_MEASURED_POINTS = 10

# Typical figures for turning a household load into battery draw during an outage.
# pbattery1 is measured on the battery side, so the time remaining doesn't need them.
CONVERSION_EFFICIENCY = 0.93
STANDBY_W = 40


def smoothed_power(readings: Sequence[float | None]) -> float | None:
    """Average of the recent readings, oldest first, counting only the current direction.

    Once the battery reverses or stops, the older readings describe something that has ended.
    """
    known = [w for w in readings if w is not None]
    if not known:
        return None
    if abs(known[-1]) < IDLE_W:
        return known[-1]
    direction = next((w for w in reversed(known) if abs(w) >= IDLE_W), 0)
    current: list[float] = []
    for w in reversed(known):
        if w * direction < 0 and abs(w) >= IDLE_W:
            break
        current.append(w)
    return sum(current) / len(current)


def _direction(power_w: float) -> int:
    return 0 if abs(power_w) < IDLE_W else 1 if power_w > 0 else -1


def measure_capacity(samples: Iterable[tuple[int, float | None, float | None]]) -> tuple[float, int] | None:
    """(kWh per 100% charge, percentage points seen) from logged (ts, battery W, SOC), oldest first.

    Adds up the energy between the first and last change of SOC in each stretch of steady
    charging or discharging, so each stretch runs from one rounding boundary to another.
    None until enough has been logged to trust it.
    """
    total_wh = 0.0
    total_points = 0

    def finish(stretch: list[tuple[int, float, float]]) -> None:
        nonlocal total_wh, total_points
        changes = [i for i in range(1, len(stretch)) if stretch[i][2] != stretch[i - 1][2]]
        if not changes:
            return
        first, last = changes[0], changes[-1]
        points = abs(stretch[last][2] - stretch[first][2])
        if points < MIN_STRETCH_POINTS:
            return
        total_points += int(points)
        for i in range(first + 1, last + 1):
            total_wh += abs(stretch[i][1]) * (stretch[i][0] - stretch[i - 1][0]) / 3600

    stretch: list[tuple[int, float, float]] = []
    heading = 0
    for ts, power_w, soc in samples:
        if power_w is None or soc is None:
            continue
        direction = _direction(power_w)
        reversed_ = direction and heading and direction != heading
        gap = bool(stretch) and ts - stretch[-1][0] > MAX_SAMPLE_GAP_SECONDS
        if reversed_ or gap:
            finish(stretch)
            stretch = []
            heading = 0
        stretch.append((ts, power_w, soc))
        heading = heading or direction
    finish(stretch)

    if total_points < MIN_MEASURED_POINTS:
        return None
    return round(total_wh / total_points * 100 / 1000, 1), total_points


def battery_estimate(
    soc: float | None,
    power_w: float | None,
    capacity_kwh: float | None,
    min_soc: float,
    target_soc: float,
) -> dict[str, object] | None:
    """How long until the battery reaches its limit at this power. Negative power is charging.

    `hours` is None when the capacity isn't known yet, and 0 when the limit is already reached.
    """
    if soc is None or power_w is None:
        return None
    direction = _direction(power_w)
    if direction == 0:
        return {"state": "idle", "power_w": round(power_w), "limit_soc": None, "hours": None}
    limit = min_soc if direction > 0 else target_soc
    points = max(soc - limit, 0) if direction > 0 else max(limit - soc, 0)
    hours = None if capacity_kwh is None else points / 100 * capacity_kwh * 1000 / abs(power_w)
    return {
        "state": "discharging" if direction > 0 else "charging",
        "power_w": round(power_w),
        "limit_soc": limit,
        "hours": 0 if points == 0 else hours,
    }


def outage_hours(load_w: float, start_soc: float, reserve_soc: float, capacity_kwh: float | None) -> float | None:
    """How long the battery could carry this household load with no sun or grid."""
    if capacity_kwh is None:
        return None
    available_wh = capacity_kwh * 1000 * max(start_soc - reserve_soc, 0) / 100
    return available_wh / (load_w / CONVERSION_EFFICIENCY + STANDBY_W)


def worst_w(appliance: dict[str, object]) -> float:
    """The most it draws once running: its peak, or its typical draw if no peak was given."""
    peak = appliance.get("peak_w")
    return appliance["typical_w"] if peak is None else peak


def _case(
    load_w: float, volts: float, limit_amps: float | None, soc: float | None, reserve_soc: float, capacity_kwh: float | None
) -> dict[str, object]:
    amps = load_w / volts
    return {
        "watts": load_w,
        "amps": amps,
        "share": None if limit_amps is None else amps / limit_amps,
        "headroom_amps": None if limit_amps is None else limit_amps - amps,
        "hours_from_now": None if soc is None else outage_hours(load_w, soc, reserve_soc, capacity_kwh),
        "hours_from_full": outage_hours(load_w, 100, reserve_soc, capacity_kwh),
    }


def _surge(appliances: list[dict[str, object]], surge_limit_w: float | None) -> dict[str, object]:
    """The worst moment: everything running, plus every motor starting at once."""
    steady = sum(worst_w(a) for a in appliances)
    extra = sum(max(a["surge_w"] - worst_w(a), 0) for a in appliances if a.get("surge_w") is not None)
    peak = steady + extra
    return {
        "steady_w": steady,
        "extra_w": extra,
        "peak_w": peak,
        "share": None if surge_limit_w is None else peak / surge_limit_w,
    }


def summarize(
    appliances: list[dict[str, object]],
    *,
    volts: float,
    limit_amps: float | None,
    surge_limit_w: float | None,
    live_load_w: float | None,
    soc: float | None,
    reserve_soc: float,
    capacity_kwh: float | None,
) -> dict[str, object]:
    """Each appliance's share of the limit, and what each combination of them would mean."""
    selected = [a for a in appliances if a["use_in_outage"]]

    def case(load_w: float) -> dict[str, object]:
        return _case(load_w, volts, limit_amps, soc, reserve_soc, capacity_kwh)

    selected_worst = case(sum(worst_w(a) for a in selected))

    def row(appliance: dict[str, object]) -> dict[str, object]:
        amps = worst_w(appliance) / volts
        if appliance["use_in_outage"]:
            can_add = "in_use"
        elif limit_amps is None:
            can_add = None
        else:
            can_add = "yes" if amps <= selected_worst["headroom_amps"] else "no"
        return {
            **appliance,
            "amps": amps,
            "share": None if limit_amps is None else amps / limit_amps,
            "can_add": can_add,
        }

    return {
        "appliances": [row(a) for a in appliances],
        "cases": {
            "now": None if live_load_w is None else case(live_load_w),
            "selected_typical": case(sum(a["typical_w"] for a in selected)),
            "selected_worst": selected_worst,
            "all_typical": case(sum(a["typical_w"] for a in appliances)),
            "all_worst": case(sum(worst_w(a) for a in appliances)),
        },
        "surge": {"selected": _surge(selected, surge_limit_w), "all": _surge(appliances, surge_limit_w)},
    }
