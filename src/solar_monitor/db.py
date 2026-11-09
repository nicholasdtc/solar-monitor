"""SQLite storage: one row per poll in `samples`, one row per day and counter in `daily_counters`."""

from __future__ import annotations

import json
import math
import sqlite3
import time
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
    grid_w      REAL,                 -- active_power_total, as reported: positive means exporting,
                                      -- confirmed by a large import, see sems-delay-findings.md
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

CREATE TABLE IF NOT EXISTS inverter_writes (
    id          INTEGER PRIMARY KEY,
    ts          INTEGER NOT NULL,       -- Unix seconds, from just before it was sent
    slot        INTEGER NOT NULL,       -- which TOU period, as numbered in SEMS+
    old_percent INTEGER NOT NULL,
    new_percent INTEGER NOT NULL,
    old_bytes   TEXT NOT NULL,          -- the period in hex, as read just before: what undo goes back to
    new_bytes   TEXT NOT NULL,          -- what was sent, in hex
    outcome     TEXT NOT NULL,          -- sent (nothing recorded since), confirmed, not_applied, mismatch or unknown
    held_bytes  TEXT                    -- what the read-back found, in hex
);

-- Changes to one of the inverter's single-number settings: Backup mode and the battery's limits.
CREATE TABLE IF NOT EXISTS setting_writes (
    id         INTEGER PRIMARY KEY,
    ts         INTEGER NOT NULL,        -- Unix seconds, from just before it was sent
    setting    TEXT NOT NULL,           -- a key of control.SETTINGS, such as min_soc
    old_value  INTEGER NOT NULL,        -- as read just before: what undo goes back to
    new_value  INTEGER NOT NULL,        -- what was sent
    outcome    TEXT NOT NULL,           -- as in inverter_writes
    held_value INTEGER                  -- what the read-back found
);

-- The electricity meter's half-hours, imported from ESB Networks. Kept by the meter's own labels,
-- in Irish clock time, so nothing is lost or merged when the clocks change. Each label is when the
-- half-hour ends: slot 0 is the one ending at midnight, so it belongs to the day before.
CREATE TABLE IF NOT EXISTS meter_readings (
    day        TEXT NOT NULL,           -- YYYY-MM-DD, on the meter's clock
    slot       INTEGER NOT NULL,        -- the label's half-hour from midnight, 0 to 47
    occurrence INTEGER NOT NULL,        -- 0, or 1 for the second half-hour with this label when the
                                        -- clocks go back. ESB lists the newest first, so 1 is the earlier.
    kw         REAL NOT NULL,           -- the average kW taken from the grid over the half-hour
    PRIMARY KEY (day, slot, occurrence)
) WITHOUT ROWID;

