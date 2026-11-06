"""SQLite storage: one row per poll in `samples`, one row per day and counter in `daily_counters`."""

from __future__ import annotations

import json
import sqlite3
import zlib
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts          INTEGER PRIMARY KEY,  -- Unix seconds from this machine's clock, not the inverter's
    pv_w        REAL,                 -- ppv
    load_w      REAL,                 -- load_ptotal
    battery_w   REAL,                 -- pbattery1, as reported: negative means charging
    battery_soc REAL,                 -- battery_soc, percent
    grid_w      REAL,                 -- active_power_total, as reported: positive is believed
                                      -- to mean exporting, not yet confirmed by a large flow
    raw         BLOB NOT NULL         -- every reading: zlib-compressed JSON, see decode_raw()
);

CREATE TABLE IF NOT EXISTS daily_counters (
    day         TEXT NOT NULL,        -- local date, YYYY-MM-DD
    counter     TEXT NOT NULL,        -- a key of COUNTERS
    first_value REAL NOT NULL,        -- lifetime kWh at the day's first reading
    last_value  REAL NOT NULL,        -- lifetime kWh at the day's latest reading
    PRIMARY KEY (day, counter)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS appliances (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    typical_w     REAL NOT NULL,
    peak_w        REAL,                 -- the most it draws once running, if more than typical
    surge_w       REAL,                 -- the brief draw while starting, if known
    use_in_outage INTEGER NOT NULL DEFAULT 0,
    notes         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,             -- a key of SETTINGS
    value REAL NOT NULL
) WITHOUT ROWID;
"""

# Our name -> the inverter's lifetime kWh reading. Lifetime rather than "today" readings,
# so that a gap in polling is caught up by the next reading instead of being lost.
COUNTERS = {
    "pv": "e_total",
    "load": "e_load_total",
    "grid_import": "meter_e_total_imp",
    "grid_export": "meter_e_total_exp",
    "battery_charge": "e_bat_charge_total",
    "battery_discharge": "e_bat_discharge_total",
    # Despite their names these measure the inverter's AC side, not the grid.
    "inverter_ac_in": "e_total_imp",
    "inverter_ac_out": "e_total_exp",
}

# Column in `samples` -> the inverter reading it holds.
READINGS = {
    "pv_w": "ppv",
    "load_w": "load_ptotal",
    "battery_w": "pbattery1",
    "battery_soc": "battery_soc",
    "grid_w": "active_power_total",
}

# A chart can't usefully show more points than this, so longer ranges are averaged more.
MAX_HISTORY_POINTS = 800
BUCKET_SECONDS = (60, 120, 300, 900, 1800, 3600, 7200, 21600, 86400)

# Planning choices the inverter can't report. An unset one isn't stored, and reads as None.
SETTINGS = ("capacity_kwh", "target_soc", "limit_amps", "surge_limit_w")

_APPLIANCE_FIELDS = ("name", "typical_w", "peak_w", "surge_w", "use_in_outage", "notes")


@dataclass(frozen=True)
class Sample:
    ts: int
    values: dict[str, object]

    @property
    def day(self) -> str:
        return datetime.fromtimestamp(self.ts).date().isoformat()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")  # Safe from corruption under WAL, with fewer writes.
    conn.executescript(SCHEMA)
    return conn


def encode_raw(values: dict[str, object]) -> bytes:
    return zlib.compress(json.dumps(values, separators=(",", ":"), default=str).encode())


def decode_raw(raw: bytes) -> dict[str, object]:
    return json.loads(zlib.decompress(raw))


def _number(value: object) -> float | None:
    return value if isinstance(value, (int, float)) else None


def _columns(sample: Sample) -> tuple[object, ...]:
    return (sample.ts, *(_number(sample.values.get(reading)) for reading in READINGS.values()))


def save_samples(conn: sqlite3.Connection, samples: Iterable[Sample]) -> None:
    """Write a batch in one transaction, so it's either all saved or safe to retry."""
    samples = list(samples)
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO samples (ts, pv_w, load_w, battery_w, battery_soc, grid_w, raw) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(*_columns(s), encode_raw(s.values)) for s in samples],
        )
        conn.executemany(
            "INSERT INTO daily_counters (day, counter, first_value, last_value) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT (day, counter) DO UPDATE SET last_value = excluded.last_value",
            [
                (s.day, name, value, value)
                for s in samples
                for name, reading in COUNTERS.items()
                if (value := _number(s.values.get(reading))) is not None
            ],
        )


def daily_totals(conn: sqlite3.Connection) -> list[tuple[str, str, float]]:
    """(day, counter, kWh) rows, oldest first.

    A day's energy runs from the previous recorded day's last reading, so energy from
    days with no polling lands on the next day that has some. Only the first day ever
    recorded starts from its own first reading. A lifetime counter that drops, as when
    an inverter is replaced, counts as zero that day rather than as negative energy.
    """
    return conn.execute(
        """
        SELECT day, counter, ROUND(MAX(0, last_value - COALESCE(
            LAG(last_value) OVER (PARTITION BY counter ORDER BY day),
            first_value
        )), 3)
        FROM daily_counters
        ORDER BY day, counter
        """
    ).fetchall()


