"""Brings this app's own exports back in (the CSV ZIP, the full database and the appliances JSON),
and the half-hourly smart meter file from ESB Networks.

It only adds what's missing, so importing the same file twice changes nothing. Readings match by
their time, daily counters by day and name, appliances by name, settings by key, weather warnings
by their issuer and id, and the meter's half-hours by their label, and whatever is already here stays as it is. The one thing that grows
is a day's counters, which widen to cover the readings from both sides. The log of changes sent to
the inverter is left out on purpose: it's a record of what this copy of the app did, and it holds
the bytes an undo would send. Nothing here talks to the inverter.

Every row of the meter file carries the MPRN and the meter's serial number. Neither is kept, and
neither goes into a message, because this app's exports and screens could end up anywhere.

The whole file is read and checked before the first write, so a bad row anywhere means no change.
Like exports, this runs on a worker thread with its own connections. Writes go in small batches
with a pause between them, because the poll loop's save holds up the whole web app while it waits.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import sqlite3
import time
import zipfile
import zlib
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import date, datetime
from itertools import islice
from pathlib import Path

from pydantic import ValidationError

from solar_monitor import automation, db, export, meter
from solar_monitor.inputs import ApplianceIn, SettingsIn
from solar_monitor.weather.base import CONDITIONS, HOUR, HOUR_FIELDS, LEVELS

BATCH_ROWS = 1000
BATCH_PAUSE_SECONDS = 0.02
WAIT_FOR_SAVE_SECONDS = 30  # A save by the poll loop is quick, so this only runs out if something's badly wrong.
MAX_JSON_BYTES = 10_000_000  # It's read into memory, and an appliance list is far smaller.
# Every label in a meter file is remembered while it's read, to find repeats. Two years are about 3 MB.
MAX_METER_BYTES = 30_000_000
LATEST_TS = 4_102_444_800  # The year 2100. Anything later is a mistake, such as milliseconds.

SQLITE_HEADER = b"SQLite format 3\0"
ZIP_HEADER = b"PK\x03\x04"
BYTE_ORDER_MARK = b"\xef\xbb\xbf"
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
WEATHER_SOURCE = re.compile(r"(past|forecast):[\w.-]{1,40}")
PANELS = re.compile(r"[\d.]{1,8}/([\d.]{1,8}|-)(/[\d.]{1,8})?")

COLUMNS = {
    "samples": ("ts", *db.READINGS),
    "daily_counters": ("day", "counter", "first_value", "last_value"),
    "appliances": ("name", "typical_w", "peak_w", "surge_w", "use_in_outage", "notes"),
    "settings": ("key", "value"),
    "meter_readings": ("day", "slot", "occurrence", "kw"),
    "text_settings": ("key", "value"),
    "weather_warnings": (
        "provider",
        "id",
        "level",
        "kind",
        "headline",
        "description",
        "onset",
        "expiry",
        "regions",
        "link",
        "first_seen",
        "last_seen",
    ),
    "weather_hours": ("hour_start", "source", "fetched_at", "panels", *HOUR_FIELDS),
}
# Exports from before the app held them don't have them.
OPTIONAL = {"meter_readings", "text_settings", "weather_warnings", "weather_hours"}
LABELS = {
    "samples": "Readings",
    "daily_counters": "Daily counters",
    "appliances": "Appliances",
    "settings": "Settings",
    "meter_readings": "Meter half-hours",
    "text_settings": "Weather choices",
    "weather_warnings": "Weather warnings",
    "weather_hours": "Weather by the hour",
}
WRITE_LOGS = ("inverter_writes", "setting_writes")  # Only true of the copy that sent them.

# ESB Networks' "HDF" download of half-hourly readings in kW, as it comes from their website.
ESB_COLUMNS = ("MPRN", "Meter Serial Number", "Read Value", "Read Type", "Read Date and End Time")
ESB_IDENTIFIERS = ("MPRN", "Meter Serial Number")
ESB_VALUE, ESB_TYPE, ESB_TIME = ESB_COLUMNS[2:]
ESB_IMPORT = "Active Import Interval (kW)"
# Since late 2026 the same file can hold what went out to the grid too, row by row with the import.
# The app doesn't keep those yet, so they're checked and left out.
ESB_EXPORT = "Active Export Interval (kW)"
ESB_TIME_FORMAT = "%d-%m-%Y %H:%M"

NOT_AN_EXPORT = "This isn't an export from this app, so nothing was imported."
UNKNOWN_FILE = "This isn't an export from this app or a meter file from ESB Networks, so nothing was imported."

Row = dict[str, object]


# What a damaged, encrypted or oddly compressed ZIP can raise as it's read.
ZIP_ERRORS = (zipfile.BadZipFile, zlib.error, csv.Error, UnicodeError, EOFError, RuntimeError)


class BadFile(Exception):
    """The file can't be imported, and nothing was changed. The message is for the person who chose it."""


