"""What automation would do, worked out from the weather forecast and what the app reads live.

Suggestions only. Nothing here writes to the inverter, and nothing here leads to control.py, the
one module that can: tests/test_automation.py follows every import to check. V2 is where a rule
first gets to act, and then only through control.py's checked path.

A rule has its own settings, kept with the others in the database. One left unset takes its
default from what the app reads live (the TOU periods, the forecast's measured error), never
from a fixed figure, the decision time apart.

Each rule makes one decision for each time its TOU period starts, at its decision time. That
decision is logged in automation_runs with what it was made from, so it can be set beside what
actually happened.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from solar_monitor import db
from solar_monitor.schedule import HIGHEST_CUTOFF, LOWEST_CUTOFF, TIME_PERIODS, next_start
from solar_monitor.weather.base import HOUR

CHECK_SECONDS = 60  # How often the runner looks for a decision that's due.
LOAD_DAYS = 28  # The typical load per hour is from this many days of the log.
DEFAULT_DECIDE_MINUTES = 60  # A decision is made this long before its period starts, unless set.
MIDDAY_HOUR = 12  # The sunny morning rule counts the sun up to this local hour.


@dataclass(frozen=True)
class Setting:
    key: str  # In db.SETTINGS.
    label: str
    low: float
    high: float


@dataclass(frozen=True)
class Recommendation:
    setting: str  # What it concerns, such as "TOU 2 cut-off".
    current: float | None  # What the inverter holds.
    would: float | None  # What the rule would choose, or None if it can't decide, the reason says why.
    reason: str  # A few words.
    figures: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class State:
    """What a rule decides from, all of it read by the app already."""

    now: int
    settings: dict[str, object] = field(default_factory=dict)  # db.get_settings()
    schedule: dict[str, object] | None = None  # The TOU periods and switch, as the collector read them.
    soc: float | None = None
    limits: dict[str, object] = field(default_factory=dict)  # The floors and ceiling: min_soc, max_soc, reserve_soc.
    capacity_kwh: float | None = None
    capacity_source: str | None = None  # measured or entered
    load_by_hour: dict[int, float] = field(default_factory=dict)  # Typical W, by local hour of the day.
    error_pct: float | None = None  # How far off the day-ahead solar forecast has been, a day.
    fit_status: str | None = None  # The solar forecast's: learning or ready, None without one.
    sunrises: list[int] = field(default_factory=list)
    forecast_fetched_at: int | None = None

    def summary(self) -> dict[str, object]:
        """What a logged run keeps of the state: enough to see why it decided as it did."""
        periods = (self.schedule or {}).get("periods", [])
        return {
            "soc": self.soc,
            "limits": self.limits,
            "capacity_kwh": self.capacity_kwh,
            "capacity_source": self.capacity_source,
            "error_pct": self.error_pct,
            "fit_status": self.fit_status,
            "forecast_fetched_at": self.forecast_fetched_at,
            "tou_on": (self.schedule or {}).get("tou_on"),
            "periods": [{k: v for k, v in p.items() if k != "raw"} for p in periods],  # type: ignore[union-attr]
            "load_by_hour": {str(hour): round(w) for hour, w in sorted(self.load_by_hour.items())},
        }


def _tou_active(schedule: dict[str, object]) -> bool:
    """As the inverter has it: the switch, or the Eco working mode where the switch can't be read."""
    return bool(schedule.get("tou_on") or (schedule.get("tou_on") is None and schedule.get("work_mode") == "Eco"))


