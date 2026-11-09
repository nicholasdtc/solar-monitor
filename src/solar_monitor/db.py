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

from solar_monitor.weather.base import HOUR, HOUR_FIELDS, Hour, Warning

# Each Hour field's column: the condition is text, is_day 0 or 1, and the rest numbers.
_HOUR_COLUMNS = ",\n    ".join(
    f"{name} {'TEXT' if name == 'condition' else 'INTEGER' if name == 'is_day' else 'REAL'}" for name in HOUR_FIELDS
)

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
    held_bytes  TEXT,                   -- what the read-back found, in hex
    user_id     INTEGER,                -- who asked for it: a users id, or NULL without accounts or before them
    username    TEXT                    -- their name then, kept after they're removed. NULL before accounts.
);

-- Changes to one of the inverter's single-number settings: Backup mode and the battery's limits.
CREATE TABLE IF NOT EXISTS setting_writes (
    id         INTEGER PRIMARY KEY,
    ts         INTEGER NOT NULL,        -- Unix seconds, from just before it was sent
    setting    TEXT NOT NULL,           -- a key of control.SETTINGS, such as min_soc
    old_value  INTEGER NOT NULL,        -- as read just before: what undo goes back to
    new_value  INTEGER NOT NULL,        -- what was sent
    outcome    TEXT NOT NULL,           -- as in inverter_writes
    held_value INTEGER,                 -- what the read-back found
    user_id    INTEGER,                 -- as in inverter_writes
    username   TEXT
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
-- rather than in the browser, so the choices follow each person to every device.
CREATE TABLE IF NOT EXISTS display_prefs (
    user_id INTEGER NOT NULL,           -- whose: a users id, or 0 without accounts
    name    TEXT NOT NULL,              -- a field of inputs.DisplayPrefsIn
    value   TEXT NOT NULL,
    PRIMARY KEY (user_id, name)
) WITHOUT ROWID;

-- The people who can log in. The account named admin is the owner, which can't be removed.
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE, -- lower case, see auth.USERNAME
    password_hash TEXT NOT NULL,        -- see auth.hash_password
    admin         INTEGER NOT NULL DEFAULT 0,
    permissions   TEXT NOT NULL DEFAULT '', -- keys of auth.PERMISSIONS, separated by spaces
    created       INTEGER NOT NULL,     -- Unix seconds
    last_login    INTEGER
);

-- Who is logged in where. Only a hash of each cookie's token, so reading this can't log anyone in.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,        -- SHA-256 of the token, in hex
    user_id    INTEGER NOT NULL,
    expires    INTEGER NOT NULL         -- Unix seconds
) WITHOUT ROWID;

