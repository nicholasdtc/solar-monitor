"""Battery time remaining, load sums, load planners and tariffs. Plain arithmetic, with no I/O.

Ratings printed on the hardware aren't used where the inverter or the log says better:
voltage, state of health and minimum charge are read live, and capacity and the inverter's
overhead are measured.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import NamedTuple

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

# Typical figures for turning a household load into battery draw, until the log can measure them.
# pbattery1 is measured on the battery side, so the time remaining doesn't need them.
CONVERSION_EFFICIENCY = 0.93
STANDBY_W = 40
# Typical figure for grid to battery to home. The log has no way to measure that whole path.
ROUND_TRIP_EFFICIENCY = 0.90

# The battery only counts as supplying the house alone with next to nothing from solar or the
# grid. This is tight because a flow of 50 W would be as big as the overhead being measured.
BATTERY_ONLY_MARGIN_W = 20
OVERHEAD_BAND_W = 250
MIN_BAND_SAMPLES = 10
# Bands close together can give a wild slope, so the fit needs a good spread of loads.
MIN_OVERHEAD_BANDS = 4
MIN_BELIEVABLE_EFFICIENCY = 0.8


class Overhead(NamedTuple):
    """What the inverter takes for itself: battery draw = load / efficiency + standby_w."""

    standby_w: float
    efficiency: float


TYPICAL_OVERHEAD = Overhead(STANDBY_W, CONVERSION_EFFICIENCY)


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


def battery_only(pv_w: object, load_w: object, battery_w: object, grid_w: object) -> bool:
    """Whether the battery alone is supplying the house, so any gap between them is the inverter's."""
    if not all(isinstance(w, (int, float)) for w in (pv_w, load_w, battery_w, grid_w)):
        return False
    quiet = abs(pv_w) <= BATTERY_ONLY_MARGIN_W and abs(grid_w) <= BATTERY_ONLY_MARGIN_W
    return quiet and battery_w >= IDLE_W and load_w > 0


def _overhead_row(house_w: float, overhead_w: float) -> dict[str, float | None]:
    battery_w = house_w + overhead_w
    return {
        "house_w": house_w,
        "battery_w": battery_w,
        "overhead_w": overhead_w,
        "share": overhead_w / battery_w if battery_w > 0 else None,
    }


def overhead_now(pv_w: object, load_w: object, battery_w: object, grid_w: object) -> dict[str, float | None] | None:
    """Battery power not reaching the house at this moment, if the battery alone is supplying it."""
    if not battery_only(pv_w, load_w, battery_w, grid_w):
        return None
    return _overhead_row(load_w, battery_w - load_w)


