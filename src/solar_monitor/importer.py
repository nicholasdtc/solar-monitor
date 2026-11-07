"""Brings this app's own exports back in: the CSV ZIP, the full database and the appliances JSON.

It only adds what's missing, so importing the same file twice changes nothing. Readings match by
their time, daily counters by day and name, appliances by name and settings by key, and whatever
is already here stays as it is. The one thing that grows is a day's counters, which widen to cover
the readings from both sides. The log of changes sent to the inverter is left out on purpose: it's
a record of what this copy of the app did, and it holds the bytes an undo would send. Nothing here
talks to the inverter.

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
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import date
from itertools import islice
from pathlib import Path

from pydantic import ValidationError

from solar_monitor import db, export
from solar_monitor.inputs import ApplianceIn, SettingsIn

BATCH_ROWS = 1000
BATCH_PAUSE_SECONDS = 0.02
WAIT_FOR_SAVE_SECONDS = 30  # A save by the poll loop is quick, so this only runs out if something's badly wrong.
MAX_JSON_BYTES = 10_000_000  # It's read into memory, and an appliance list is far smaller.
LATEST_TS = 4_102_444_800  # The year 2100. Anything later is a mistake, such as milliseconds.

SQLITE_HEADER = b"SQLite format 3\0"
ZIP_HEADER = b"PK\x03\x04"
BYTE_ORDER_MARK = b"\xef\xbb\xbf"
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")

COLUMNS = {
    "samples": ("ts", *db.READINGS),
    "daily_counters": ("day", "counter", "first_value", "last_value"),
    "appliances": ("name", "typical_w", "peak_w", "surge_w", "use_in_outage", "notes"),
    "settings": ("key", "value"),
}
LABELS = {"samples": "Readings", "daily_counters": "Daily counters", "appliances": "Appliances", "settings": "Settings"}
WRITE_LOG = "inverter_writes"

NOT_AN_EXPORT = "This isn't an export from this app, so nothing was imported."

Row = dict[str, object]


# What a damaged, encrypted or oddly compressed ZIP can raise as it's read.
ZIP_ERRORS = (zipfile.BadZipFile, zlib.error, csv.Error, UnicodeError, EOFError, RuntimeError)


class BadFile(Exception):
    """The file can't be imported, and nothing was changed. The message is for the person who chose it."""


class CsvZip:
    format = "csv"
    tables = tuple(COLUMNS)
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
        if "README.txt" not in names or any(f"{table}.csv" not in names for table in self.tables):
            raise BadFile(NOT_AN_EXPORT)
        try:
            with self.archive.open("README.txt") as readme:
                layout = readme.readline(200).decode("utf-8", "replace").strip()
        except ZIP_ERRORS as error:
            raise BadFile(NOT_AN_EXPORT) from error
        if layout != export.CSV_LAYOUT:
            raise BadFile("This ZIP isn't in a layout this version of the app can read, so nothing was imported.")
        for table, columns in COLUMNS.items():
            with self._reader(table) as reader:
                missing = set(columns) - set(reader.fieldnames or ())
            if missing:
                raise BadFile(f"{table}.csv has no {', '.join(sorted(missing))} column, so nothing was imported.")

    def rows(self, table: str) -> Iterator[Row]:
        with self._reader(table) as reader:
            yield from reader

    def left_out(self) -> int:
        if f"{WRITE_LOG}.csv" not in self.archive.namelist():  # Exports from before the log existed.
            return 0
        with self._reader(WRITE_LOG) as reader:
            return sum(1 for _ in reader)

    def close(self) -> None:
        self.archive.close()


class Database:
    format = "sqlite"
    tables = tuple(COLUMNS)
    first_row = 1

    def __init__(self, path: Path) -> None:
        # Immutable because nothing is writing to it, so there's no journal to look for or leave behind.
        self.conn = sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)
        self.columns = {**COLUMNS, "samples": (*COLUMNS["samples"], "raw")}
        self.has_write_log = False

    def _damaged(self, error: sqlite3.Error) -> BadFile:
        return BadFile(f"This database file can't be read ({error}), so nothing was imported.")

    def check(self) -> None:
        try:
            self.conn.execute("PRAGMA trusted_schema = OFF")
            # Only real tables, because a view or trigger in a file from elsewhere could run anything.
            found = {name for (name,) in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            for table, columns in self.columns.items():
                held = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")} if table in found else set()
                if not set(columns) <= held:
                    raise BadFile(NOT_AN_EXPORT)
        except sqlite3.Error as error:
            raise self._damaged(error) from error
        self.has_write_log = WRITE_LOG in found  # Exports from before the log existed don't have it.

    def rows(self, table: str) -> Iterator[Row]:
        columns = self.columns[table]
        try:
            for row in self.conn.execute(f"SELECT {', '.join(columns)} FROM {table}"):
                yield dict(zip(columns, row))
        except sqlite3.Error as error:
            raise self._damaged(error) from error

    def left_out(self) -> int:
        if not self.has_write_log:
            return 0
        try:
            return self.conn.execute(f"SELECT COUNT(*) FROM {WRITE_LOG}").fetchone()[0]
        except sqlite3.Error as error:
            raise self._damaged(error) from error

    def close(self) -> None:
        self.conn.close()


class AppliancesJson:
    format = "appliances"
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


Source = CsvZip | Database | AppliancesJson


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
    else:
        raise BadFile(NOT_AN_EXPORT)
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
    SettingsIn.model_validate({key: value})
    return key, value


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
            if key not in db.SETTINGS:
                unknown += 1
            elif key not in here:
                new += 1
                here.add(key)
                if write:
                    db.save_settings(target, {key: value})
    return {"in_file": in_file, "new": new, "unknown": unknown}


MERGES = {
    "samples": _merge_samples,
    "daily_counters": _merge_counters,
    "appliances": _merge_appliances,
    "settings": _merge_settings,
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
    unknown = counters.get("unknown", 0) + results.get("settings", {}).get("unknown", 0)
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
        "label": export.FORMATS[source.format].label,
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
