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
from solar_monitor.weather.base import HOUR_FIELDS

# File name in the ZIP (without .csv) -> what goes in it.
CSV_QUERIES = {
    "samples": f"SELECT ts, datetime(ts, 'unixepoch', 'localtime') AS local_time, {', '.join(db.READINGS)} "
    "FROM samples ORDER BY ts",
    "daily_counters": "SELECT day, counter, first_value, last_value FROM daily_counters ORDER BY day, counter",
    "appliances": "SELECT id, name, typical_w, peak_w, surge_w, use_in_outage, notes FROM appliances ORDER BY id",
    "settings": "SELECT key, value FROM settings ORDER BY key",
    "text_settings": "SELECT key, value FROM text_settings ORDER BY key",
    "inverter_writes": "SELECT id, ts, datetime(ts, 'unixepoch', 'localtime') AS local_time, slot, old_percent, new_percent, "
    "outcome, old_bytes, new_bytes, held_bytes, username FROM inverter_writes ORDER BY id",
    "setting_writes": "SELECT id, ts, datetime(ts, 'unixepoch', 'localtime') AS local_time, setting, old_value, new_value, "
    "outcome, held_value, username FROM setting_writes ORDER BY id",
    "meter_readings": "SELECT day, slot, occurrence, kw FROM meter_readings ORDER BY day, slot, occurrence",
    "actions": "SELECT id, ts, datetime(ts, 'unixepoch', 'localtime') AS local_time, username, kind, summary "
    "FROM actions ORDER BY id",
    "weather_warnings": "SELECT provider, id, level, kind, headline, description, onset, expiry, regions, link, "
    "first_seen, last_seen FROM weather_warnings ORDER BY first_seen, provider, id",
    "weather_hours": f"SELECT hour_start, datetime(hour_start, 'unixepoch', 'localtime') AS local_time, source, "
    f"fetched_at, panels, {', '.join(HOUR_FIELDS)} FROM weather_hours ORDER BY hour_start",
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
text_settings.csv   The choices that are words, such as where the weather comes from.
inverter_writes.csv Every change this app sent to the inverter. Confirmed means it read the change
                    back. The bytes are the whole TOU programme in hex, before and after.
                    username is who asked for it, empty from before there were accounts.
setting_writes.csv  Every change this app sent to Backup mode (backup, 1 is on) or the battery's
                    limits in %: min_soc and reserve_soc stop discharging on-grid and in a power
                    cut, max_soc stops charging. outcome and username are as in inverter_writes.csv.
meter_readings.csv  The electricity meter's half-hours, as imported from ESB Networks. day and
                    slot are its labels, on Irish clock time, and each is when the half-hour
                    ends: slot 1 ends at 00:30, slot 47 at 23:30, and slot 0 at midnight, so
                    it's the day before's last. occurrence is 1 for the earlier of two
                    half-hours with the same label when the clocks go back. kw is the average
                    drawn from the grid over the half-hour, so its kWh is half of it.
actions.csv         The Activity page: what people did here other than change the
                    inverter. kind is data, users or login. An empty username is a log-in
                    with an unknown name.
weather_warnings.csv Weather warnings as issued. onset, expiry, first_seen and last_seen are
                    Unix seconds: when it starts and ends, and the first and latest fetch that
                    had it. regions is a JSON list of the issuer's region codes. description
                    is as issued, which may be HTML.
weather_hours.csv   The weather each hour, to set beside the readings. hour_start is Unix
                    seconds when the hour starts, and local_time the same on this machine's
                    clock. source is past: and the provider for its record of the weather
                    then, or forecast: and the provider for its latest forecast of the hour.
                    panels is the tilt/facing gti_wm2 is for. Units: °C, %, hPa, mm, m/s and
                    W/m². wind_from_deg is where the wind comes from. is_day is 1 or 0.

Accounts and their passwords aren't exported.
The other readings from each poll are only in the full database export.
So are the weather forecasts and the log of fetching them.
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
        # Accounts stay behind: a password hash can be guessed at offline, and a session logs
        # someone in. secure_delete and VACUUM leave no trace of them in the file's free space.
        copy.execute("PRAGMA secure_delete = ON")
        with copy:
            copy.execute("DELETE FROM sessions")
            copy.execute("DELETE FROM users")
        copy.execute("VACUUM")
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
        "For Excel or Google Sheets. Each poll's main readings, daily counters, appliances, settings, inverter changes, "
        "the meter's half-hours and the history.",
        "zip",
        "application/zip",
        _write_csv_zip,
    ),
    "sqlite": Format(
        "Full database (SQLite)",
        "A complete backup, including every reading from every poll, but not the accounts. Needs a database tool to open.",
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
