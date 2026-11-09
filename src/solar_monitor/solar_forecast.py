"""Expected solar output, learned from the log and the weather.

Each hour's expected output is its sunshine times a factor, in W per W/m²: on the panels' tilt
and facings where those are set, otherwise on flat ground. The factor is learned for each hour of
the day (UTC, so the sun is at about the same place each day), because panels facing two ways, or
shaded at one end of the day, make more of the same sunshine at some hours than others.

Each hour's factor starts from the one for all hours, which starts from a guess when the panels'
rating is set: the rating times START_RATIO, a typical performance ratio. As hours are logged the
data outweighs the guesses. Output never goes over the ceiling: the lower of the panels' rating
and the most they've been seen to make.

The fit is redone each night from the last FIT_DAYS of hours with enough logged and enough sun.
The expected output for each hour of tomorrow is kept from each forecast fetched today, so the
last forecast before midnight is the day-ahead one each day is scored against.

Known limit: a factor for an hour of the day is an average over the seasons it has seen, while
the sun's position at that hour shifts with the season.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime

from solar_monitor import db
from solar_monitor.weather.base import HOUR

DAY = 24 * HOUR
FIT_DAYS = 60
START_RATIO = 0.8  # The guessed performance ratio: output over rating × sunshine on the panels / 1000 W/m².
MIN_SUN_WM2 = 50  # Dimmer hours say more about the dark than about the panels.
COVERAGE_SECONDS = 45 * 60  # An hour's readings must span this much of it to count.
CLIPPED_SHARE = 0.9  # An hour averaging this share of the ceiling was most likely held down by it.
PRIOR_HOURS = 10  # How many hours of data the guess for all hours counts as.
HOUR_PRIOR_HOURS = 3  # How many hours of data the all-hours factor counts as in each hour's own.
REFERENCE_WM2 = 300.0  # The sunshine those prior hours are taken to have had.
READY_DAYS = 14  # The fit is learning until it has this many days with...
READY_HOURS_A_DAY = 3  # ...at least this many usable hours each.
SCORE_DAYS = 30


@dataclass(frozen=True)
class Pair:
    """One logged hour beside its weather."""

    hour_start: int
    sun_wm2: float
    made_w: float  # the hour's mean output


@dataclass
class Fit:
    basis: str  # "gti", sunshine on the panels, or "ghi", on flat ground
    panels: str | None  # what the gti was for, as weather_hours.panels has it
    kwp: float | None
    ceiling_w: float | None
    factor: float  # W per W/m², for all hours
    factors: dict[int, float] = field(default_factory=dict)  # by UTC hour of the day
    hours: int = 0  # hours learned from
    days: int = 0  # days with READY_HOURS_A_DAY of them
    fitted_at: int = 0

    @property
    def status(self) -> str:
        return "ready" if self.days >= READY_DAYS else "learning"

    @property
    def ratio(self) -> float | None:
        """The performance ratio: the factor over the rating, on the panels' own sunshine only."""
        return self.factor / self.kwp if self.basis == "gti" and self.kwp else None

    def sun_of(self, hour: dict[str, object]) -> float | None:
        value = hour.get("gti_wm2" if self.basis == "gti" else "ghi_wm2")
        return float(value) if isinstance(value, (int, float)) else None  # type: ignore[arg-type]

    def expected_w(self, hour_start: int, sun_wm2: float | None) -> float | None:
        if sun_wm2 is None:
            return None
        w = max(0.0, self.factors.get(hour_start % DAY // HOUR, self.factor) * sun_wm2)
        return min(w, self.ceiling_w) if self.ceiling_w is not None else w

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> Fit:
        values = json.loads(text)
        values["factors"] = {int(k): float(v) for k, v in values.get("factors", {}).items()}
        return cls(**values)


def ceiling(kwp: float | None, most_made_w: float | None) -> float | None:
    limits = [x for x in (kwp * 1000 if kwp else None, most_made_w if most_made_w and most_made_w > 0 else None) if x]
    return min(limits) if limits else None


def fit(
    pairs: Iterable[Pair], kwp: float | None, basis: str, panels: str | None, ceiling_w: float | None, fitted_at: int
) -> Fit | None:
    """Learn the factors, or None with no rating and nothing to learn from."""
    usable = [
        p
        for p in pairs
        if p.sun_wm2 >= MIN_SUN_WM2 and p.made_w >= 0 and (ceiling_w is None or p.made_w < CLIPPED_SHARE * ceiling_w)
    ]
    if not usable and not kwp:
        return None
    reference = REFERENCE_WM2**2
    prior = PRIOR_HOURS * reference if kwp else 0.0
    guess = kwp * START_RATIO if kwp else 0.0
    made_sun = sum(p.made_w * p.sun_wm2 for p in usable)
    sun_sun = sum(p.sun_wm2**2 for p in usable)
    factor = (made_sun + prior * guess) / (sun_sun + prior)

    by_hour: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0])
    per_day: dict[int, int] = defaultdict(int)
    for p in usable:
        sums = by_hour[p.hour_start % DAY // HOUR]
        sums[0] += p.made_w * p.sun_wm2
        sums[1] += p.sun_wm2**2
        per_day[p.hour_start // DAY] += 1
    weight = HOUR_PRIOR_HOURS * reference
    factors = {hour: (made + weight * factor) / (sun + weight) for hour, (made, sun) in sorted(by_hour.items())}
    days = sum(1 for count in per_day.values() if count >= READY_HOURS_A_DAY)
    return Fit(basis, panels, kwp, ceiling_w, factor, factors, len(usable), days, fitted_at)


# The database side.


def hourly_made(conn: sqlite3.Connection, start: int, end: int, coverage: int = COVERAGE_SECONDS) -> dict[int, float]:
    """Each UTC hour's mean output from `start` up to `end`, where its readings span `coverage` seconds."""
    return {
        int(hour): float(mean)
        for hour, mean in conn.execute(
            "SELECT ts / 3600 * 3600 AS hour, AVG(MAX(COALESCE(pv_w, 0), 0)) FROM samples "
            "WHERE ts >= ? AND ts < ? GROUP BY hour HAVING MAX(ts) - MIN(ts) >= ?",
            (start, end, coverage),
        )
    }


def basis_of(panels: str | None) -> str:
    return "gti" if panels is not None else "ghi"


def refit(conn: sqlite3.Connection, now: int, panels: str | None) -> Fit | None:
    """Learn from the last FIT_DAYS and keep the fit, or forget it if there's nothing to go on."""
    settings = db.get_settings(conn)
    kwp = settings.get("pv_capacity_kwp")
    kwp = float(kwp) if isinstance(kwp, (int, float)) and kwp > 0 else None
    basis = basis_of(panels)
    start, end = now // DAY * DAY - FIT_DAYS * DAY, now // HOUR * HOUR
    made = hourly_made(conn, start, end)
    column = "gti_wm2" if basis == "gti" else "ghi_wm2"
    sun = conn.execute(
        f"SELECT hour_start, {column} FROM weather_hours WHERE hour_start >= ? AND hour_start < ? "
        f"AND {column} IS NOT NULL" + (" AND panels = ?" if basis == "gti" else ""),
        (start, end, panels) if basis == "gti" else (start, end),
    ).fetchall()
    pairs = [Pair(int(hour), float(wm2), made[hour]) for hour, wm2 in sun if hour in made]
    most = conn.execute("SELECT MAX(pv_w) FROM samples").fetchone()[0]
    result = fit(pairs, kwp, basis, panels, ceiling(kwp, most), now)
    with conn:
        conn.execute("DELETE FROM solar_fit")
        if result is not None:
            conn.execute("INSERT INTO solar_fit (id, fitted_at, fit) VALUES (1, ?, ?)", (now, result.to_json()))
    return result


def load_fit(conn: sqlite3.Connection) -> Fit | None:
    row = conn.execute("SELECT fit FROM solar_fit WHERE id = 1").fetchone()
    if row is None:
        return None
    try:
        return Fit.from_json(row[0])
    except (ValueError, TypeError, KeyError):  # From a version that kept something else.
        return None


def day_start(ts: int) -> int:
    """Local midnight at the start of the day holding `ts`, on this machine's clock."""
    return db.bucket_start(ts, DAY)


def next_day_start(ts: int) -> int:
    return db.bucket_end(ts, DAY)


def local_date(ts: int) -> str:
    return datetime.fromtimestamp(ts).date().isoformat()


def record_day_ahead(conn: sqlite3.Connection, fetch_id: int, fetched_at: int, hours: Iterable[object], fit_: Fit | None) -> None:
    """Keep what this forecast expects for each hour of tomorrow, replacing an earlier forecast's.
    Hours of today are left as the last forecast of yesterday had them."""
    if fit_ is None:
        return
    tomorrow = next_day_start(fetched_at)
    after = next_day_start(tomorrow)
    rows = []
    for hour in hours:
        values = hour if isinstance(hour, dict) else asdict(hour)  # type: ignore[call-overload]
        start = int(values["hour_start"])
        if tomorrow <= start < after:
            w = fit_.expected_w(start, fit_.sun_of(values))
            if w is not None:
                rows.append((start, fetch_id, w))
    with conn:
        conn.executemany("INSERT OR REPLACE INTO solar_expected (hour_start, fetch_id, expected_w) VALUES (?, ?, ?)", rows)


def day_ahead(conn: sqlite3.Connection, start: int, end: int) -> dict[int, float]:
    return {
        int(hour): float(w)
        for hour, w in conn.execute(
            "SELECT hour_start, expected_w FROM solar_expected WHERE hour_start >= ? AND hour_start < ?", (start, end)
        )
    }


def _error(pairs: list[tuple[float, float]]) -> tuple[float, float] | None:
    """(error %, bias %): how far off, and which way, as shares of what was made."""
    made = sum(a for _, a in pairs)
    if made <= 0:
        return None
    return 100 * sum(abs(e - a) for e, a in pairs) / made, 100 * sum(e - a for e, a in pairs) / made


def score(conn: sqlite3.Connection, now: int, days: int = SCORE_DAYS) -> dict[str, object]:
    """Each whole day of the last `days` that had a day-ahead expectation for every hour, beside
    what was made: per day from the inverter's counters, per hour from the readings."""
    today = day_start(now)
    start = today
    for _ in range(days):
        start = day_start(start - HOUR)
    expected = day_ahead(conn, start, today)
    made_by_day = {day: kwh for day, counter, kwh in db.daily_totals(conn) if counter == "pv"}
    made_by_hour = hourly_made(conn, start, today)
    scored: list[dict[str, object]] = []
    hour_pairs: list[tuple[float, float]] = []
    day = start
    while day < today:
        end = next_day_start(day)
        hours = range(day, end, HOUR)
        made_kwh = made_by_day.get(local_date(day))
        if made_kwh is not None and all(h in expected for h in hours):
            scored.append({"day": local_date(day), "expected_kwh": sum(expected[h] for h in hours) / 1000, "made_kwh": made_kwh})
            hour_pairs += [(expected[h], made_by_hour[h]) for h in hours if h in made_by_hour]
        day = end
    by_day = _error([(float(d["expected_kwh"]), float(d["made_kwh"])) for d in scored])  # type: ignore[arg-type]
    by_hour = _error(hour_pairs)
    return {
        "days": scored,
        "day_error_pct": by_day[0] if by_day else None,
        "bias_pct": by_day[1] if by_day else None,
        "hour_error_pct": by_hour[0] if by_hour else None,
    }


def summary(conn: sqlite3.Connection, now: int, forecast_hours: list[dict[str, object]] | None) -> dict[str, object]:
    """For /api/weather: the fit, the day-ahead expectations for yesterday and today, how they've
    done, and expected_w added to each forecast hour."""
    fit_ = load_fit(conn)
    if fit_ is None:
        return {"status": "none"}
    for hour in forecast_hours or []:
        hour["expected_w"] = fit_.expected_w(int(hour["hour_start"]), fit_.sun_of(hour))  # type: ignore[call-overload]
    today = day_start(now)
    yesterday = day_start(today - HOUR)
    scores = score(conn, now)
    last = scores["days"][-1] if scores["days"] and scores["days"][-1]["day"] == local_date(yesterday) else None  # type: ignore[index]
    return {
        "status": fit_.status,
        "basis": fit_.basis,
        "ratio": fit_.ratio,
        "ceiling_w": fit_.ceiling_w,
        "hours": fit_.hours,
        "days": fit_.days,
        "ready_days": READY_DAYS,
        "fitted_at": fit_.fitted_at,
        "day_ahead": [{"hour_start": h, "expected_w": w} for h, w in sorted(day_ahead(conn, yesterday, next_day_start(today)).items())],
        "yesterday": last,
        "score": {key: scores[key] for key in ("day_error_pct", "bias_pct", "hour_error_pct")} | {"days": len(scores["days"])},  # type: ignore[arg-type]
    }
