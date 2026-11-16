"""How the solar output now compares with what a clear sky would give: "% of clear sky".

The sunshine a cloudless sky would put on the panels comes from where the sun is and a simple
clear-sky model (Ineichen-Perez at sea level, with a Linke turbidity of 3, typical of a clean
temperate sky), split into the sun's beam, the sky's even glow and light off the ground. That
sunshine is turned into watts by learning, from the log, the most each string has made per W/m² of
it: the 98th percentile of 10-minute means over the last month, with enough sun to tell. So 100%
means as good as these panels have done lately in that sunshine, not a physical limit, and a cold
bright day with some cloud edge can go over it.

With panels facing two ways, each string is matched to a facing by how its share of the output
follows that facing's share of the sunshine through the day. Without per-string readings, or when
the match isn't clear, the total is compared with the sunshine averaged over the facings.

The fit is redone once a night (see weather/service.py) and kept in clear_sky_fit.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from solar_monitor import db
from solar_monitor.weather import sun
from solar_monitor.weather.base import Location

DAY = 86400
FIT_DAYS = 30
BUCKET = 600  # Seconds averaged together, so a passing cloud's edge doesn't count as the best.
BUCKET_SAMPLES = 5  # A bucket needs this many of its one-a-minute samples.
LINKE = 3.0
ALBEDO = 0.2
SOLAR_CONSTANT = 1361.0  # W/m² above the air, at the mean distance from the sun.
LOW_SUN = 0.05  # Below this cosine of the sun's angle from overhead, about 3° up, it counts as none.
MIN_SUNSHINE = 100.0  # W/m² on the panels: less is mostly glow off the sky, too weak to tell by.
MIN_STRING_W = 50.0  # A string that has never made this much in a bucket isn't connected.
MIN_BUCKETS = 24  # Buckets with enough sunshine that a factor needs: 4 hours.
ROUGH_BUCKETS = 200  # With fewer, a factor is still rough: there have been few chances for its best.
MIN_MATCH = 0.3  # How closely a string's share must follow a facing's for the match to count.
QUANTILE = 0.98
STRINGS = ("ppv1", "ppv2", "ppv3", "ppv4")
TOTAL = "ppv"


@dataclass(frozen=True)
class Sky:
    """A clear sky's sunshine at a moment: the sun's position in degrees and W/m² on flat ground,
    in the sun's beam and from the rest of the sky."""

    elevation: float
    azimuth: float
    ghi: float
    dni: float
    dhi: float


def sky(lat: float, lon: float, ts: float) -> Sky:
    elevation, azimuth = sun.position(lat, lon, ts)
    cz = math.sin(math.radians(elevation))
    if cz <= LOW_SUN:
        return Sky(elevation, azimuth, 0.0, 0.0, 0.0)
    zenith = 90 - elevation
    air_mass = 1 / (cz + 0.50572 * (96.07995 - zenith) ** -1.6364)  # Kasten and Young.
    day_of_year = datetime.fromtimestamp(ts, UTC).timetuple().tm_yday
    i0 = SOLAR_CONSTANT * (1 + 0.033 * math.cos(2 * math.pi * day_of_year / 365))  # Nearest the sun in January.
    ghi = 0.868 * i0 * cz * math.exp(-0.0387 * air_mass * LINKE)
    dni = min(
        0.827 * i0 * math.exp(-0.09 * air_mass * (LINKE - 1)),
        (1 - (0.1 - 0.2 * math.exp(-LINKE)) / (0.1 + 0.882)) / cz * ghi,
    )
    dni = max(0.0, min(dni, ghi / cz))
    return Sky(elevation, azimuth, ghi, dni, ghi - dni * cz)


def on_panels(s: Sky, tilt: float | None, facing: float | None) -> float:
    """W/m² on panels at this tilt and compass facing: flat ground's without a tilt, south without a facing."""
    if s.ghi <= 0:
        return 0.0
    if tilt is None:
        return s.ghi
    t = math.radians(tilt)
    zenith = math.radians(90 - s.elevation)
    incidence = math.cos(zenith) * math.cos(t) + math.sin(zenith) * math.sin(t) * math.cos(
        math.radians(s.azimuth - (180.0 if facing is None else facing))
    )
    return max(incidence, 0.0) * s.dni + s.dhi * (1 + math.cos(t)) / 2 + ALBEDO * s.ghi * (1 - math.cos(t)) / 2