-- What people did, other than changing the inverter, which inverter_writes and setting_writes record.
CREATE TABLE IF NOT EXISTS actions (
    id       INTEGER PRIMARY KEY,
    ts       INTEGER NOT NULL,          -- Unix seconds
    user_id  INTEGER,                   -- a users id, or NULL without accounts or for an unknown name
                                        -- (whose username is empty)
    username TEXT NOT NULL,             -- their name then, kept after they're removed
    kind     TEXT NOT NULL,             -- data, users or login
    summary  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS text_settings (
    key   TEXT PRIMARY KEY,             -- a key of TEXT_SETTINGS
    value TEXT NOT NULL
) WITHOUT ROWID;

-- Every time the weather was asked for, and how it went.
CREATE TABLE IF NOT EXISTS weather_fetches (
    id         INTEGER PRIMARY KEY,
    fetched_at INTEGER NOT NULL,        -- Unix seconds
    provider   TEXT NOT NULL,
    kind       TEXT NOT NULL,           -- forecast or warnings
    outcome    TEXT NOT NULL,           -- ok, not modified, rate limited or error
    items      INTEGER NOT NULL DEFAULT 0,
    error      TEXT
);
CREATE INDEX IF NOT EXISTS weather_fetches_kind ON weather_fetches (kind, outcome, fetched_at);

-- The hours of each forecast fetched, labelled by when each hour starts, in Unix seconds.
CREATE TABLE IF NOT EXISTS forecast_hours (
    fetch_id   INTEGER NOT NULL,        -- a weather_fetches id
    hour_start INTEGER NOT NULL,
    {hour_columns},
    PRIMARY KEY (fetch_id, hour_start)
) WITHOUT ROWID;

-- Weather warnings as issued, kept after they end.
CREATE TABLE IF NOT EXISTS weather_warnings (
    provider    TEXT NOT NULL,
    id          TEXT NOT NULL,          -- the issuer's own id
    level       TEXT NOT NULL,          -- minor, moderate, severe or extreme
    kind        TEXT NOT NULL,
    headline    TEXT NOT NULL,
    description TEXT NOT NULL,          -- as issued, which may be HTML
    onset       INTEGER,                -- Unix seconds
    expiry      INTEGER,
    regions     TEXT NOT NULL,          -- the provider's region codes, as a JSON list
    link        TEXT NOT NULL,
    first_seen  INTEGER NOT NULL,       -- when a fetch first and last had it
    last_seen   INTEGER NOT NULL,
    PRIMARY KEY (provider, id)
) WITHOUT ROWID;
""".replace("{hour_columns}", _HOUR_COLUMNS)

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
    # The solar panels, entered because the inverter can't report them: their total rating in kWp,
    # their tilt from flat and the compass direction they face (180 is south), in degrees.
    "pv_capacity_kwp",
    "panel_tilt_deg",
    "panel_azimuth_deg",
    "panel_azimuth_2_deg",  # optional: half the panels face this way instead
    # Where the house is, for the weather. Personal: rounded to 2 decimals and kept only here.
    "latitude",
    "longitude",
)

# Settings that are words rather than numbers. An unset one reads as None, which means the default.
TEXT_SETTINGS = (
    "forecast_provider",  # a name in weather.FORECASTS
    "warnings_provider",  # a name in weather.WARNINGS, or unset for no warnings
    "warnings_region",  # one of that provider's region codes, or unset for all of them
)

# How long an older forecast keeps its hours, from when it was fetched. The newest keeps them all.
FORECAST_KEEP_SECONDS = 48 * HOUR

# Settings older versions kept. They're removed on start, and skipped quietly in an import.
RETIRED_SETTINGS = (
    "target_soc",  # The charge target, now read from the inverter instead.
)

_APPLIANCE_FIELDS = ("name", "typical_w", "peak_w", "surge_w", "use_in_outage", "notes")


# Who did something: their users id (None without accounts) and their name at the time.
Who = tuple[int | None, str]


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
        _upgrade(conn)
        conn.executemany("DELETE FROM settings WHERE key = ?", [(key,) for key in RETIRED_SETTINGS])
    return conn


def _columns_of(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _upgrade(conn: sqlite3.Connection) -> None:
    """Bring a database from an older version up to SCHEMA, which only creates what's missing."""
    for log in ("inverter_writes", "setting_writes"):
        held = _columns_of(conn, log)
        for column, kind in (("user_id", "INTEGER"), ("username", "TEXT")):
            if column not in held:
                conn.execute(f"ALTER TABLE {log} ADD COLUMN {column} {kind}")
    if "user_id" not in _columns_of(conn, "display_prefs"):
        # From before accounts, when there was one set. It becomes the set used without accounts.
        conn.execute(
            "CREATE TABLE display_prefs_new (user_id INTEGER NOT NULL, name TEXT NOT NULL, value TEXT NOT NULL, "
            "PRIMARY KEY (user_id, name)) WITHOUT ROWID"
        )
        conn.execute("INSERT INTO display_prefs_new (user_id, name, value) SELECT 0, name, value FROM display_prefs")
        conn.execute("DROP TABLE display_prefs")
        conn.execute("ALTER TABLE display_prefs_new RENAME TO display_prefs")


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
# What was imported in each rate band, in kWh.
IMPORT_BANDS = ("import_night", "import_day", "import_peak")


def _in_band(start: str, end: str) -> str:
    """SQL for whether a reading's minute of the day is in a band, which may run past midnight.

    A band that starts and ends at the same time isn't used.
    """
    return f"""(CASE WHEN {start} = {end} THEN 0
        WHEN {start} < {end} THEN minute >= {start} AND minute < {end}
        ELSE minute >= {start} OR minute < {end} END)"""


def daily_flows(
    conn: sqlite3.Connection,
    start: int,
    end: int,
    pending: Iterable[Sample] = (),
    bands: dict[str, int] | None = None,
) -> dict[str, dict[str, float]]:
    """{local day: {flow: kWh}} worked out reading by reading, for start <= ts < end.

    The inverter only counts totals, so this splits them: solar goes to the house first,
    then to charging the battery, then to the grid, and the house takes what solar can't
    give from the battery first, then from the grid. Each part is capped by what was
    actually charged, discharged, exported or imported, so conversion losses aren't
    counted as anything. Battery and grid signs are as stored: positive battery is
    discharging, positive grid is exporting.

    Given the rate bands, as minutes after midnight ({"night_start": ..., "night_end": ...,
    "peak_start": ..., "peak_end": ...}), it also adds what was imported in each band, by the
    clock time of each reading. Peak wins where the two overlap.
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
                MIN(COALESCE(LEAD(ts) OVER (ORDER BY ts) - ts, 0), :longest) AS seconds,
                CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INTEGER) * 60
                    + CAST(strftime('%M', ts, 'unixepoch', 'localtime') AS INTEGER) AS minute
            FROM ({_readings_with_pending()})
        ),
        banded AS (
            SELECT *,
                CASE WHEN {_in_band(":peak_start", ":peak_end")} THEN 'peak'
                    WHEN {_in_band(":night_start", ":night_end")} THEN 'night'
                    ELSE 'day' END AS band
            FROM readings
        ),
        home AS (SELECT *, MIN(pv, load) AS solar_home FROM banded),
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
            SUM(MIN(load - solar_home - battery_home, import) * seconds),
            SUM(CASE band WHEN 'night' THEN import * seconds ELSE 0 END),
            SUM(CASE band WHEN 'day' THEN import * seconds ELSE 0 END),
            SUM(CASE band WHEN 'peak' THEN import * seconds ELSE 0 END)
        FROM split
        GROUP BY day
        """,
        {
            "start": start,
            "end": end,
            "waiting": _waiting(pending, start, end),
            "longest": MAX_READING_SECONDS,
            **{key: (bands or {}).get(key, 0) for key in ("night_start", "night_end", "peak_start", "peak_end")},
        },
    ).fetchall()
    names = FLOWS + IMPORT_BANDS if bands else FLOWS
    joules_per_kwh = 3_600_000
    return {day: {name: round(joules / joules_per_kwh, 3) for name, joules in zip(names, sums)} for day, *sums in rows}


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