-- How the page shows things, as chosen in its Settings: theme, date format and so on. Kept here
-- rather than in the browser, so the choices follow you to every device.
CREATE TABLE IF NOT EXISTS display_prefs (
    name  TEXT PRIMARY KEY,             -- a field of inputs.DisplayPrefsIn
    value TEXT NOT NULL
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
# The most a chart that asks for its own bucket can fetch at once, which is a few screens' worth.
MAX_BUCKETS_PER_REQUEST = 2000
# Power readings whose lowest and highest in each period come with the average, so a chart
# can show the spikes that averaging flattens. Charge is slow enough not to need it.
SPREAD_READINGS = ("pv_w", "load_w", "battery_w", "grid_w")

# A reading stands for the power until the next one, but never for longer than this, so a
# dropout of the inverter's Wi-Fi isn't filled with the last power it reported.
MAX_READING_SECONDS = 60

# Planning choices the inverter can't report. An unset one isn't stored, and reads as None.
SETTINGS = (
    "capacity_kwh",
    "limit_amps",
    "surge_limit_w",
    "outage_hours",  # the length of power cut to plan for
    "run_until_minutes",  # the clock time to run until, in minutes after midnight
    "tariff_day",  # the tariffs are per kWh, in whatever money unit they were entered
    "tariff_peak",
    "tariff_night",
    "tariff_export",
    # When the tariff's bands start and end, in minutes after midnight. Night may run past midnight.
    "band_night_start_minutes",
    "band_night_end_minutes",
    "band_peak_start_minutes",
    "band_peak_end_minutes",
)

# Settings older versions kept. They're removed on start, and skipped quietly in an import.
RETIRED_SETTINGS = (
    "target_soc",  # The charge target, now read from the inverter instead.
)

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
    with conn:
        conn.executemany("DELETE FROM settings WHERE key = ?", [(key,) for key in RETIRED_SETTINGS])
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


def _utc_offset(ts: int) -> int:
    return time.localtime(ts).tm_gmtoff


def bucket_start(ts: int, bucket: int) -> int:
    """The start of the averaging period that holds `ts`.

    Periods follow this machine's clock, so an hour starts on the hour and a day at midnight,
    including on the days the clocks change.
    """
    local = (ts + _utc_offset(ts)) // bucket * bucket
    # The offset at the period's start, which on the day the clocks change isn't the one at `ts`.
    return local - _utc_offset(local - _utc_offset(ts))


def bucket_end(ts: int, bucket: int) -> int:
    """The start of the period after the one that holds `ts`. Half a period past the end of this
    one is always in the next, even when the clocks change during it."""
    return bucket_start(bucket_start(ts, bucket) + bucket * 3 // 2, bucket)


def whole_buckets(start: int, end: int, bucket: int) -> tuple[int, int]:
    """The range widened to whole periods, so none at its edges averages only part of its readings."""
    return bucket_start(start, bucket), bucket_end(end - 1, bucket)


def first_reading(conn: sqlite3.Connection, pending: Iterable[Sample] = ()) -> int | None:
    """When the oldest reading was taken, saved or not, or None before the first."""
    saved = conn.execute("SELECT MIN(ts) FROM samples").fetchone()[0]
    return min((ts for ts in (saved, *(s.ts for s in pending)) if ts is not None), default=None)


def history(
    conn: sqlite3.Connection, start: int, end: int, pending: Iterable[Sample] = (), bucket: int | None = None
) -> tuple[int, list[dict[str, object]]]:
    """(bucket seconds, averaged points) for start <= ts < end, oldest first.

    The bucket is chosen to suit the range unless given. Each point's "ts" is the start of
    its period, see bucket_start(). `pending` is readings not saved yet, so the curve reaches
    the present. A bucket with no readings returns no point, so a gap in logging stays a gap.
    Averaging signed power means a charge and a discharge within one bucket partly cancel out,
    which is why the power readings also come with their lowest and highest, as "<name>_min"
    and "<name>_max".
    """
    bucket = bucket or bucket_seconds(end - start)
    # SQLite adds up the readings in steps of UTC time, which can't cross the start of a
    # period because clocks only ever differ by whole quarter hours. Then each period adds
    # up its steps here, since only Python knows when the local clock changed.
    step = math.gcd(bucket, 900)
    figures = ", ".join([
        *(f"SUM({column}), COUNT({column})" for column in READINGS),
        *(f"MIN({column}), MAX({column})" for column in SPREAD_READINGS),
    ])
    rows = conn.execute(
        f"""
        SELECT ts / :step * :step AS step_start, {figures}
        FROM ({_readings_with_pending()})
        GROUP BY step_start
        ORDER BY step_start
        """,
        {"step": step, "start": start, "end": end, "waiting": _waiting(pending, start, end)},
    ).fetchall()
    periods: dict[int, list[object]] = {}
    following = None
    for step_start, *figures in rows:
        if following is None or step_start >= following:
            begins, following = bucket_start(step_start, bucket), bucket_end(step_start, bucket)
            periods[begins] = figures
        else:
            periods[begins] = _add_steps(periods[begins], figures)
    return bucket, [_point(begins, figures) for begins, figures in periods.items()]


_SUMS = 2 * len(READINGS)  # Each reading's sum and count come first, then each spread's lowest and highest.


def _add_steps(totals: list[object], step: list[object]) -> list[object]:
    # SQLite's SUM is NULL when every reading was missing, while its COUNT is 0.
    sums = [(a or 0) + (b or 0) for a, b in zip(totals[:_SUMS], step[:_SUMS])]
    edges = []
    for i, pair in enumerate(zip(totals[_SUMS:], step[_SUMS:])):
        known = [value for value in pair if value is not None]
        edges.append((max if i % 2 else min)(known) if known else None)
    return [*sums, *edges]


def _point(begins: int, figures: list[object]) -> dict[str, object]:
    sums = figures[:_SUMS]
    point: dict[str, object] = {"ts": begins}
    for i, name in enumerate(READINGS):
        total, count = sums[2 * i], sums[2 * i + 1]
        point[name] = round(total / count, 1) if count else None
    for i, name in enumerate(SPREAD_READINGS):
        point[f"{name}_min"], point[f"{name}_max"] = figures[_SUMS + 2 * i], figures[_SUMS + 2 * i + 1]
    return point


def _waiting(pending: Iterable[Sample], start: int, end: int) -> str:
    return json.dumps([_columns(s) for s in pending if start <= s.ts < end])


def _readings_with_pending() -> str:
    """Saved readings from :start to before :end, and the unsaved ones passed as :waiting."""
    columns = ", ".join(READINGS)
    from_json = ", ".join(f"json_extract(value, '$[{i}]')" for i in range(len(READINGS) + 1))
    return f"""
        SELECT ts, {columns} FROM samples WHERE ts >= :start AND ts < :end
        UNION ALL
        SELECT {from_json} FROM json_each(:waiting)
    """


# Where each day's solar went and where the house's power came from, in kWh.
FLOWS = ("solar_home", "solar_battery", "solar_export", "battery_home", "grid_home")


def daily_flows(
    conn: sqlite3.Connection, start: int, end: int, pending: Iterable[Sample] = ()
) -> dict[str, dict[str, float]]:
    """{local day: {flow: kWh}} worked out reading by reading, for start <= ts < end.

    The inverter only counts totals, so this splits them: solar goes to the house first,
    then to charging the battery, then to the grid, and the house takes what solar can't
    give from the battery first, then from the grid. Each part is capped by what was
    actually charged, discharged, exported or imported, so conversion losses aren't
    counted as anything. Battery and grid signs are as stored: positive battery is
    discharging, positive grid is exporting.
    """
    rows = conn.execute(
        f"""
        WITH readings AS (
            SELECT ts,
                MAX(COALESCE(pv_w, 0), 0) AS pv,
                MAX(COALESCE(load_w, 0), 0) AS load,
                MAX(COALESCE(-battery_w, 0), 0) AS charge,
                MAX(COALESCE(battery_w, 0), 0) AS discharge,
                MAX(COALESCE(grid_w, 0), 0) AS export,
                MAX(COALESCE(-grid_w, 0), 0) AS import,
                MIN(COALESCE(LEAD(ts) OVER (ORDER BY ts) - ts, 0), :longest) AS seconds
            FROM ({_readings_with_pending()})
        ),
        home AS (SELECT *, MIN(pv, load) AS solar_home FROM readings),
        split AS (
            SELECT *,
                MIN(pv - solar_home, charge) AS solar_battery,
                MIN(discharge, load - solar_home) AS battery_home
            FROM home
        )
        SELECT date(ts, 'unixepoch', 'localtime') AS day,
            SUM(solar_home * seconds),
            SUM(solar_battery * seconds),
            SUM(MIN(pv - solar_home - solar_battery, export) * seconds),
            SUM(battery_home * seconds),
            SUM(MIN(load - solar_home - battery_home, import) * seconds)
        FROM split
        GROUP BY day
        """,
        {"start": start, "end": end, "waiting": _waiting(pending, start, end), "longest": MAX_READING_SECONDS},
    ).fetchall()
    joules_per_kwh = 3_600_000
    return {day: {name: round(joules / joules_per_kwh, 3) for name, joules in zip(FLOWS, sums)} for day, *sums in rows}


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


def overhead_samples(
    conn: sqlite3.Connection, start: int
) -> list[tuple[float | None, float | None, float | None, float | None]]:
    """(solar, house, battery, grid) W of every saved sample from `start` on."""
    return conn.execute("SELECT pv_w, load_w, battery_w, grid_w FROM samples WHERE ts >= ?", (start,)).fetchall()


def get_settings(conn: sqlite3.Connection) -> dict[str, float | None]:
    stored = dict(conn.execute("SELECT key, value FROM settings"))
    return {key: stored.get(key) for key in SETTINGS}


def save_settings(conn: sqlite3.Connection, settings: dict[str, float | None]) -> None:
    """Change only the settings given, so each form sends just its own. None makes one unset."""
    given = [key for key in SETTINGS if key in settings]
    with conn:
        conn.executemany("DELETE FROM settings WHERE key = ?", [(key,) for key in given])
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?)",
            [(key, settings[key]) for key in given if settings[key] is not None],
        )