def _is_charge_period(period: dict[str, object]) -> bool:
    cutoff = period.get("cutoff_soc")
    return period.get("state") == "on" and period.get("action") == "charge" and isinstance(cutoff, int) and 1 <= cutoff <= 100


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class SunnyMorning:
    """V2: charge the battery less overnight when the morning's sun will fill the room left."""

    name = "sunny_morning"
    title = "Charge less before a sunny morning"
    settings = (
        Setting("auto_sunny_slot", "TOU period", 1, TIME_PERIODS),
        Setting("auto_sunny_usual_soc", "Usual cut-off (%)", LOWEST_CUTOFF, HIGHEST_CUTOFF),
        Setting("auto_sunny_sunny_soc", "Sunny cut-off (%)", LOWEST_CUTOFF, HIGHEST_CUTOFF),
        Setting("auto_sunny_margin_pct", "Margin (%)", 0, 90),
        Setting("auto_sunny_lead_min", "Decide, minutes before", 0, 12 * 60),
    )

    def periods(self, state: State) -> list[dict[str, object]]:
        return [p for p in (state.schedule or {}).get("periods", []) if _is_charge_period(p)]  # type: ignore[union-attr]

    def resolve(self, state: State) -> tuple[dict[str, object], list[str]]:
        """Each setting's value, and the keys whose value is a default because they're unset."""
        chosen = {s.key: _number(state.settings.get(s.key)) for s in self.settings}
        charges = self.periods(state)
        if chosen["auto_sunny_slot"] is None and charges:
            chosen["auto_sunny_slot"] = charges[0]["slot"]
        period = self.period(state, chosen)
        defaults = {
            "auto_sunny_usual_soc": period.get("cutoff_soc") if period else None,
            "auto_sunny_margin_pct": state.error_pct,
            "auto_sunny_lead_min": DEFAULT_DECIDE_MINUTES,
        }
        unset = [s.key for s in self.settings if _number(state.settings.get(s.key)) is None]
        for key, value in defaults.items():
            if chosen[key] is None:
                chosen[key] = value
        if chosen["auto_sunny_slot"] is not None:
            chosen["auto_sunny_slot"] = int(chosen["auto_sunny_slot"])  # type: ignore[arg-type]
        return chosen, unset

    @staticmethod
    def period(state: State, chosen: dict[str, object]) -> dict[str, object] | None:
        slot = chosen.get("auto_sunny_slot")
        periods = (state.schedule or {}).get("periods", [])
        return next((p for p in periods if p.get("slot") == slot), None)  # type: ignore[union-attr]

    def next_start(self, state: State) -> datetime | None:
        """When the chosen period next starts, if it's a charge period that's on."""
        chosen, _ = self.resolve(state)
        period = self.period(state, chosen)
        if period is None or not _is_charge_period(period) or not _tou_active(state.schedule or {}):
            return None
        return next_start(period, datetime.fromtimestamp(state.now))

    def decision(self, state: State) -> tuple[int, int] | None:
        """(the start of the period it concerns, when to decide), or None while there's nothing to decide."""
        start = self.next_start(state)
        if start is None:
            return None
        chosen, _ = self.resolve(state)
        when = start - timedelta(minutes=float(chosen["auto_sunny_lead_min"]))  # type: ignore[arg-type]
        return int(start.timestamp()), int(when.timestamp())

    def evaluate(self, forecast: list[dict[str, object]], state: State) -> Recommendation | None:
        chosen, unset = self.resolve(state)
        slot = chosen["auto_sunny_slot"]
        if slot is None:
            return None  # No charge period to talk about.
        period = self.period(state, chosen)
        usual, sunny, margin = chosen["auto_sunny_usual_soc"], chosen["auto_sunny_sunny_soc"], chosen["auto_sunny_margin_pct"]
        current = period.get("cutoff_soc") if period else None
        figures: dict[str, object] = {
            "slot": slot,
            "usual_soc": usual,
            "sunny_soc": sunny,
            "margin_pct": margin,
            "defaults": unset,
            "fit_status": state.fit_status,
            "capacity_kwh": state.capacity_kwh,
        }

        def cannot(reason: str) -> Recommendation:
            return Recommendation(f"TOU {slot} cut-off", current, None, reason, figures)  # type: ignore[arg-type]

        if period is None or not _is_charge_period(period):
            return cannot("Not a charge period that's on")
        if not _tou_active(state.schedule or {}):
            return cannot("TOU is off")
        start = next_start(period, datetime.fromtimestamp(state.now))
        if start is None:
            return cannot("Period never runs")
        figures["for_ts"] = int(start.timestamp())
        if sunny is None:
            return cannot("Sunny cut-off not set")
        if usual is None or not (LOWEST_CUTOFF <= usual <= HIGHEST_CUTOFF and LOWEST_CUTOFF <= sunny <= HIGHEST_CUTOFF):  # type: ignore[operator]
            return cannot(f"Cut-offs must be {LOWEST_CUTOFF}–{HIGHEST_CUTOFF}%")
        if sunny >= usual:  # type: ignore[operator]
            return cannot("Sunny cut-off not below usual")
        if current not in (usual, sunny):
            return cannot("Changed by hand")
        if state.capacity_kwh is None:
            return cannot("Battery capacity unknown")
        if state.fit_status is None:
            return cannot("No solar forecast yet")
        if margin is None:
            return cannot("Forecast error not known yet")

        morning = self.morning(period, start, forecast, state)
        if isinstance(morning, str):
            return cannot(morning)
        window_start, window_end, hours = morning
        pv_kwh = sum(h["expected_w"] * h["share"] for h in hours) / 1000  # type: ignore[operator]
        load_kwh = sum(h["load_w"] * h["share"] for h in hours) / 1000  # type: ignore[operator]
        # The margin is a share of the forecast, so it comes off the solar, not the load.
        spare_kwh = pv_kwh * (1 - margin / 100) - load_kwh  # type: ignore[operator]
        room_kwh = state.capacity_kwh * (usual - sunny) / 100  # type: ignore[operator]
        figures.update({
            "morning_start": window_start,
            "morning_end": window_end,
            "hours": hours,
            "pv_kwh": round(pv_kwh, 2),
            "load_kwh": round(load_kwh, 2),
            "spare_kwh": round(spare_kwh, 2),
            "room_kwh": round(room_kwh, 2),
        })
        if spare_kwh >= room_kwh:
            return Recommendation(f"TOU {slot} cut-off", current, sunny, f"Sunny morning: {spare_kwh:.1f} kWh spare, {room_kwh:.1f} room", figures)  # type: ignore[arg-type]
        return Recommendation(f"TOU {slot} cut-off", current, usual, f"Not sunny enough: {max(spare_kwh, 0):.1f} of {room_kwh:.1f} kWh", figures)  # type: ignore[arg-type]

    @staticmethod
    def morning(
        period: dict[str, object], start: datetime, forecast: list[dict[str, object]], state: State
    ) -> tuple[int, int, list[dict[str, object]]] | str:
        """The morning after the period: sunrise to midday on the day it ends, and the forecast and
        typical load for each hour of it, with the share of each hour that falls in it. Or why not."""
        day = start.date() + timedelta(days=1 if period.get("next_day") else 0)
        midday = int(datetime(day.year, day.month, day.day, MIDDAY_HOUR).timestamp())
        expected = {int(h["hour_start"]): h.get("expected_w") for h in forecast}  # type: ignore[call-overload]
        rise = next((r for r in state.sunrises if datetime.fromtimestamp(r).date() == day), None)
        if rise is None:  # Without a location for the sun, the first hour the forecast expects output.
            first_of_day = int(datetime(day.year, day.month, day.day).timestamp())
            lit = [t for t, w in sorted(expected.items()) if first_of_day <= t < midday and isinstance(w, (int, float)) and w > 0]
            if not lit:
                return "Forecast doesn't reach the morning" if not any(first_of_day <= t < midday for t in expected) else "No sun before midday"
            rise = lit[0]
        if rise >= midday:
            return "No sun before midday"
        hours = []
        hour = db.bucket_start(rise, HOUR)
        while hour < midday:
            w = expected.get(hour)
            if not isinstance(w, (int, float)):
                return "Forecast doesn't reach the morning"
            load = state.load_by_hour.get(datetime.fromtimestamp(hour).hour)
            if load is None:
                return "Typical load not known"
            share = (min(hour + HOUR, midday) - max(hour, rise)) / HOUR
            hours.append({"hour_start": hour, "expected_w": round(float(w)), "load_w": round(load), "share": round(share, 3)})
            hour += HOUR
        return rise, midday, hours