def get_settings(conn: sqlite3.Connection) -> dict[str, float | str | None]:
    stored = dict(conn.execute("SELECT key, value FROM settings"))
    stored.update(conn.execute("SELECT key, value FROM text_settings"))
    return {key: stored.get(key) for key in (*SETTINGS, *TEXT_SETTINGS)}


def save_settings(conn: sqlite3.Connection, settings: dict[str, float | str | None]) -> None:
    """Change only the settings given, so each form sends just its own. None makes one unset."""
    with conn:
        for table, keys in (("settings", SETTINGS), ("text_settings", TEXT_SETTINGS)):
            given = [key for key in keys if key in settings]
            conn.executemany(f"DELETE FROM {table} WHERE key = ?", [(key,) for key in given])
            conn.executemany(
                f"INSERT INTO {table} (key, value) VALUES (?, ?)",
                [(key, settings[key]) for key in given if settings[key] is not None],
            )


def _add_weather_fetch(
    conn: sqlite3.Connection, fetched_at: int, provider: str, kind: str, outcome: str, items: int, error: str | None
) -> int:
    cursor = conn.execute(
        "INSERT INTO weather_fetches (fetched_at, provider, kind, outcome, items, error) VALUES (?, ?, ?, ?, ?, ?)",
        (fetched_at, provider, kind, outcome, items, error),
    )
    return int(cursor.lastrowid)