def get_display_prefs(conn: sqlite3.Connection) -> dict[str, str]:
    return dict(conn.execute("SELECT name, value FROM display_prefs ORDER BY name"))


def save_display_prefs(conn: sqlite3.Connection, prefs: dict[str, str]) -> None:
    """Replace the whole set, so a choice put back to its default is forgotten too."""
    with conn:
        conn.execute("DELETE FROM display_prefs")
        conn.executemany("INSERT INTO display_prefs (name, value) VALUES (?, ?)", prefs.items())


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


def add_inverter_write(
    conn: sqlite3.Connection, slot: int, old_bytes: str, new_bytes: str, old_percent: int, new_percent: int
) -> int:
    """Called before a write is sent, so it's on record whatever happens next."""
    with conn:
        cursor = conn.execute(
            "INSERT INTO inverter_writes (ts, slot, old_percent, new_percent, old_bytes, new_bytes, outcome) "
            "VALUES (?, ?, ?, ?, ?, ?, 'sent')",
            (int(time.time()), slot, old_percent, new_percent, old_bytes, new_bytes),
        )
    return cursor.lastrowid


def finish_inverter_write(conn: sqlite3.Connection, write_id: int, outcome: str, held_bytes: str | None) -> None:
    with conn:
        conn.execute(
            "UPDATE inverter_writes SET outcome = ?, held_bytes = ? WHERE id = ?", (outcome, held_bytes, write_id)
        )