class CsvZip:
    format = "csv"
    label = export.FORMATS["csv"].label
    tables = tuple(COLUMNS)  # Narrowed by check() to the ones in the file.
    first_row = 2  # As a spreadsheet numbers them, with the header as row 1.

    def __init__(self, path: Path) -> None:
        try:
            self.archive = zipfile.ZipFile(path)
        except ZIP_ERRORS as error:
            raise BadFile(NOT_AN_EXPORT) from error

    @contextmanager
    def _reader(self, table: str) -> Iterator[csv.DictReader]:
        try:
            with self.archive.open(f"{table}.csv") as member, io.TextIOWrapper(member, encoding="utf-8-sig", newline="") as text:
                yield csv.DictReader(text)
        except ZIP_ERRORS as error:
            raise BadFile(f"{table}.csv can't be read ({error}), so nothing was imported.") from error

    def check(self) -> None:
        names = self.archive.namelist()
        self.tables = tuple(table for table in COLUMNS if table not in OPTIONAL or f"{table}.csv" in names)
        if "README.txt" not in names or any(f"{table}.csv" not in names for table in self.tables):
            raise BadFile(NOT_AN_EXPORT)
        try:
            with self.archive.open("README.txt") as readme:
                layout = readme.readline(200).decode("utf-8", "replace").strip()
        except ZIP_ERRORS as error:
            raise BadFile(NOT_AN_EXPORT) from error
        if layout != export.CSV_LAYOUT:
            raise BadFile("This ZIP isn't in a layout this version of the app can read, so nothing was imported.")
        for table in self.tables:
            with self._reader(table) as reader:
                missing = set(COLUMNS[table]) - set(reader.fieldnames or ())
            if missing:
                raise BadFile(f"{table}.csv has no {', '.join(sorted(missing))} column, so nothing was imported.")

    def rows(self, table: str) -> Iterator[Row]:
        with self._reader(table) as reader:
            yield from reader

    def left_out(self) -> int:
        count = 0
        for log in WRITE_LOGS:
            if f"{log}.csv" not in self.archive.namelist():  # Exports from before the log existed.
                continue
            with self._reader(log) as reader:
                count += sum(1 for _ in reader)
        return count

    def close(self) -> None:
        self.archive.close()