def daily_series(conn: sqlite3.Connection, days: int, today: date | None = None) -> list[dict[str, object]]:
    """The last `days` calendar days, oldest first, as {"day": ..., counter: kWh, ...}.

    A day with no readings has only its "day", so a gap in logging stays visible.
    """
    totals: dict[str, dict[str, float]] = defaultdict(dict)
    for day, counter, kwh in daily_totals(conn):  # All days: each total needs the day before it.
        totals[day][counter] = kwh
    last = today or date.today()
    calendar = (last - timedelta(days=back) for back in reversed(range(days)))
    return [{"day": day.isoformat(), **totals.get(day.isoformat(), {})} for day in calendar]


def bucket_seconds(span_seconds: int) -> int:
    """The shortest averaging period that keeps a range within MAX_HISTORY_POINTS."""
    return next((b for b in BUCKET_SECONDS if span_seconds / b <= MAX_HISTORY_POINTS), BUCKET_SECONDS[-1])


def history(
    conn: sqlite3.Connection, start: int, end: int, pending: Iterable[Sample] = ()
) -> tuple[int, list[dict[str, object]]]:
    """(bucket seconds, averaged points) for start <= ts < end, oldest first.

    `pending` is readings not saved yet, so the curve reaches the present. A bucket with
    no readings returns no point, so a gap in logging stays a gap. Averaging signed power
    means a charge and a discharge within one bucket partly cancel out.
    """
    bucket = bucket_seconds(end - start)
    waiting = [_columns(s) for s in pending if start <= s.ts < end]
    averages = ", ".join(f"ROUND(AVG({column}), 1)" for column in READINGS)
    columns = ", ".join(READINGS)
    from_json = ", ".join(f"json_extract(value, '$[{i}]')" for i in range(len(READINGS) + 1))
    rows = conn.execute(
        f"""
        SELECT ts / :bucket * :bucket AS bucket_start, {averages}
        FROM (
            SELECT ts, {columns} FROM samples WHERE ts >= :start AND ts < :end
            UNION ALL
            SELECT {from_json} FROM json_each(:waiting)
        )
        GROUP BY bucket_start
        ORDER BY bucket_start
        """,
        {"bucket": bucket, "start": start, "end": end, "waiting": json.dumps(waiting)},
    ).fetchall()
    return bucket, [dict(zip(("ts", *READINGS), row)) for row in rows]


def battery_samples(
    conn: sqlite3.Connection, start: int, pending: Iterable[Sample] = ()
) -> list[tuple[int, float | None, float | None]]:
    """(ts, battery W, SOC) from `start` on, oldest first, including readings not saved yet."""
    saved = conn.execute(
        "SELECT ts, battery_w, battery_soc FROM samples WHERE ts >= ? ORDER BY ts", (start,)
    ).fetchall()
    waiting = [
        (s.ts, _number(s.values.get(READINGS["battery_w"])), _number(s.values.get(READINGS["battery_soc"])))
        for s in pending
        if s.ts >= start
    ]
    return saved + waiting


def get_settings(conn: sqlite3.Connection) -> dict[str, float | None]:
    stored = dict(conn.execute("SELECT key, value FROM settings"))
    return {key: stored.get(key) for key in SETTINGS}


def save_settings(conn: sqlite3.Connection, settings: dict[str, float | None]) -> None:
    """Replace every setting. One left out or None goes back to being unset."""
    with conn:
        conn.execute("DELETE FROM settings")
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?)",
            [(key, settings[key]) for key in SETTINGS if settings.get(key) is not None],
        )


def _appliance(row: tuple[object, ...]) -> dict[str, object]:
    appliance = dict(zip(("id", *_APPLIANCE_FIELDS), row))
    appliance["use_in_outage"] = bool(appliance["use_in_outage"])
    return appliance


def list_appliances(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(f"SELECT id, {', '.join(_APPLIANCE_FIELDS)} FROM appliances ORDER BY id").fetchall()
    return [_appliance(row) for row in rows]


def add_appliance(conn: sqlite3.Connection, appliance: dict[str, object]) -> int:
    with conn:
        cursor = conn.execute(
            f"INSERT INTO appliances ({', '.join(_APPLIANCE_FIELDS)}) "
            f"VALUES ({', '.join('?' * len(_APPLIANCE_FIELDS))})",
            [appliance[field] for field in _APPLIANCE_FIELDS],
        )
    return cursor.lastrowid


def update_appliance(conn: sqlite3.Connection, appliance_id: int, appliance: dict[str, object]) -> bool:
    """False if there's no such appliance."""
    assignments = ", ".join(f"{field} = ?" for field in _APPLIANCE_FIELDS)
    with conn:
        cursor = conn.execute(
            f"UPDATE appliances SET {assignments} WHERE id = ?",
            [*(appliance[field] for field in _APPLIANCE_FIELDS), appliance_id],
        )
    return cursor.rowcount > 0


def delete_appliance(conn: sqlite3.Connection, appliance_id: int) -> bool:
    """False if there's no such appliance."""
    with conn:
        cursor = conn.execute("DELETE FROM appliances WHERE id = ?", (appliance_id,))
    return cursor.rowcount > 0