def facings_of(where: Location) -> list[float | None]:
    return [where.panel_facing] + ([] if where.panel_facing_2 is None else [where.panel_facing_2])


def sunshine(where: Location, s: Sky, facings: Sequence[float | None]) -> float:
    """The mean W/m² on panels with these facings."""
    return sum(on_panels(s, where.panel_tilt, f) for f in facings) / len(facings)


@dataclass(frozen=True)
class Term:
    """Readings summed, compared with the sunshine averaged over these facings, at this many W per W/m²."""

    readings: tuple[str, ...]
    facings: tuple[float | None, ...]
    factor: float
    buckets: int


@dataclass(frozen=True)
class Calibration:
    where: Location
    terms: tuple[Term, ...]
    days: int
    fitted_at: int

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> Calibration:
        data = json.loads(text)
        terms = tuple(
            Term(tuple(t["readings"]), tuple(t["facings"]), float(t["factor"]), int(t["buckets"])) for t in data["terms"]
        )
        return cls(Location(**data["where"]), terms, int(data["days"]), int(data["fitted_at"]))


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def _correlation(a: list[float], b: list[float]) -> float:
    n = len(a)
    if n < 3:
        return 0.0
    ma, mb = sum(a) / n, sum(b) / n
    sa = math.sqrt(sum((x - ma) ** 2 for x in a))
    sb = math.sqrt(sum((y - mb) ** 2 for y in b))
    if sa == 0 or sb == 0:
        return 0.0
    return sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True)) / (sa * sb)


@dataclass(frozen=True)
class _Bucket:
    start: int
    means: dict[str, float]  # reading -> mean W
    sun: tuple[float, ...]  # W/m² on each facing, at the middle of the bucket


def _buckets(rows: Iterable[tuple[int, dict[str, object]]], where: Location) -> list[_Bucket]:
    sums: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    counts: dict[int, int] = defaultdict(int)
    for ts, values in rows:
        start = ts // BUCKET * BUCKET
        counts[start] += 1
        for reading in (*STRINGS, TOTAL):
            value = values.get(reading)
            if isinstance(value, (int, float)):
                sums[start][reading] += value
    facings = facings_of(where)
    found = []
    for start in sorted(counts):
        n = counts[start]
        if n < BUCKET_SAMPLES:
            continue
        s = sky(where.latitude, where.longitude, start + BUCKET / 2)
        found.append(
            _Bucket(start, {r: v / n for r, v in sums[start].items()}, tuple(on_panels(s, where.panel_tilt, f) for f in facings))
        )
    return found


def _term(buckets: list[_Bucket], readings: tuple[str, ...], facings: tuple[int, ...], where: Location) -> Term | None:
    ratios = []
    for b in buckets:
        sun_wm2 = sum(b.sun[i] for i in facings) / len(facings)
        if sun_wm2 >= MIN_SUNSHINE and all(r in b.means for r in readings):
            ratios.append(sum(b.means[r] for r in readings) / sun_wm2)
    if len(ratios) < MIN_BUCKETS:
        return None
    all_facings = facings_of(where)
    return Term(readings, tuple(all_facings[i] for i in facings), _quantile(ratios, QUANTILE), len(ratios))


def _matched(buckets: list[_Bucket], strings: list[str], facing_count: int) -> dict[str, int] | None:
    """Each string's facing, by how its share of the output follows that facing's share of the sunshine."""
    lit = [b for b in buckets if sum(b.means[s] for s in strings) > MIN_STRING_W and sum(b.sun) > MIN_SUNSHINE / 2]
    if len(lit) < MIN_BUCKETS:
        return None
    matched = {}
    for s in strings:
        share = [b.means[s] / sum(b.means[x] for x in strings) for b in lit]
        scores = [_correlation(share, [b.sun[i] / sum(b.sun) for b in lit]) for i in range(facing_count)]
        best = max(range(facing_count), key=lambda i: scores[i])
        if scores[best] < MIN_MATCH:
            return None
        matched[s] = best
    if len(set(matched.values())) < facing_count:
        return None  # A facing with no string of its own: the match is most likely wrong.
    return matched