def add_weather_fetch(
    conn: sqlite3.Connection, fetched_at: int, provider: str, kind: str, outcome: str, error: str | None = None
) -> None:
    """Note a fetch that brought nothing to keep: not modified, rate limited or an error."""
    with conn:
        _add_weather_fetch(conn, fetched_at, provider, kind, outcome, 0, error)


def save_forecast(conn: sqlite3.Connection, fetched_at: int, provider: str, hours: Iterable[Hour]) -> int:
    """Keep a forecast's hours, and trim the older forecasts to the hours just after they were fetched."""
    hours = list(hours)
    columns = ("fetch_id", "hour_start", *HOUR_FIELDS)
    with conn:
        fetch_id = _add_weather_fetch(conn, fetched_at, provider, "forecast", "ok", len(hours), None)
        conn.executemany(
            f"INSERT OR REPLACE INTO forecast_hours ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
            [(fetch_id, h.hour_start, *(getattr(h, name) for name in HOUR_FIELDS)) for h in hours],
        )
        # TODO(W6): keep one older forecast a day for 14 days, to see how far ahead they were right.
        conn.execute(
            "DELETE FROM forecast_hours WHERE fetch_id < ? AND hour_start >= "
            "(SELECT fetched_at FROM weather_fetches WHERE id = fetch_id) + ?",
            (fetch_id, FORECAST_KEEP_SECONDS),
        )
    return fetch_id