class Database:
    format = "sqlite"
    label = export.FORMATS["sqlite"].label
    tables = tuple(COLUMNS)  # Narrowed by check() to the ones in the file.
    first_row = 1

    def __init__(self, path: Path) -> None:
        # Immutable because nothing is writing to it, so there's no journal to look for or leave behind.
        self.conn = sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)
        self.columns = {**COLUMNS, "samples": (*COLUMNS["samples"], "raw")}
        self.write_logs: tuple[str, ...] = ()

    def _damaged(self, error: sqlite3.Error) -> BadFile:
        return BadFile(f"This database file can't be read ({error}), so nothing was imported.")

    def check(self) -> None:
        try:
            self.conn.execute("PRAGMA trusted_schema = OFF")
            # Only real tables, because a view or trigger in a file from elsewhere could run anything.
            found = {name for (name,) in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            self.tables = tuple(table for table in COLUMNS if table not in OPTIONAL or table in found)
            for table in self.tables:
                held = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")} if table in found else set()
                if not set(self.columns[table]) <= held:
                    raise BadFile(NOT_AN_EXPORT)
        except sqlite3.Error as error:
            raise self._damaged(error) from error
        self.write_logs = tuple(log for log in WRITE_LOGS if log in found)  # Older exports lack them.

    def rows(self, table: str) -> Iterator[Row]:
        columns = self.columns[table]
        try:
            for row in self.conn.execute(f"SELECT {', '.join(columns)} FROM {table}"):
                yield dict(zip(columns, row))
        except sqlite3.Error as error:
            raise self._damaged(error) from error

    def left_out(self) -> int:
        try:
            return sum(self.conn.execute(f"SELECT COUNT(*) FROM {log}").fetchone()[0] for log in self.write_logs)
        except sqlite3.Error as error:
            raise self._damaged(error) from error

    def close(self) -> None:
        self.conn.close()


class AppliancesJson:
    format = "appliances"
    label = export.FORMATS["appliances"].label
    tables = ("appliances",)
    first_row = 1

    def __init__(self, path: Path) -> None:
        self.path = path
        self.appliances: list[Row] = []

    def check(self) -> None:
        if self.path.stat().st_size > MAX_JSON_BYTES:
            raise BadFile(NOT_AN_EXPORT)
        try:
            document = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (ValueError, RecursionError) as error:  # Covers text that isn't UTF-8 as well as text that isn't JSON.
            raise BadFile(NOT_AN_EXPORT) from error
        if not isinstance(document, dict) or document.get("format") != export.APPLIANCES_FORMAT:
            raise BadFile(NOT_AN_EXPORT)
        if document.get("version") != export.APPLIANCES_VERSION:
            raise BadFile("This appliance list isn't in a version this app can read, so nothing was imported.")
        appliances = document.get("appliances")
        if not isinstance(appliances, list) or not all(isinstance(appliance, dict) for appliance in appliances):
            raise BadFile(NOT_AN_EXPORT)
        self.appliances = appliances

    def rows(self, table: str) -> Iterator[Row]:
        yield from self.appliances

    def left_out(self) -> int:
        return 0

    def close(self) -> None:
        pass


def _label_key(stamp: object) -> object:
    """What makes two of the meter file's time labels the same, even if written differently."""
    try:
        when = datetime.strptime(str(stamp).strip(), ESB_TIME_FORMAT)
    except ValueError:
        return stamp  # The check names the row.
    return when.date(), meter.slot_of(when)


class EsbCsv:
    """The half-hourly smart meter file from ESB Networks, newest first, one row per half-hour."""

    format = "esb"
    label = "Smart meter half-hours from ESB Networks"
    tables = ("meter_readings",)
    first_row = 2

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _reader(self) -> Iterator[Iterator[list[str]]]:
        try:
            with self.path.open(encoding="utf-8-sig", newline="") as text:
                yield csv.reader(text)
        except (csv.Error, UnicodeError) as error:
            raise BadFile(f"This meter file can't be read ({error}), so nothing was imported.") from error

    def check(self) -> None:
        if self.path.stat().st_size > MAX_METER_BYTES:
            raise BadFile("This file is too big to be a meter file from ESB Networks, so nothing was imported.")
        with self._reader() as reader:
            header = next(reader, None)
        if header is None or tuple(name.strip() for name in header) != ESB_COLUMNS:
            raise BadFile(
                "This looks like a file from ESB Networks, but not the one with each half-hour's reading in kW, "
                "so nothing was imported."
            )

    def rows(self, table: str) -> Iterator[Row]:
        """Each row's reading, time and type, without the identifiers, and with what the check needs to
        know: how many times its label came before, and whether it's for a different meter."""
        seen: Counter[object] = Counter()
        first_meter = None
        with self._reader() as reader:
            next(reader, None)  # The header, already checked.
            for fields in reader:
                row: Row = dict(zip(ESB_COLUMNS, fields))
                row["columns"] = len(fields)
                if first_meter is None:
                    first_meter = row.get("MPRN")
                row["other_meter"] = row.get("MPRN") != first_meter  # Compared here, and dropped straight after.
                for name in ESB_IDENTIFIERS:
                    row.pop(name, None)
                # Import and export rows share their labels, so each type counts its own repeats.
                key = (str(row.get(ESB_TYPE)).strip(), _label_key(row.get(ESB_TIME)))
                row["occurrence"] = seen[key]
                seen[key] += 1
                yield row

    def left_out(self) -> int:
        return 0

    def close(self) -> None:
        pass


Source = CsvZip | Database | AppliancesJson | EsbCsv


@contextmanager
def _opened(path: Path) -> Iterator[Source]:
    """The right reader for the file, going by what's in it rather than its name. Always closed afterwards."""
    with path.open("rb") as file:
        head = file.read(len(SQLITE_HEADER))
    if not head:
        raise BadFile("This file is empty, so nothing was imported.")
    source: Source
    if head == SQLITE_HEADER:
        source = Database(path)
    elif head.startswith(ZIP_HEADER):
        source = CsvZip(path)
    elif head.removeprefix(BYTE_ORDER_MARK).lstrip().startswith(b"{"):
        source = AppliancesJson(path)
    elif head.removeprefix(BYTE_ORDER_MARK).startswith(b"MPRN,"):
        source = EsbCsv(path)
    else:
        raise BadFile(UNKNOWN_FILE)
    try:
        source.check()
        yield source
    finally:
        source.close()


def _number(value: object, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} isn't a number: {str(value)[:40]}") from None
    if not math.isfinite(number):
        raise ValueError(f"{name} isn't a usable number: {value}")
    return number


def _sample(row: Row) -> tuple[object, ...]:
    """(ts, each reading, raw), with raw None if the file doesn't have it."""
    try:
        ts = int(row["ts"])
    except (TypeError, ValueError):  # A spreadsheet can turn these into something like 1.8E+09.
        raise ValueError(f"ts isn't a whole number of seconds: {str(row['ts'])[:40]}") from None
    if not 0 < ts <= LATEST_TS:
        raise ValueError(f"ts isn't a time in seconds: {ts}")
    raw = row.get("raw")
    if not isinstance(raw, (bytes, type(None))):
        raise ValueError("raw isn't what this app saves there")
    readings = (None if row[name] in (None, "") else _number(row[name], name) for name in db.READINGS)
    return (ts, *readings, raw)


def _with_raw(sample: tuple[object, ...]) -> tuple[object, ...]:
    """Only made when it's about to be saved, because compressing every row is slow and a preview doesn't need it."""
    if sample[-1] is not None:
        return sample
    readings = sample[1:-1]
    known = {reading: value for reading, value in zip(db.READINGS.values(), readings) if value is not None}
    return (*sample[:-1], db.encode_raw(known))


def _counter(row: Row) -> tuple[object, object, float, float]:
    day = row["day"]
    if not isinstance(day, str) or not DAY.fullmatch(day):
        raise ValueError(f"day isn't in the form YYYY-MM-DD: {str(day)[:40]}")
    date.fromisoformat(day)
    # Not checked against each other: a counter that drops, as when an inverter is replaced, is real data.
    return day, row["counter"], _number(row["first_value"], "first_value"), _number(row["last_value"], "last_value")


def _appliance(row: Row) -> dict[str, object]:
    blanks = {name: None for name in ("peak_w", "surge_w") if row.get(name) == ""}
    return ApplianceIn.model_validate({**row, **blanks}).model_dump()


def _setting(row: Row) -> tuple[object, float | None]:
    key = row["key"]
    if key not in db.SETTINGS:  # Perhaps from a newer version, which says nothing about whether the rest are good.
        return key, None
    value = _number(row["value"], str(key))
    if key in automation.SETTINGS:
        if automation.problem(key, value):
            raise ValueError(automation.problem(key, value))
    else:
        SettingsIn.model_validate({key: value})
    return key, value


def _text_setting(row: Row) -> tuple[object, str | None]:
    """A known key with a value this version takes, or the key and None: a provider or key from a
    newer version, which says nothing about whether the rest are good."""
    key, value = row["key"], row["value"]
    if key not in db.TEXT_SETTINGS or not isinstance(value, str):
        return key, None
    try:
        SettingsIn.model_validate({key: value})
    except ValidationError:
        return key, None
    return key, value


def _optional_whole(value: object, name: str) -> int | None:
    if value is None or value == "":
        return None
    return _whole(value, name, LATEST_TS)


def _weather_warning(row: Row) -> tuple[object, ...]:
    texts = {}
    for name in ("provider", "id", "level", "kind", "headline", "description", "link"):
        value = row[name]
        if not isinstance(value, str) or (name in ("provider", "id") and not value):
            raise ValueError(f"{name} is missing")
        texts[name] = value
    if texts["level"] not in LEVELS:
        raise ValueError(f"level isn't one of {', '.join(LEVELS)}: {texts['level'][:40]}")
    # The page makes it a link, so nothing but a web address.
    if not texts["link"].startswith(("https://", "http://")):
        raise ValueError(f"link isn't a web address: {texts['link'][:40]}")
    try:
        regions = json.loads(str(row["regions"]))
    except json.JSONDecodeError:
        regions = None
    if not isinstance(regions, list) or not all(isinstance(code, str) for code in regions):
        raise ValueError(f"regions isn't a list of region codes: {str(row['regions'])[:40]}")
    return (
        texts["provider"],
        texts["id"],
        texts["level"],
        texts["kind"],
        texts["headline"],
        texts["description"],
        _optional_whole(row["onset"], "onset"),
        _optional_whole(row["expiry"], "expiry"),
        json.dumps(regions),
        texts["link"],
        _whole(row["first_seen"], "first_seen", LATEST_TS),
        _whole(row["last_seen"], "last_seen", LATEST_TS),
    )


def _whole(value: object, name: str, highest: int) -> int:
    number = _number(value, name)
    if not number.is_integer() or not 0 <= number <= highest:
        raise ValueError(f"{name} isn't a whole number from 0 to {highest}: {value}")
    return int(number)


def _kw(value: object, name: str) -> float:
    kw = _number(value, name)
    if kw < 0:
        raise ValueError(f"{name} is below zero: {value}")
    return kw


def _doubled(day: date, slot: int, occurrence: int) -> bool:
    """Whether a half-hour can come twice: only the hour the clocks go back repeats."""
    return occurrence == 1 and day == meter.last_sunday(day.year, 10) and slot in meter.REPEATED_SLOTS


def _meter_reading(row: Row) -> tuple[str, int, int, float]:
    """A row of this app's own export: (day, slot, occurrence, kW)."""
    day = row["day"]
    if not isinstance(day, str) or not DAY.fullmatch(day):
        raise ValueError(f"day isn't in the form YYYY-MM-DD: {str(day)[:40]}")
    when = date.fromisoformat(day)
    slot = _whole(row["slot"], "slot", 47)
    occurrence = _whole(row["occurrence"], "occurrence", 1)
    if occurrence and not _doubled(when, slot, occurrence):
        raise ValueError(f"occurrence is 1 for a half-hour that only comes once: {day} slot {slot}")
    return day, slot, occurrence, _kw(row["kw"], "kw")


def _esb_reading(row: Row) -> tuple[str, int, int, float] | None:
    """A row of the meter file: (day, slot, occurrence, kW), or None for an export row, which is
    checked like the rest but not kept. Its identifiers never reach here."""
    if row["columns"] != len(ESB_COLUMNS):
        raise ValueError(f"it has {row['columns']} columns, not {len(ESB_COLUMNS)}")
    if row["other_meter"]:
        raise ValueError("it's for a different meter from the rows above it, and a file should have one meter's readings")
    stamp = str(row[ESB_TIME]).strip()
    try:
        when = datetime.strptime(stamp, ESB_TIME_FORMAT)
    except ValueError:
        raise ValueError(f"{ESB_TIME} isn't a time like 31-12-2025 23:30: {stamp[:40]}") from None
    if when.minute % 30:
        raise ValueError(f"{ESB_TIME} isn't on the hour or half past: {stamp}")
    read_type = str(row[ESB_TYPE]).strip()
    if read_type not in (ESB_IMPORT, ESB_EXPORT):
        raise ValueError(f"{ESB_TYPE} is {read_type[:40]!r}, and only {ESB_IMPORT!r} and {ESB_EXPORT!r} can be read")
    slot = meter.slot_of(when)
    if row["occurrence"] and not _doubled(when.date(), slot, row["occurrence"]):
        raise ValueError(f"{stamp} is in the file more than once")
    kw = _kw(row[ESB_VALUE], ESB_VALUE)
    if read_type == ESB_EXPORT:
        return None
    return when.date().isoformat(), slot, row["occurrence"], kw


def _problem(error: ValueError) -> str:
    if isinstance(error, ValidationError):
        return "; ".join(f"{'.'.join(map(str, detail['loc']))}: {detail['msg']}" for detail in error.errors())
    return str(error)


@contextmanager
def _checked(source: Source, table: str, check: Callable[[Row], object]) -> Iterator[Iterator[object]]:
    """The table's rows, each checked. Afterwards the source's reader is closed however this ended,
    so that a refused file isn't still open when its temporary folder is removed."""
    rows = source.rows(table)

    def read() -> Iterator[object]:
        for number, row in enumerate(rows, source.first_row):
            try:
                checked = check(row)
            except ValueError as error:  # Which includes pydantic's.
                raise BadFile(f"{LABELS[table]}, row {number}: {_problem(error)}. Nothing was imported.") from None
            yield checked

    try:
        yield read()
    finally:
        rows.close()


def _batches(rows: Iterable[object]) -> Iterator[list]:
    rows = iter(rows)
    while batch := list(islice(rows, BATCH_ROWS)):
        yield batch


def _merge_samples(source: Source, target: sqlite3.Connection, write: bool) -> dict[str, object]:
    in_file = new = 0
    first_ts = last_ts = None
    with _checked(source, "samples", _sample) as rows:
        for batch in _batches(rows):
            times = {sample[0] for sample in batch}
            earliest, latest = min(times), max(times)
            in_file += len(batch)
            first_ts = earliest if first_ts is None else min(first_ts, earliest)
            last_ts = latest if last_ts is None else max(last_ts, latest)
            if write:
                before = target.total_changes
                with target:
                    target.executemany(
                        "INSERT OR IGNORE INTO samples (ts, pv_w, load_w, battery_w, battery_soc, grid_w, raw) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [_with_raw(sample) for sample in batch],
                    )
                new += target.total_changes - before
                time.sleep(BATCH_PAUSE_SECONDS)
            else:
                here = target.execute("SELECT ts FROM samples WHERE ts BETWEEN ? AND ?", (earliest, latest))
                new += len(times - {ts for (ts,) in here})
    return {"in_file": in_file, "new": new, "first_ts": first_ts, "last_ts": last_ts}


def _merge_counters(source: Source, target: sqlite3.Connection, write: bool) -> dict[str, object]:
    known = {
        (day, counter): (first, last)
        for day, counter, first, last in target.execute("SELECT day, counter, first_value, last_value FROM daily_counters")
    }
    in_file = new = extended = unknown = 0
    with _checked(source, "daily_counters", _counter) as rows:
        for batch in _batches(rows):
            changes = []
            for day, counter, first, last in batch:
                in_file += 1
                if counter not in db.COUNTERS:
                    unknown += 1
                    continue
                held = known.get((day, counter))
                merged = (first, last) if held is None else (min(first, held[0]), max(last, held[1]))
                if held is None:
                    new += 1
                elif merged == held:
                    continue
                else:
                    extended += 1
                known[(day, counter)] = merged
                changes.append((day, counter, *merged))
            if write and changes:
                with target:
                    target.executemany(
                        "INSERT INTO daily_counters (day, counter, first_value, last_value) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT (day, counter) DO UPDATE SET "
                        "first_value = MIN(first_value, excluded.first_value), "
                        "last_value = MAX(last_value, excluded.last_value)",
                        changes,
                    )
                time.sleep(BATCH_PAUSE_SECONDS)
    return {"in_file": in_file, "new": new, "extended": extended, "unknown": unknown}


def _merge_meter_readings(source: Source, target: sqlite3.Connection, write: bool) -> dict[str, object]:
    check = _esb_reading if isinstance(source, EsbCsv) else _meter_reading
    in_file = new = differ = export = 0
    first = last = None

    def kept(rows: Iterator[object]) -> Iterator[object]:
        nonlocal export
        for reading in rows:
            if reading is None:
                export += 1
            else:
                yield reading

    with _checked(source, "meter_readings", check) as rows:
        for batch in _batches(kept(rows)):
            days = {reading[0] for reading in batch}
            earliest, latest = min(days), max(days)
            in_file += len(batch)
            first = earliest if first is None else min(first, earliest)
            last = latest if last is None else max(last, latest)
            here = {
                (day, slot, occurrence): kw
                for day, slot, occurrence, kw in target.execute(
                    "SELECT day, slot, occurrence, kw FROM meter_readings WHERE day BETWEEN ? AND ?", (earliest, latest)
                )
            }
            # ESB Networks can correct a reading after publishing it. The one already here stays.
            differ += sum(1 for *key, kw in batch if tuple(key) in here and abs(here[tuple(key)] - kw) > 1e-9)
            if write:
                before = target.total_changes
                with target:
                    target.executemany(
                        "INSERT OR IGNORE INTO meter_readings (day, slot, occurrence, kw) VALUES (?, ?, ?, ?)", batch
                    )
                new += target.total_changes - before
                time.sleep(BATCH_PAUSE_SECONDS)
            else:
                new += len({tuple(reading[:3]) for reading in batch} - set(here))
    return {"in_file": in_file, "new": new, "differ": differ, "export": export, "first": first, "last": last}


def _merge_appliances(source: Source, target: sqlite3.Connection, write: bool) -> dict[str, object]:
    # Taken once, so two rows with one name both come back to an empty app, as they were when exported.
    here = {name.casefold() for (name,) in target.execute("SELECT name FROM appliances")}
    in_file = new = 0
    kept = []
    with _checked(source, "appliances", _appliance) as rows:
        for appliance in rows:
            in_file += 1
            if appliance["name"].casefold() in here:
                kept.append(appliance["name"])
                continue
            new += 1
            if write:
                db.add_appliance(target, appliance)
    return {"in_file": in_file, "new": new, "kept": kept}


def _merge_settings(source: Source, target: sqlite3.Connection, write: bool) -> dict[str, object]:
    here = {key for (key,) in target.execute("SELECT key FROM settings")}
    in_file = new = unknown = 0
    with _checked(source, "settings", _setting) as rows:
        for key, value in rows:
            in_file += 1
            if key in db.RETIRED_SETTINGS:  # From an older copy of this app, so nothing to warn about.
                continue
            if key not in db.SETTINGS:
                unknown += 1
            elif key not in here:
                new += 1
                here.add(key)
                if write:
                    db.save_settings(target, {key: value})
    return {"in_file": in_file, "new": new, "unknown": unknown}


def _merge_text_settings(source: Source, target: sqlite3.Connection, write: bool) -> dict[str, object]:
    here = {key for (key,) in target.execute("SELECT key FROM text_settings")}
    in_file = new = unknown = 0
    with _checked(source, "text_settings", _text_setting) as rows:
        for key, value in rows:
            in_file += 1
            if value is None:
                unknown += 1
            elif key not in here:
                new += 1
                here.add(key)
                if write:
                    db.save_settings(target, {key: value})
    return {"in_file": in_file, "new": new, "unknown": unknown}


def _blank(value: object) -> bool:
    return value is None or value == ""


def _weather_hour(row: Row) -> tuple[object, ...]:
    hour_start = _whole(row["hour_start"], "hour_start", LATEST_TS)
    if hour_start % HOUR:
        raise ValueError(f"hour_start isn't on the hour: {hour_start}")
    source = row["source"]
    if not isinstance(source, str) or not WEATHER_SOURCE.fullmatch(source):
        raise ValueError(f"source isn't past: or forecast: and a provider: {str(source)[:40]}")
    panels = None if _blank(row["panels"]) else row["panels"]
    if panels is not None and not (isinstance(panels, str) and PANELS.fullmatch(panels)):
        raise ValueError(f"panels isn't a tilt and facings: {str(panels)[:40]}")
    values: list[object] = []
    for name in HOUR_FIELDS:
        value = row[name]
        if _blank(value):
            values.append(None)
        elif name == "condition":
            if value not in CONDITIONS:
                raise ValueError(f"condition isn't one this app knows: {str(value)[:40]}")
            values.append(value)
        elif name == "is_day":
            values.append(_whole(value, name, 1))
        else:
            values.append(_number(value, name))
    return (hour_start, source, _whole(row["fetched_at"], "fetched_at", LATEST_TS), panels, *values)


def _merge_weather_hours(source: Source, target: sqlite3.Connection, write: bool) -> dict[str, object]:
    """Hours not here are added, and a record of past weather replaces a forecast here."""
    here = {hour: bool(past) for hour, past in target.execute("SELECT hour_start, source LIKE 'past:%' FROM weather_hours")}
    columns = COLUMNS["weather_hours"]
    in_file = new = 0
    with _checked(source, "weather_hours", _weather_hour) as rows:
        for batch in _batches(rows):
            in_file += len(batch)
            fresh = []
            for hour in batch:
                past = str(hour[1]).startswith("past:")
                if hour[0] not in here or (past and not here[hour[0]]):
                    here[hour[0]] = past
                    fresh.append(hour)
            new += len(fresh)
            if write and fresh:
                with target:
                    target.executemany(
                        f"INSERT OR REPLACE INTO weather_hours ({', '.join(columns)}) "
                        f"VALUES ({', '.join('?' * len(columns))})",
                        fresh,
                    )
                time.sleep(BATCH_PAUSE_SECONDS)
    return {"in_file": in_file, "new": new}


def _merge_weather_warnings(source: Source, target: sqlite3.Connection, write: bool) -> dict[str, object]:
    here = set(target.execute("SELECT provider, id FROM weather_warnings"))
    columns = COLUMNS["weather_warnings"]
    in_file = new = 0
    with _checked(source, "weather_warnings", _weather_warning) as rows:
        for batch in _batches(rows):
            in_file += len(batch)
            fresh = []
            for warning in batch:
                if warning[:2] not in here:
                    here.add(warning[:2])
                    fresh.append(warning)
            new += len(fresh)
            if write and fresh:
                with target:
                    target.executemany(
                        f"INSERT OR IGNORE INTO weather_warnings ({', '.join(columns)}) "
                        f"VALUES ({', '.join('?' * len(columns))})",
                        fresh,
                    )
                time.sleep(BATCH_PAUSE_SECONDS)
    return {"in_file": in_file, "new": new}


MERGES = {
    "samples": _merge_samples,
    "daily_counters": _merge_counters,
    "appliances": _merge_appliances,
    "settings": _merge_settings,
    "meter_readings": _merge_meter_readings,
    "text_settings": _merge_text_settings,
    "weather_warnings": _merge_weather_warnings,
    "weather_hours": _merge_weather_hours,
}


def _notes(source: Source, results: dict[str, dict[str, object]]) -> list[str]:
    notes = []
    counters = results.get("daily_counters", {})
    if counters.get("extended"):
        notes.append(f"Daily counters widened to cover readings from the file as well: {counters['extended']}.")
    kept = results.get("appliances", {}).get("kept")
    if kept:
        notes.append(f"Appliances with a name already here, left as they are: {', '.join(kept)}.")
    if source.format == "csv" and results["samples"]["new"]:
        notes.append("A CSV export only has each poll's main readings, so the rest aren't restored. A full database export has them.")
    differ = results.get("meter_readings", {}).get("differ")
    if differ:
        notes.append(
            f"Meter half-hours that read differently in the file from the ones already here, which were kept: {differ}."
        )
    export_rows = results.get("meter_readings", {}).get("export")
    if export_rows:
        notes.append(f"Export half-hours, not imported: {export_rows}. The app only keeps import for now.")
    if source.format == "esb":
        notes.append("The MPRN and the meter's serial number on each row aren't kept.")
    unknown = sum(results.get(table, {}).get("unknown", 0) for table in ("daily_counters", "settings", "text_settings"))
    if unknown:
        notes.append(f"Rows this version of the app doesn't recognise, skipped: {unknown}.")
    left_out = source.left_out()
    if left_out:
        notes.append(f"Changes sent to the inverter, not imported: {left_out}. That log only records what this copy of the app did.")
    return notes


def _merge(source: Source, target: sqlite3.Connection, write: bool) -> dict[str, object]:
    results = {table: merge(source, target, write) for table, merge in MERGES.items() if table in source.tables}
    tables = []
    for table, result in results.items():
        already_here = result["in_file"] - result["new"] - result.get("unknown", 0)
        tables.append({"id": table, "label": LABELS[table], "already_here": already_here, **result})
    return {
        "format": source.format,
        "label": source.label,
        "applied": write,
        "changes": any(table["new"] or table.get("extended") for table in tables),
        "tables": tables,
        "notes": _notes(source, results),
    }


def run(path: Path, database: Path, *, apply: bool) -> dict[str, object]:
    """Say what the file at `path` would add to the database, and add it if `apply` is set.

    Runs on a worker thread. Raises BadFile, with the database untouched, if any of the file is unusable.
    """
    with _opened(path) as source:
        reader = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
        try:
            report = _merge(source, reader, write=False)  # The check. Reading every row is what finds a bad one.
        finally:
            reader.close()
        if not apply:
            return report
        writer = sqlite3.connect(database, timeout=WAIT_FOR_SAVE_SECONDS)
        try:
            writer.execute("PRAGMA synchronous = NORMAL")  # As the poll loop's connection does.
            return _merge(source, writer, write=True)
        finally:
            writer.close()