def fit(rows: Iterable[tuple[int, dict[str, object]]], where: Location, now: int) -> Calibration | None:
    """Learn the factors from samples of (ts, readings), or None if there isn't enough to go on."""
    buckets = _buckets(rows, where)
    facing_count = len(facings_of(where))
    strings = [s for s in STRINGS if any(b.means.get(s, 0.0) >= MIN_STRING_W for b in buckets)]
    buckets = [b for b in buckets if all(s in b.means for s in strings)]
    terms: list[Term] = []
    matched = _matched(buckets, strings, facing_count) if facing_count > 1 and len(strings) >= facing_count else None
    if matched is not None:
        for i in range(facing_count):
            term = _term(buckets, tuple(s for s in strings if matched[s] == i), (i,), where)
            if term is None:
                terms = []
                break
            terms.append(term)
    if not terms:
        readings = tuple(strings) if strings else (TOTAL,)
        term = _term(buckets, readings, tuple(range(facing_count)), where)
        if term is None:
            return None
        terms = [term]
    days = len({b.start // DAY for b in buckets if any(b.sun)})
    return Calibration(where, tuple(terms), days, now)


def read(conn: sqlite3.Connection, now: int) -> list[tuple[int, bytes]]:
    """One sample a minute with some output, from the last FIT_DAYS, still compressed."""
    return conn.execute(
        "SELECT MIN(ts), raw FROM samples WHERE ts >= ? AND ts < ? AND pv_w > 0 AND raw IS NOT NULL GROUP BY ts / 60",
        (now - FIT_DAYS * DAY, now),
    ).fetchall()


def decode(rows: list[tuple[int, bytes]]) -> list[tuple[int, dict[str, object]]]:
    return [(int(ts), db.decode_raw(raw)) for ts, raw in rows]


def save(conn: sqlite3.Connection, calibration: Calibration | None) -> None:
    with conn:
        conn.execute("DELETE FROM clear_sky_fit")
        if calibration is not None:
            conn.execute(
                "INSERT INTO clear_sky_fit (id, fitted_at, fit) VALUES (1, ?, ?)",
                (calibration.fitted_at, calibration.to_json()),
            )


def load(conn: sqlite3.Connection, where: Location | None) -> Calibration | None:
    """The kept fit, if it was for this location and these panels."""
    row = conn.execute("SELECT fit FROM clear_sky_fit WHERE id = 1").fetchone()
    if row is None or where is None:
        return None
    try:
        calibration = Calibration.from_json(row[0])
    except (ValueError, TypeError, KeyError):  # From a version that kept something else.
        return None
    return calibration if calibration.where == where else None


def now(calibration: Calibration | None, ts: float, readings: dict[str, object]) -> dict[str, object] | None:
    """What a clear sky would give now, and today's output as a percentage of it, or None at night,
    with the sun too low to tell, or before there's a fit."""
    if calibration is None:
        return None
    where = calibration.where
    s = sky(where.latitude, where.longitude, ts)
    parts = []
    for term in calibration.terms:
        sun_wm2 = sunshine(where, s, term.facings)
        made = [readings.get(r) for r in term.readings]
        if not all(isinstance(m, (int, float)) for m in made):
            return None
        parts.append((sun_wm2, term.factor * sun_wm2, sum(made)))  # type: ignore[arg-type]
    if all(sun_wm2 < MIN_SUNSHINE for sun_wm2, _, _ in parts):
        return None
    clear_w = sum(c for _, c, _ in parts)
    made_w = sum(m for _, _, m in parts)
    return {
        "clear_w": round(clear_w),
        "made_w": round(made_w),
        "pct": round(100 * made_w / clear_w) if clear_w > 0 else None,
        "parts": [
            {"readings": list(term.readings), "facings": list(term.facings), "clear_w": round(c), "made_w": round(m)}
            for term, (_, c, m) in zip(calibration.terms, parts, strict=True)
        ],
        "days": calibration.days,
    }


def summary(calibration: Calibration | None) -> dict[str, object] | None:
    """What the kept fit was learned from, for the page."""
    if calibration is None:
        return None
    return {
        "days": calibration.days,
        "fitted_at": calibration.fitted_at,
        "parts": [
            {"readings": list(t.readings), "facings": list(t.facings), "rough": t.buckets < ROUGH_BUCKETS}
            for t in calibration.terms
        ],
    }