def latest_forecast(conn: sqlite3.Connection, since: int) -> dict[str, object] | None:
    """The newest forecast fetched, with its hours from `since` on, or None before the first."""
    row = conn.execute(
        "SELECT id, fetched_at, provider FROM weather_fetches WHERE kind = 'forecast' AND outcome = 'ok' "
        "ORDER BY fetched_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    columns = ("hour_start", *HOUR_FIELDS)
    hours = [
        dict(zip(columns, values, strict=True))
        for values in conn.execute(
            f"SELECT {', '.join(columns)} FROM forecast_hours WHERE fetch_id = ? AND hour_start >= ? "
            "ORDER BY hour_start",
            (row[0], since),
        )
    ]
    for hour in hours:
        if hour["is_day"] is not None:
            hour["is_day"] = bool(hour["is_day"])
    return {"fetched_at": row[1], "provider": row[2], "hours": hours}


def save_warnings(conn: sqlite3.Connection, fetched_at: int, provider: str, warnings: Iterable[Warning]) -> None:
    """Keep the warnings a fetch had. One seen before keeps its first_seen and takes any update."""
    warnings = list(warnings)
    with conn:
        _add_weather_fetch(conn, fetched_at, provider, "warnings", "ok", len(warnings), None)
        conn.executemany(
            "INSERT INTO weather_warnings (provider, id, level, kind, headline, description, onset, expiry, regions, "
            "link, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (provider, id) DO UPDATE SET level = excluded.level, kind = excluded.kind, "
            "headline = excluded.headline, description = excluded.description, onset = excluded.onset, "
            "expiry = excluded.expiry, regions = excluded.regions, link = excluded.link, "
            "last_seen = excluded.last_seen",
            [
                (
                    w.provider,
                    w.id,
                    w.level,
                    w.kind,
                    w.headline,
                    w.description,
                    w.onset,
                    w.expiry,
                    json.dumps(list(w.regions)),
                    w.link,
                    fetched_at,
                    fetched_at,
                )
                for w in warnings
            ],
        )


def current_warnings(conn: sqlite3.Connection, provider: str, now: int) -> list[dict[str, object]]:
    """The warnings the provider's latest fetch had that haven't ended, soonest first."""
    latest = conn.execute(
        "SELECT MAX(fetched_at) FROM weather_fetches WHERE kind = 'warnings' AND outcome = 'ok' AND provider = ?",
        (provider,),
    ).fetchone()[0]
    if latest is None:
        return []
    columns = ("provider", "id", "level", "kind", "headline", "description", "onset", "expiry", "regions", "link")
    rows = conn.execute(
        f"SELECT {', '.join(columns)} FROM weather_warnings WHERE provider = ? AND last_seen = ? "
        "AND (expiry IS NULL OR expiry > ?) ORDER BY onset IS NULL, onset, id",
        (provider, latest, now),
    )
    found = [dict(zip(columns, row, strict=True)) for row in rows]
    for warning in found:
        warning["regions"] = json.loads(warning["regions"])
    return found


def last_weather_fetches(conn: sqlite3.Connection) -> dict[str, dict[str, object]]:
    """Each kind's latest fetch, whatever its outcome, by kind."""
    columns = ("kind", "fetched_at", "provider", "outcome", "items", "error")
    rows = conn.execute(
        f"SELECT {', '.join(columns)} FROM weather_fetches "
        "WHERE id IN (SELECT MAX(id) FROM weather_fetches GROUP BY kind)"
    )
    return {row[0]: dict(zip(columns, row, strict=True)) for row in rows}


def get_display_prefs(conn: sqlite3.Connection, user_id: int) -> dict[str, str]:
    """One person's choices. User 0 is the set used without accounts."""
    return dict(conn.execute("SELECT name, value FROM display_prefs WHERE user_id = ? ORDER BY name", (user_id,)))


def save_display_prefs(conn: sqlite3.Connection, user_id: int, prefs: dict[str, str]) -> None:
    """Replace the person's whole set, so a choice put back to its default is forgotten too."""
    with conn:
        conn.execute("DELETE FROM display_prefs WHERE user_id = ?", (user_id,))
        conn.executemany(
            "INSERT INTO display_prefs (user_id, name, value) VALUES (?, ?, ?)",
            [(user_id, name, value) for name, value in prefs.items()],
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


def add_inverter_write(
    conn: sqlite3.Connection,
    slot: int,
    old_bytes: str,
    new_bytes: str,
    old_percent: int,
    new_percent: int,
    by: Who | None = None,
) -> int:
    """Called before a write is sent, so it's on record whatever happens next."""
    user_id, username = by or (None, None)
    with conn:
        cursor = conn.execute(
            "INSERT INTO inverter_writes (ts, slot, old_percent, new_percent, old_bytes, new_bytes, outcome, "
            "user_id, username) VALUES (?, ?, ?, ?, ?, ?, 'sent', ?, ?)",
            (int(time.time()), slot, old_percent, new_percent, old_bytes, new_bytes, user_id, username),
        )
    return cursor.lastrowid


def finish_inverter_write(conn: sqlite3.Connection, write_id: int, outcome: str, held_bytes: str | None) -> None:
    with conn:
        conn.execute(
            "UPDATE inverter_writes SET outcome = ?, held_bytes = ? WHERE id = ?", (outcome, held_bytes, write_id)
        )


def list_inverter_writes(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, object]]:
    """The latest first."""
    columns = ("id", "ts", "slot", "old_percent", "new_percent", "old_bytes", "new_bytes", "outcome", "username")
    rows = conn.execute(
        f"SELECT {', '.join(columns)} FROM inverter_writes ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(zip(columns, row)) for row in rows]


def add_setting_write(
    conn: sqlite3.Connection, setting: str, old_value: int, new_value: int, by: Who | None = None
) -> int:
    """Called before a write is sent, so it's on record whatever happens next."""
    user_id, username = by or (None, None)
    with conn:
        cursor = conn.execute(
            "INSERT INTO setting_writes (ts, setting, old_value, new_value, outcome, user_id, username) "
            "VALUES (?, ?, ?, ?, 'sent', ?, ?)",
            (int(time.time()), setting, old_value, new_value, user_id, username),
        )
    return cursor.lastrowid


def finish_setting_write(conn: sqlite3.Connection, write_id: int, outcome: str, held_value: int | None) -> None:
    with conn:
        conn.execute(
            "UPDATE setting_writes SET outcome = ?, held_value = ? WHERE id = ?", (outcome, held_value, write_id)
        )


def list_setting_writes(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, object]]:
    """The latest first."""
    columns = ("id", "ts", "setting", "old_value", "new_value", "outcome", "username")
    rows = conn.execute(
        f"SELECT {', '.join(columns)} FROM setting_writes ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(zip(columns, row)) for row in rows]


_USER_FIELDS = ("id", "username", "password_hash", "admin", "permissions", "created", "last_login")
_USER_COLUMNS = ", ".join(f"users.{field}" for field in _USER_FIELDS)


def _user(row: tuple[object, ...] | None) -> dict[str, object] | None:
    if row is None:
        return None
    user = dict(zip(_USER_FIELDS, row))
    user["admin"] = bool(user["admin"])
    user["permissions"] = user["permissions"].split()
    return user


def has_users(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT EXISTS (SELECT 1 FROM users)").fetchone()[0] == 1


def get_user(conn: sqlite3.Connection, user_id: int) -> dict[str, object] | None:
    return _user(conn.execute(f"SELECT {_USER_COLUMNS} FROM users WHERE id = ?", (user_id,)).fetchone())


def find_user(conn: sqlite3.Connection, username: str) -> dict[str, object] | None:
    """Capitals don't matter, as names are kept in lower case."""
    return _user(conn.execute(f"SELECT {_USER_COLUMNS} FROM users WHERE username = ?", (username.lower(),)).fetchone())


def list_users(conn: sqlite3.Connection, owner: str) -> list[dict[str, object]]:
    """The owner first, then by name."""
    rows = conn.execute(
        f"SELECT {_USER_COLUMNS} FROM users ORDER BY username != ?, username", (owner,)
    ).fetchall()
    return [_user(row) for row in rows]


def add_user(
    conn: sqlite3.Connection, username: str, password_hash: str, admin: bool, permissions: Iterable[str]
) -> int:
    """Raises sqlite3.IntegrityError if the name is taken."""
    with conn:
        cursor = conn.execute(
            "INSERT INTO users (username, password_hash, admin, permissions, created) VALUES (?, ?, ?, ?, ?)",
            (username.lower(), password_hash, int(admin), " ".join(sorted(permissions)), int(time.time())),
        )
    return cursor.lastrowid


def update_user(conn: sqlite3.Connection, user_id: int, admin: bool, permissions: Iterable[str]) -> bool:
    """False if there's no such user."""
    with conn:
        cursor = conn.execute(
            "UPDATE users SET admin = ?, permissions = ? WHERE id = ?",
            (int(admin), " ".join(sorted(permissions)), user_id),
        )
    return cursor.rowcount > 0


def set_password_hash(conn: sqlite3.Connection, user_id: int, password_hash: str) -> None:
    with conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))


def delete_user(conn: sqlite3.Connection, user_id: int) -> bool:
    """With their sessions and display choices. What they did stays on record, under their name."""
    with conn:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM display_prefs WHERE user_id = ?", (user_id,))
        cursor = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return cursor.rowcount > 0


def copy_display_prefs(conn: sqlite3.Connection, from_user: int, to_user: int, leave_out: Iterable[str] = ()) -> None:
    prefs = {name: value for name, value in get_display_prefs(conn, from_user).items() if name not in set(leave_out)}
    save_display_prefs(conn, to_user, prefs)


def add_session(conn: sqlite3.Connection, token_hash: str, user_id: int, expires: int) -> None:
    """Also forgets sessions that have expired, and notes when the person logged in."""
    now = int(time.time())
    with conn:
        conn.execute("DELETE FROM sessions WHERE expires <= ?", (now,))
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, expires) VALUES (?, ?, ?)", (token_hash, user_id, expires)
        )
        conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (now, user_id))