rules = {rule.name: rule for rule in (SunnyMorning(),)}
SETTINGS = {setting.key: setting for rule in rules.values() for setting in rule.settings}


def problem(key: str, value: object) -> str | None:
    """What's wrong with a value for one of the rules' settings, or None if it will do. None unsets."""
    setting = SETTINGS.get(key)
    if setting is None:
        return f"{key} isn't an automation setting"
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
        return f"{setting.label}: a whole number"
    if not setting.low <= value <= setting.high:
        return f"{setting.label}: {setting.low:g} to {setting.high:g}"
    return None


def evaluate(rule: SunnyMorning, forecast: list[dict[str, object]], state: State) -> Recommendation | None:
    """What the rule would do now, or None if it has nothing to say."""
    return rule.evaluate(forecast, state)


def tick(conn: sqlite3.Connection, now: int, state_of: Callable[[bool], tuple[list[dict[str, object]], State]]) -> int:
    """Make and log each decision that's due and not yet made. state_of(False) is enough to know
    what's due; state_of(True) adds the parts that take a while, such as the typical load. Only logs.
    Returns how many runs were logged."""
    logged = 0
    full: tuple[list[dict[str, object]], State] | None = None
    _, light = state_of(False)
    for rule in rules.values():
        decision = rule.decision(light)
        if decision is None:
            continue
        for_ts, decide_at = decision
        if now < decide_at or db.automation_run_exists(conn, rule.name, for_ts):
            continue
        full = full or state_of(True)
        forecast, state = full
        recommendation = evaluate(rule, forecast, state)
        if recommendation is None:
            continue
        db.add_automation_run(conn, now, rule.name, for_ts, state.summary(), recommendation.to_dict())
        logged += 1
    return logged
