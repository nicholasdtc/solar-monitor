"""Downloadable copies of this app's data. To add a format, write a writer and list it in FORMATS.

Even a query for a few columns reads the whole `samples` table, because each row holds its raw
readings, and a year of those is gigabytes. So exports run on a worker thread with their own
read-only connection, and go to a temporary file rather than memory, which leaves the poll loop
and the page running meanwhile. WAL means they also see a steady snapshot while new readings save.
"""

from __future__ import annotations

import csv
import io
import json
import shutil
import sqlite3
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from solar_monitor import db

# File name in the ZIP (without .csv) -> what goes in it.
CSV_QUERIES = {
    "samples": f"SELECT ts, datetime(ts, 'unixepoch', 'localtime') AS local_time, {', '.join(db.READINGS)} "
    "FROM samples ORDER BY ts",
    "daily_counters": "SELECT day, counter, first_value, last_value FROM daily_counters ORDER BY day, counter",
    "appliances": "SELECT id, name, typical_w, peak_w, surge_w, use_in_outage, notes FROM appliances ORDER BY id",
    "settings": "SELECT key, value FROM settings ORDER BY key",
    "inverter_writes": "SELECT id, ts, datetime(ts, 'unixepoch', 'localtime') AS local_time, slot, old_percent, new_percent, "
    "outcome, old_bytes, new_bytes, held_bytes FROM inverter_writes ORDER BY id",
    "setting_writes": "SELECT id, ts, datetime(ts, 'unixepoch', 'localtime') AS local_time, setting, old_value, new_value, "
    "outcome, held_value FROM setting_writes ORDER BY id",
    "meter_readings": "SELECT day, slot, occurrence, kw FROM meter_readings ORDER BY day, slot, occurrence",
}

CSV_LAYOUT = "Solar monitor export, CSV layout 1"  # Importing reads this, so a change of layout needs a new number.

CSV_README = f"""{CSV_LAYOUT}

samples.csv        One row per poll. ts is Unix seconds, local_time is the same moment on this
                    machine's clock. Powers are in watts and battery_soc is a percentage.
                    battery_w is as the inverter reports it: negative means charging.
                    grid_w is too: positive means exporting.
                    Excel opens at most 1,048,576 rows, about six months of polls every 15 seconds.
daily_counters.csv  Each day's first and latest reading of each lifetime counter, in kWh.
                    A day's energy runs from the previous recorded day's latest reading.
appliances.csv      Your appliances, in watts. use_in_outage is 1 if ticked for a power cut.
settings.csv        Your planning choices. Unset ones aren't listed.
inverter_writes.csv Every change this app sent to the inverter. Confirmed means it read the change
                    back. The bytes are the whole TOU programme in hex, before and after.
setting_writes.csv  Every change this app sent to Backup mode (backup, 1 is on) or the battery's
                    limits in %: min_soc and reserve_soc stop discharging on-grid and in a power
                    cut, max_soc stops charging. Outcomes are as in inverter_writes.csv.
meter_readings.csv  The electricity meter's half-hours, as imported from ESB Networks. day and
                    slot are on Irish clock time: slot 0 starts at midnight, slot 47 at 23:30.
                    occurrence is 1 for the second half-hour with the same label when the
                    clocks go back. kw is the average drawn from the grid over the half-hour,
                    so its kWh is half of it.

The other readings from each poll are only in the full database export.
"""

APPLIANCES_FORMAT = "solar-monitor-appliances"
APPLIANCES_VERSION = 1


def _write_csv_zip(source: sqlite3.Connection, path: Path) -> None:
    source.execute("BEGIN")  # So that the files all describe the same moment.
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.txt", CSV_README)
        for name, query in CSV_QUERIES.items():
            rows = source.execute(query)
            with archive.open(f"{name}.csv", "w") as member:
                # The byte order mark is how Excel knows it's UTF-8, for names with accents.
                with io.TextIOWrapper(member, encoding="utf-8-sig", newline="") as text:
                    writer = csv.writer(text)
                    writer.writerow(column[0] for column in rows.description)
                    writer.writerows(rows)


def _write_sqlite(source: sqlite3.Connection, path: Path) -> None:
    copy = sqlite3.connect(path)
    try:
        source.backup(copy)
    finally:
        copy.close()


def _write_appliances_json(source: sqlite3.Connection, path: Path) -> None:
    appliances = db.list_appliances(source)
    for appliance in appliances:
        del appliance["id"]  # It only means something inside this database.
    document = {"format": APPLIANCES_FORMAT, "version": APPLIANCES_VERSION, "appliances": appliances}
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class Format:
    label: str
    description: str
    extension: str
    media_type: str
    write: Callable[[sqlite3.Connection, Path], None]


FORMATS = {
    "csv": Format(
        "Spreadsheets (CSV files in a ZIP)",
        "For Excel or Google Sheets. Each poll's main readings, daily counters, appliances, settings, inverter changes "
        "and the meter's half-hours.",
        "zip",
        "application/zip",
        _write_csv_zip,
    ),
    "sqlite": Format(
        "Full database (SQLite)",
        "A complete backup, including every reading from every poll. Needs a database tool to open.",
        "db",
        "application/vnd.sqlite3",
        _write_sqlite,
    ),
    "appliances": Format(
        "Appliances (JSON)",
        "Just your appliance list, in a small file that's easy to read or edit. No readings or settings.",
        "json",
        "application/json",
        _write_appliances_json,
    ),
}


def summary(conn: sqlite3.Connection) -> dict[str, object]:
    """What an export would hold, cheaply enough to ask for on the poll loop's own connection."""
    (first_ts, last_ts, appliances, settings, meter_readings, pages, page_size) = conn.execute(
        """
        SELECT
            -- Separate queries, because asking for both in one would read the whole table.
            (SELECT MIN(ts) FROM samples), (SELECT MAX(ts) FROM samples),
            (SELECT COUNT(*) FROM appliances),
            (SELECT COUNT(*) FROM settings),
            (SELECT COUNT(*) FROM meter_readings),
            (SELECT page_count FROM pragma_page_count), (SELECT page_size FROM pragma_page_size)
        """
    ).fetchone()
    return {
        "first_ts": first_ts,
        "last_ts": last_ts,
        "appliances": appliances,
        "settings": settings,
        "meter_readings": meter_readings,
        "size_bytes": pages * page_size,
    }


def database_path(conn: sqlite3.Connection) -> Path:
    return Path(conn.execute("PRAGMA database_list").fetchone()[2])


def write(export_format: Format, database: Path) -> Path:
    """Write the export into a new temporary folder, which the caller removes. Runs on a worker thread."""
    folder = Path(tempfile.mkdtemp(prefix="solar-monitor-export-"))
    path = folder / f"solar-monitor-export-{date.today().isoformat()}.{export_format.extension}"
    source = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    try:
        export_format.write(source, path)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    finally:
        source.close()
    return path