def session_user(conn: sqlite3.Connection, token_hash: str) -> dict[str, object] | None:
    """Whose session this is, read afresh every time so a change to their account applies at once."""
    row = conn.execute(
        f"SELECT {_USER_COLUMNS} FROM sessions JOIN users ON users.id = sessions.user_id "
        "WHERE sessions.token_hash = ? AND sessions.expires > ?",
        (token_hash, int(time.time())),
    ).fetchone()
    return _user(row)


def delete_session(conn: sqlite3.Connection, token_hash: str) -> None:
    with conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


def delete_sessions(conn: sqlite3.Connection, user_id: int, keep: str | None = None) -> int:
    """End all of a person's sessions, except the one whose hash is `keep`. Returns how many ended."""
    with conn:
        cursor = conn.execute(
            "DELETE FROM sessions WHERE user_id = ? AND token_hash IS NOT ? AND expires > ?",
            (user_id, keep, int(time.time())),
        )
    return cursor.rowcount


def count_sessions(conn: sqlite3.Connection) -> dict[int, int]:
    """Each person's live sessions."""
    return dict(
        conn.execute(
            "SELECT user_id, COUNT(*) FROM sessions WHERE expires > ? GROUP BY user_id", (int(time.time()),)
        )
    )


def add_action(conn: sqlite3.Connection, by: Who, kind: str, summary: str) -> None:
    user_id, username = by
    with conn:
        conn.execute(
            "INSERT INTO actions (ts, user_id, username, kind, summary) VALUES (?, ?, ?, ?, ?)",
            (int(time.time()), user_id, username, kind, summary),
        )