def list_inverter_writes(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, object]]:
    """The latest first."""
    columns = ("id", "ts", "slot", "old_percent", "new_percent", "old_bytes", "new_bytes", "outcome")
    rows = conn.execute(
        f"SELECT {', '.join(columns)} FROM inverter_writes ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(zip(columns, row)) for row in rows]


def add_setting_write(conn: sqlite3.Connection, setting: str, old_value: int, new_value: int) -> int:
    """Called before a write is sent, so it's on record whatever happens next."""
    with conn:
        cursor = conn.execute(
            "INSERT INTO setting_writes (ts, setting, old_value, new_value, outcome) VALUES (?, ?, ?, ?, 'sent')",
            (int(time.time()), setting, old_value, new_value),
        )
    return cursor.lastrowid


def finish_setting_write(conn: sqlite3.Connection, write_id: int, outcome: str, held_value: int | None) -> None:
    with conn:
        conn.execute(
            "UPDATE setting_writes SET outcome = ?, held_value = ? WHERE id = ?", (outcome, held_value, write_id)
        )


def list_setting_writes(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, object]]:
    """The latest first."""
    columns = ("id", "ts", "setting", "old_value", "new_value", "outcome")
    rows = conn.execute(
        f"SELECT {', '.join(columns)} FROM setting_writes ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(zip(columns, row)) for row in rows]