def overhead_bands(samples: Iterable[tuple[object, object, object, object]]) -> list[dict[str, float | None]]:
    """Battery power not reaching the house at each level of load, from logged (solar, house, battery, grid).

    Medians, because one reading can be a kilowatt out while a load is switching on or off.
    Battery is worked out from the other two, in whole watts, so that each row subtracts exactly.
    """
    groups: dict[int, list[tuple[float, float]]] = {}
    for pv_w, load_w, battery_w, grid_w in samples:
        if battery_only(pv_w, load_w, battery_w, grid_w):
            groups.setdefault(int(load_w // OVERHEAD_BAND_W), []).append((load_w, battery_w - load_w))
    return [
        {
            **_overhead_row(
                round(statistics.median(house_w for house_w, _ in group)),
                round(statistics.median(overhead_w for _, overhead_w in group)),
            ),
            "samples": len(group),
        }
        for _, group in sorted(groups.items())
        if len(group) >= MIN_BAND_SAMPLES
    ]


def fit_overhead(bands: Sequence[dict[str, float | None]]) -> Overhead | None:
    """The straight line through the bands: overhead = standby + load * (1 / efficiency - 1).

    None until there's enough to fit, or if what comes out isn't believable.
    """
    if len(bands) < MIN_OVERHEAD_BANDS:
        return None
    gaps = [band["overhead_w"] for band in bands]
    slope, standby_w = statistics.linear_regression([band["house_w"] for band in bands], gaps)
    if slope < 0:  # More load can't cost less, so a falling line is only noise.
        slope, standby_w = 0, statistics.mean(gaps)
    efficiency = 1 / (1 + slope)
    if standby_w <= 0 or efficiency < MIN_BELIEVABLE_EFFICIENCY:
        return None
    return Overhead(round(standby_w), round(efficiency, 3))


def measure_overhead(samples: Iterable[tuple[object, object, object, object]]) -> dict[str, object]:
    """The inverter's overhead from the log once there's enough of it, until then the typical figures."""
    bands = overhead_bands(samples)
    fitted = fit_overhead(bands)
    return {
        **(fitted or TYPICAL_OVERHEAD)._asdict(),
        "source": "measured" if fitted else "typical",
        "bands": bands,
    }


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


def available_wh(capacity_kwh: float, start_soc: float, stop_soc: float) -> float:
    return capacity_kwh * 1000 * max(start_soc - stop_soc, 0) / 100


def outage_hours(
    load_w: float,
    start_soc: float,
    reserve_soc: float,
    capacity_kwh: float | None,
    overhead: Overhead = TYPICAL_OVERHEAD,
) -> float | None:
    """How long the battery could carry this household load with no sun or grid."""
    if capacity_kwh is None:
        return None
    return available_wh(capacity_kwh, start_soc, reserve_soc) / (load_w / overhead.efficiency + overhead.standby_w)


def max_load_w(energy_wh: float, hours: float, overhead: Overhead = TYPICAL_OVERHEAD) -> float:
    """The steady household load this energy could carry for this long: outage_hours in reverse."""
    return max((energy_wh / hours - overhead.standby_w) * overhead.efficiency, 0)


def _allowance(
    energy_wh: float, hours: float, limit_w: float | None, selected_w: float, overhead: Overhead
) -> dict[str, object]:
    """The most that can run, whichever of the energy or the limit runs out first."""
    load_w = max_load_w(energy_wh, hours, overhead)
    capped = limit_w is not None and load_w > limit_w
    most_w = limit_w if capped else load_w
    return {"max_w": most_w, "capped": capped, "spare_w": most_w - selected_w}


def outage_plan(
    hours: float,
    selected_w: float,
    soc: float | None,
    reserve_soc: float,
    capacity_kwh: float | None,
    limit_w: float | None,
    overhead: Overhead = TYPICAL_OVERHEAD,
) -> dict[str, object]:
    """The most the battery could carry through a power cut this long, from now and from full."""

    def starting_at(start_soc: float | None) -> dict[str, object] | None:
        if start_soc is None or capacity_kwh is None:
            return None
        return _allowance(available_wh(capacity_kwh, start_soc, reserve_soc), hours, limit_w, selected_w, overhead)

    return {
        "hours": hours,
        "selected_w": selected_w,
        "limit_w": limit_w,
        "from_now": starting_at(soc),
        "from_full": starting_at(100),
    }


def next_time(now: datetime, minutes_of_day: int) -> datetime:
    """The next time this clock time comes round. Once it has arrived, that means tomorrow."""
    today = now.replace(hour=minutes_of_day // 60, minute=minutes_of_day % 60, second=0, microsecond=0)
    return today if today > now else today + timedelta(days=1)


def run_until_plan(
    now: datetime,
    minutes_of_day: int,
    *,
    soc: float | None,
    min_soc: float,
    capacity_kwh: float | None,
    limit_w: float | None,
    selected_w: float,
    battery_w: float | None,
    overhead: Overhead = TYPICAL_OVERHEAD,
) -> dict[str, object]:
    """The average load the battery could carry until a clock time, ignoring solar and the grid.

    `now` is local wall-clock time, and going through timestamp() allows for a clock change.
    """
    until = next_time(now, minutes_of_day)
    hours = (until.timestamp() - now.timestamp()) / 3600
    plan: dict[str, object] = {
        "until": until.isoformat(timespec="minutes"),
        "hours": hours,
        "min_soc": min_soc,
        "selected_w": selected_w,
        "limit_w": limit_w,
        "battery_w": battery_w,
        "energy_kwh": None,
        "max_battery_w": None,
        "max_w": None,
        "capped": False,
        "spare_w": None,
    }
    if soc is None or capacity_kwh is None:
        return plan
    energy_wh = available_wh(capacity_kwh, soc, min_soc)
    return {
        **plan,
        **_allowance(energy_wh, hours, limit_w, selected_w, overhead),
        "energy_kwh": energy_wh / 1000,
        "max_battery_w": energy_wh / hours,
    }


def tariff_values(
    day: float | None, peak: float | None, night: float | None, export: float | None
) -> dict[str, float | None]:
    """What a battery kWh is worth at these rates, in the same units as the rates."""

    def less(rate: float | None, cost: float | None) -> float | None:
        return None if rate is None or cost is None else rate - cost

    delivered = None if night is None else night / ROUND_TRIP_EFFICIENCY
    return {
        "round_trip_efficiency": ROUND_TRIP_EFFICIENCY,
        "delivery_efficiency": CONVERSION_EFFICIENCY,
        "night_kwh_cost": delivered,
        "saving_day": less(day, delivered),
        "saving_peak": less(peak, delivered),
        "gain_export": less(export, delivered),
        # A kWh still in the battery as the night rate starts: export it, or use it instead of buying one?
        "leftover_export": less(None if export is None else export * CONVERSION_EFFICIENCY, night),
        "solar_export": export,
    }


def worst_w(appliance: dict[str, object]) -> float:
    """The most it draws once running: its peak, or its typical draw if no peak was given."""
    peak = appliance.get("peak_w")
    return appliance["typical_w"] if peak is None else peak


def _case(
    load_w: float,
    volts: float,
    limit_amps: float | None,
    soc: float | None,
    reserve_soc: float,
    capacity_kwh: float | None,
    overhead: Overhead,
) -> dict[str, object]:
    amps = load_w / volts
    return {
        "watts": load_w,
        "amps": amps,
        "share": None if limit_amps is None else amps / limit_amps,
        "headroom_amps": None if limit_amps is None else limit_amps - amps,
        "hours_from_now": None if soc is None else outage_hours(load_w, soc, reserve_soc, capacity_kwh, overhead),
        "hours_from_full": outage_hours(load_w, 100, reserve_soc, capacity_kwh, overhead),
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
    overhead: Overhead = TYPICAL_OVERHEAD,
) -> dict[str, object]:
    """Each appliance's share of the limit, and what each combination of them would mean."""
    selected = [a for a in appliances if a["use_in_outage"]]

    def case(load_w: float) -> dict[str, object]:
        return _case(load_w, volts, limit_amps, soc, reserve_soc, capacity_kwh, overhead)

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