_HISTORY_FIELDS = ("source", "id", "ts", "username", "kind", "summary", "slot", "setting", "old", "new", "outcome")


def list_history(
    conn: sqlite3.Connection,
    before: int | None = None,
    limit: int = 100,
    username: str | None = None,
    kind: str | None = None,
) -> list[dict[str, object]]:
    """The actions and both write logs in one list, latest first. A write's summary is left for the
    caller to describe. `before` includes that second, so a page that ends part-way through one
    misses nothing: the caller drops what it already has, by source and id."""
    rows = conn.execute(
        """
        SELECT * FROM (
            SELECT 'action' AS source, id, ts, username, kind, summary,
                NULL AS slot, NULL AS setting, NULL AS old, NULL AS new, NULL AS outcome
            FROM actions
            UNION ALL
            SELECT 'tou', id, ts, username, 'inverter', NULL, slot, NULL, old_bytes, new_bytes, outcome
            FROM inverter_writes
            UNION ALL
            SELECT 'setting', id, ts, username, 'inverter', NULL, NULL, setting, old_value, new_value, outcome
            FROM setting_writes
        )
        WHERE (:before IS NULL OR ts <= :before)
            AND (:kind IS NULL OR kind = :kind)
            AND (:username IS NULL OR username = :username)
        ORDER BY ts DESC, source, id DESC
        LIMIT :limit
        """,
        {"before": before, "kind": kind, "username": username, "limit": limit},
    ).fetchall()
    return [dict(zip(_HISTORY_FIELDS, row)) for row in rows]


def history_names(conn: sqlite3.Connection) -> list[str]:
    """Everyone who has done something on record, including people since removed."""
    rows = conn.execute(
        """
        SELECT username FROM actions WHERE username != ''
        UNION SELECT username FROM inverter_writes WHERE username IS NOT NULL
        UNION SELECT username FROM setting_writes WHERE username IS NOT NULL
        UNION SELECT username FROM users
        ORDER BY 1
        """
    ).fetchall()
    return [name for (name,) in rows]
