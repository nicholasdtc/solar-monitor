import asyncio
import csv
import io
import json
import shutil
import sqlite3
import tempfile
import threading
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from solar_monitor import db, export, importer, planning
from solar_monitor.api import create_app
from solar_monitor.auth import LoginRequired
from solar_monitor.collector import Collector
from solar_monitor.importer import BadFile

TS = 1_800_000_000
DAY = 86_400
READING = {"ppv": 1500.5, "load_ptotal": 400, "pbattery1": -800, "battery_soc": 62, "active_power_total": 300, "e_total": 10.5, "vpv1": 230}
LATER = {"ppv": 0, "battery_soc": 55, "e_total": 11.5}  # No load, battery or grid reading.
FRIDGE = {"name": "Crème fridge", "typical_w": 120.0, "peak_w": None, "surge_w": 600.0, "use_in_outage": True, "notes": 'a "cold", one'}
KETTLE = {"name": "Kettle", "typical_w": 2000.0, "peak_w": 2200.0, "surge_w": None, "use_in_outage": False, "notes": ""}
BINARY = {"Content-Type": "application/octet-stream"}
FORMATS = ("csv", "sqlite", "appliances")
CSV_READINGS = {"ppv": 1500.5, "load_ptotal": 400.0, "pbattery1": -800.0, "battery_soc": 62.0, "active_power_total": 300.0}


@pytest.fixture(autouse=True)
def temp_folder(tmp_path, monkeypatch):
    """Where the route keeps an upload, so a test can check nothing is left behind."""
    folder = tmp_path / "temp"
    folder.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(folder))
    return folder


@pytest.fixture
def conn(tmp_path):
    """The app being imported into."""
    connection = db.connect(tmp_path / "here.db")
    yield connection
    connection.close()


@pytest.fixture
def source(tmp_path):
    """The app that the exports came from."""
    connection = db.connect(tmp_path / "source.db")
    db.save_samples(connection, [db.Sample(TS, READING), db.Sample(TS + DAY, LATER)])
    db.add_appliance(connection, FRIDGE)
    db.add_appliance(connection, KETTLE)
    db.save_settings(connection, {"tariff_day": 0.25, "target_soc": 90})
    write = db.add_inverter_write(connection, 2, "aa" * 12, "bb" * 12, 50, 60)
    db.finish_inverter_write(connection, write, "confirmed", None)
    yield connection
    connection.close()


def exported(source, name):
    """The bytes of an export, leaving the temporary folder as it was."""
    path = export.write(export.FORMATS[name], export.database_path(source))
    try:
        return path.read_bytes()
    finally:
        shutil.rmtree(path.parent)


def run(tmp_path, conn, content, *, apply=True):
    upload = tmp_path / "upload"
    upload.write_bytes(content)
    return importer.run(upload, export.database_path(conn), apply=apply)


def contents(conn):
    """Everything in the database, to check nothing changed."""
    tables = ("samples", "daily_counters", "appliances", "settings", "inverter_writes")
    return {table: conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall() for table in tables}


def appliances(conn):
    return [{key: value for key, value in row.items() if key != "id"} for row in db.list_appliances(conn)]


def table(report, name):
    return next(entry for entry in report["tables"] if entry["id"] == name)


def rezip(original, changes):
    """A copy of the ZIP with some members replaced, or removed if the new text is None."""
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as before, zipfile.ZipFile(output, "w") as after:
        for name in before.namelist():
            text = changes.get(name, before.read(name))
            if text is not None:
                after.writestr(name, text)
    return output.getvalue()


def with_cell(original, name, column, value):
    """A copy of the CSV export with one cell changed in the last row of a file."""
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        rows = list(csv.DictReader(io.StringIO(archive.read(f"{name}.csv").decode("utf-8-sig"), newline="")))
    rows[-1][column] = value
    text = io.StringIO(newline="")
    writer = csv.DictWriter(text, rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    return rezip(original, {f"{name}.csv": text.getvalue()})


def with_sql(tmp_path, original, *statements):
    """A copy of the database export with some SQL run on it."""
    path = tmp_path / "edited.db"
    path.write_bytes(original)
    connection = sqlite3.connect(path)
    with connection:
        for statement in statements:
            connection.execute(statement)
    connection.close()
    edited = path.read_bytes()
    path.unlink()
    return edited


def spoiled(tmp_path, source, name):
    """An export of the format with one unusable value in it."""
    content = exported(source, name)
    if name == "csv":
        return with_cell(content, "settings", "value", "abc")
    if name == "sqlite":
        return with_sql(tmp_path, content, "UPDATE settings SET value = 500 WHERE key = 'target_soc'")
    return content.replace(b'"typical_w": 2000.0', b'"typical_w": -1')


def assert_refused(tmp_path, conn, content, match=None):
    before = contents(conn)
    with pytest.raises(BadFile, match=match):
        run(tmp_path, conn, content)
    assert contents(conn) == before


# Each format into an empty app.


def test_full_database_brings_back_everything_but_the_inverter_log(tmp_path, conn, source):
    report = run(tmp_path, conn, exported(source, "sqlite"))
    assert report["format"] == "sqlite"
    assert report["applied"] is True
    assert report["changes"] is True
    expected = contents(source)
    actual = contents(conn)
    for name in ("samples", "daily_counters", "settings"):
        assert actual[name] == expected[name], name
    assert appliances(conn) == appliances(source)
    assert db.daily_totals(conn) == db.daily_totals(source)
    assert actual["inverter_writes"] == []


def test_csv_brings_back_the_main_readings_and_what_can_be_rebuilt_from_them(tmp_path, conn, source):
    report = run(tmp_path, conn, exported(source, "csv"))
    assert report["format"] == "csv"
    columns = "ts, pv_w, load_w, battery_w, battery_soc, grid_w"
    assert conn.execute(f"SELECT {columns} FROM samples").fetchall() == source.execute(f"SELECT {columns} FROM samples").fetchall()
    assert contents(conn)["daily_counters"] == contents(source)["daily_counters"]
    assert contents(conn)["settings"] == contents(source)["settings"]
    assert appliances(conn) == appliances(source)  # Accents, quotes and the tick all survive.
    assert db.daily_totals(conn) == db.daily_totals(source)
    assert db.decode_raw(conn.execute("SELECT raw FROM samples WHERE ts = ?", (TS,)).fetchone()[0]) == CSV_READINGS
    assert db.decode_raw(conn.execute("SELECT raw FROM samples WHERE ts = ?", (TS + DAY,)).fetchone()[0]) == {"ppv": 0.0, "battery_soc": 55.0}
    assert any("main readings" in note for note in report["notes"])


def test_appliances_json_brings_back_only_appliances(tmp_path, conn, source):
    report = run(tmp_path, conn, exported(source, "appliances"))
    assert report["format"] == "appliances"
    assert [entry["id"] for entry in report["tables"]] == ["appliances"]
    assert table(report, "appliances")["new"] == 2
    assert appliances(conn) == appliances(source)
    assert contents(conn)["samples"] == []
    assert contents(conn)["settings"] == []


def test_a_hand_edited_appliance_list_can_leave_out_what_has_a_default(tmp_path, conn):
    document = {"format": export.APPLIANCES_FORMAT, "version": export.APPLIANCES_VERSION, "appliances": [{"name": "  Oven ", "typical_w": 2500}]}
    run(tmp_path, conn, b"\xef\xbb\xbf" + json.dumps(document, indent=2).encode())  # Notepad can add the byte order mark.
    assert appliances(conn) == [{"name": "Oven", "typical_w": 2500.0, "peak_w": None, "surge_w": None, "use_in_outage": False, "notes": ""}]


def test_it_goes_by_what_is_in_the_file_not_its_name(tmp_path, conn, source):
    upload = tmp_path / "backup.json"
    upload.write_bytes(exported(source, "sqlite"))
    assert importer.run(upload, export.database_path(conn), apply=False)["format"] == "sqlite"


def test_the_report_says_when_the_readings_are_from(tmp_path, conn, source):
    report = run(tmp_path, conn, exported(source, "sqlite"), apply=False)
    assert (table(report, "samples")["first_ts"], table(report, "samples")["last_ts"]) == (TS, TS + DAY)
    assert report["label"] == export.FORMATS["sqlite"].label


# Previewing and repeating.


@pytest.mark.parametrize("name", FORMATS)
def test_a_preview_writes_nothing_and_says_what_importing_then_adds(tmp_path, conn, source, name):
    content = exported(source, name)
    before = contents(conn)
    preview = run(tmp_path, conn, content, apply=False)
    assert preview["applied"] is False
    assert preview["changes"] is True
    assert contents(conn) == before
    report = run(tmp_path, conn, content)
    assert report["applied"] is True
    assert report["tables"] == preview["tables"]
    assert report["notes"] == preview["notes"]


@pytest.mark.parametrize("name", FORMATS)
def test_importing_the_same_file_again_changes_nothing(tmp_path, conn, source, name):
    content = exported(source, name)
    run(tmp_path, conn, content)
    before = contents(conn)
    preview = run(tmp_path, conn, content, apply=False)
    report = run(tmp_path, conn, content)
    assert preview["changes"] is False
    assert report["changes"] is False
    assert all(entry["new"] == 0 and entry["already_here"] == entry["in_file"] for entry in report["tables"])
    assert contents(conn) == before


def test_a_file_with_nothing_in_it_has_nothing_to_add(tmp_path, conn):
    empty = db.connect(tmp_path / "empty.db")
    content = exported(empty, "sqlite")
    empty.close()
    report = run(tmp_path, conn, content)
    assert report["changes"] is False
    assert table(report, "samples")["first_ts"] is None


# Merging with what's already here.


def test_readings_already_here_stay_and_the_missing_ones_are_added(tmp_path, conn, source):
    db.save_samples(conn, [db.Sample(TS, {"ppv": 7})])
    samples = table(run(tmp_path, conn, exported(source, "sqlite")), "samples")
    assert (samples["in_file"], samples["new"], samples["already_here"]) == (2, 1, 1)
    assert conn.execute("SELECT ts, pv_w FROM samples ORDER BY ts").fetchall() == [(TS, 7.0), (TS + DAY, 0.0)]


def test_an_appliance_with_a_name_already_here_keeps_what_it_has(tmp_path, conn, source):
    mine = {**FRIDGE, "name": "crème FRIDGE", "typical_w": 99.0}
    db.add_appliance(conn, mine)
    report = run(tmp_path, conn, exported(source, "appliances"))
    assert appliances(conn) == [mine, KETTLE]
    assert table(report, "appliances")["new"] == 1
    assert table(report, "appliances")["already_here"] == 1
    assert any("Crème fridge" in note for note in report["notes"])


def test_two_appliances_with_one_name_both_come_back_as_they_were_exported(tmp_path, conn, source):
    db.add_appliance(source, {**KETTLE, "typical_w": 1000.0})
    content = exported(source, "appliances")
    assert table(run(tmp_path, conn, content), "appliances")["new"] == 3
    assert len(appliances(conn)) == 3
    assert table(run(tmp_path, conn, content), "appliances")["new"] == 0


def test_a_setting_already_set_stays_and_an_unset_one_is_filled(tmp_path, conn, source):
    db.save_settings(conn, {"tariff_day": 0.5})
    report = run(tmp_path, conn, exported(source, "csv"))
    assert db.get_settings(conn)["tariff_day"] == 0.5
    assert db.get_settings(conn)["target_soc"] == 90
    assert table(report, "settings")["new"] == 1
    assert table(report, "settings")["already_here"] == 1


def test_a_days_counters_widen_to_cover_both_copies(tmp_path, conn, source):
    day, counter = source.execute("SELECT day, counter FROM daily_counters ORDER BY day").fetchone()
    source.execute("UPDATE daily_counters SET first_value = 8, last_value = 11 WHERE day = ? AND counter = ?", (day, counter))
    source.commit()
    with conn:
        conn.execute("INSERT INTO daily_counters VALUES (?, ?, 10, 12)", (day, counter))
    report = run(tmp_path, conn, exported(source, "sqlite"))
    assert table(report, "daily_counters")["new"] == 1
    assert table(report, "daily_counters")["extended"] == 1
    assert report["changes"] is True
    assert any("widened" in note for note in report["notes"])
    held = "SELECT first_value, last_value FROM daily_counters WHERE day = ? AND counter = ?"
    assert conn.execute(held, (day, counter)).fetchone() == (8, 12)


def test_a_day_already_covering_the_files_counters_is_left_alone(tmp_path, conn, source):
    day, counter = source.execute("SELECT day, counter FROM daily_counters ORDER BY day").fetchone()
    with conn:
        conn.execute("INSERT INTO daily_counters VALUES (?, ?, 1, 1000)", (day, counter))
    report = run(tmp_path, conn, exported(source, "sqlite"))
    assert table(report, "daily_counters")["extended"] == 0
    assert conn.execute("SELECT first_value, last_value FROM daily_counters WHERE day = ?", (day,)).fetchone() == (1, 1000)


def test_older_days_arriving_put_the_daily_totals_right(tmp_path, conn):
    """A day's energy runs from the day before's last reading, so filling a gap changes the next day's total."""
    db.save_samples(conn, [db.Sample(TS + DAY, {"e_total": 15})])
    assert db.daily_totals(conn) == [(db.Sample(TS + DAY, {}).day, "pv", 0)]
    older = db.connect(tmp_path / "older.db")
    db.save_samples(older, [db.Sample(TS, {"e_total": 10})])
    content = exported(older, "sqlite")
    older.close()
    run(tmp_path, conn, content)
    assert (db.Sample(TS + DAY, {}).day, "pv", 5) in db.daily_totals(conn)


def test_a_counter_that_dropped_is_real_data(tmp_path, conn, source):
    """As when an inverter is replaced, so it isn't a reason to refuse a backup."""
    content = with_cell(exported(source, "csv"), "daily_counters", "last_value", "0.5")
    run(tmp_path, conn, content)
    assert conn.execute("SELECT MIN(last_value) FROM daily_counters").fetchone() == (0.5,)


# What's left out.


@pytest.mark.parametrize("name", ("csv", "sqlite"))
def test_the_inverter_log_is_left_out_and_says_so(tmp_path, conn, source, name):
    report = run(tmp_path, conn, exported(source, name))
    assert contents(conn)["inverter_writes"] == []
    assert any("inverter" in note and "1" in note for note in report["notes"])


def test_exports_from_before_the_inverter_log_still_work(tmp_path, conn, source):
    older_zip = rezip(exported(source, "csv"), {"inverter_writes.csv": None})
    older_database = with_sql(tmp_path, exported(source, "sqlite"), "DROP TABLE inverter_writes")
    for content in (older_zip, older_database):
        report = run(tmp_path, conn, content, apply=False)
        assert table(report, "samples")["new"] == 2
        assert not any("inverter" in note for note in report["notes"])


def test_settings_and_counters_from_a_newer_version_are_skipped_and_counted(tmp_path, conn, source):
    content = exported(source, "csv")
    content = with_cell(content, "settings", "key", "something_new")
    content = with_cell(content, "settings", "value", "not a number")
    content = with_cell(content, "daily_counters", "counter", "something_else")
    report = run(tmp_path, conn, content)
    assert "something_new" not in dict(conn.execute("SELECT key, value FROM settings"))
    assert conn.execute("SELECT COUNT(*) FROM daily_counters WHERE counter = 'something_else'").fetchone() == (0,)
    assert table(report, "settings")["unknown"] == 1
    assert table(report, "settings")["already_here"] == 0
    assert any("recognise" in note and "2" in note for note in report["notes"])


# Files that are refused, leaving everything as it was.


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"\x00\x01\x02 not anything", id="random"),
        pytest.param(b"ts,pv_w\n1,2\n", id="plain-csv"),
        pytest.param(b"[1, 2]", id="json-list"),
        pytest.param(b'{"format"', id="cut-off-json"),
        pytest.param(b'{"format": "\xff"}', id="json-not-utf8"),
        pytest.param(b'{"a":' + b"[" * 200_000, id="json-too-deep"),
        pytest.param(b'{"name": "someone else\'s"}', id="other-json"),
        pytest.param(b'{"format": "solar-monitor-appliances", "version": 2, "appliances": []}', id="newer-json"),
        pytest.param(b'{"format": "solar-monitor-appliances", "version": 1, "appliances": "x"}', id="appliances-not-a-list"),
        pytest.param(b'{"format": "solar-monitor-appliances", "version": 1, "appliances": [1]}', id="appliance-not-an-object"),
        pytest.param(b'{"format": "solar-monitor-appliances", "version": 1, "appliances": [{"name": "x"}]}', id="appliance-no-watts"),
        pytest.param(b'{"format": "solar-monitor-appliances", "version": 1, "appliances": [{"name": "x", "typical_w": 2e6}]}', id="appliance-too-big"),
        pytest.param(b"SQLite format 3\x00" + b"\x00" * 2000, id="sqlite-header-only"),
        pytest.param(b"PK\x03\x04" + b"\x00" * 2000, id="zip-header-only"),
    ],
)
def test_a_file_that_is_not_an_export_is_refused(tmp_path, conn, content):
    db.save_samples(conn, [db.Sample(TS, READING)])
    assert_refused(tmp_path, conn, content)


def test_an_appliance_list_too_big_to_be_one_is_refused(tmp_path, conn, source, monkeypatch):
    content = exported(source, "appliances")
    monkeypatch.setattr(importer, "MAX_JSON_BYTES", len(content) - 1)
    assert_refused(tmp_path, conn, content)


def test_a_zip_from_elsewhere_is_refused(tmp_path, conn):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("photo.jpg", b"not a photo")
    assert_refused(tmp_path, conn, output.getvalue())


def test_a_zip_in_another_layout_is_refused(tmp_path, conn, source):
    content = rezip(exported(source, "csv"), {"README.txt": "Solar monitor export, CSV layout 2\n"})
    assert_refused(tmp_path, conn, content, match="layout")


def test_a_zip_missing_a_file_is_refused(tmp_path, conn, source):
    assert_refused(tmp_path, conn, rezip(exported(source, "csv"), {"settings.csv": None}))


def test_a_zip_missing_a_column_is_refused(tmp_path, conn, source):
    content = rezip(exported(source, "csv"), {"samples.csv": "ts,pv_w\n1800000000,5\n"})
    assert_refused(tmp_path, conn, content, match="samples.csv.*grid_w")


@pytest.mark.parametrize(
    "name, column, value",
    [
        ("samples", "ts", "1.8E+09"),  # What a spreadsheet can save it as.
        ("samples", "ts", ""),
        ("samples", "ts", "1800000000000"),  # Milliseconds.
        ("samples", "ts", "-5"),
        ("samples", "pv_w", "abc"),
        ("samples", "pv_w", "nan"),
        ("samples", "load_w", "inf"),
        ("daily_counters", "day", "15/01/2027"),
        ("daily_counters", "day", "2027-13-45"),
        ("daily_counters", "first_value", "abc"),
        ("daily_counters", "last_value", "inf"),
        ("appliances", "name", "   "),
        ("appliances", "typical_w", ""),
        ("appliances", "typical_w", "-1"),
        ("appliances", "typical_w", "nan"),
        ("appliances", "peak_w", "abc"),
        ("appliances", "use_in_outage", "maybe"),
        ("settings", "value", "abc"),
        ("settings", "value", "200000"),  # More than a tariff can be.
        ("settings", "value", "nan"),
    ],
)
def test_a_bad_cell_on_the_last_row_refuses_the_whole_file(tmp_path, conn, source, name, column, value):
    # Row 3 is where a spreadsheet shows it: the header, then the two rows.
    assert_refused(tmp_path, conn, with_cell(exported(source, "csv"), name, column, value), match="row 3")


@pytest.mark.parametrize(
    "statement",
    [
        pytest.param(f"UPDATE samples SET pv_w = 'abc' WHERE ts = {TS + DAY}", id="text-reading"),
        pytest.param(f"UPDATE samples SET raw = 'text' WHERE ts = {TS + DAY}", id="text-raw"),
        pytest.param(f"UPDATE samples SET ts = {TS * 1000} WHERE ts = {TS + DAY}", id="milliseconds"),
        pytest.param("UPDATE appliances SET typical_w = -1 WHERE name = 'Kettle'", id="negative-watts"),
        pytest.param("UPDATE settings SET value = 200000 WHERE key = 'tariff_day'", id="bad-setting"),
    ],
)
def test_a_bad_value_in_a_database_refuses_the_whole_file(tmp_path, conn, source, statement):
    assert_refused(tmp_path, conn, with_sql(tmp_path, exported(source, "sqlite"), statement), match="row 2")


@pytest.mark.parametrize(
    "statements",
    [
        pytest.param(["DROP TABLE samples"], id="no-samples"),
        pytest.param(["ALTER TABLE samples RENAME TO hidden", "CREATE VIEW samples AS SELECT * FROM hidden"], id="samples-is-a-view"),
        pytest.param(["DROP TABLE settings", "CREATE TABLE settings (key TEXT)"], id="missing-column"),
    ],
)
def test_a_database_of_the_wrong_shape_is_refused(tmp_path, conn, source, statements):
    assert_refused(tmp_path, conn, with_sql(tmp_path, exported(source, "sqlite"), *statements), match="isn't an export")


def test_a_bad_appliance_in_a_list_says_which_one(tmp_path, conn, source):
    assert_refused(tmp_path, conn, spoiled(tmp_path, source, "appliances"), match="Appliances, row 2: typical_w")


def test_an_unrelated_database_is_refused_and_not_altered(tmp_path, conn):
    other = tmp_path / "other.db"
    connection = sqlite3.connect(other)
    connection.execute("CREATE TABLE recipes (name TEXT)")
    connection.commit()
    connection.close()
    content = other.read_bytes()
    assert_refused(tmp_path, conn, content, match="isn't an export")
    assert (tmp_path / "upload").read_bytes() == content  # In particular, the app's tables weren't created in it.
    assert not list(tmp_path.glob("upload-*"))


@pytest.fixture
def big_source(tmp_path):
    """Enough readings that damage lands among them rather than in the headers."""
    connection = db.connect(tmp_path / "big.db")
    db.save_samples(connection, [db.Sample(TS + 15 * n, {"ppv": n * 1.5, "load_ptotal": n % 977, "e_total": n}) for n in range(5000)])
    yield connection
    connection.close()


def test_a_cut_off_zip_is_refused(tmp_path, conn, big_source):
    content = exported(big_source, "csv")
    assert_refused(tmp_path, conn, content[: len(content) // 2])


def test_a_zip_with_damaged_contents_is_refused(tmp_path, conn, big_source):
    content = bytearray(exported(big_source, "csv"))
    middle = len(content) // 2
    content[middle : middle + 100] = b"\xff" * 100
    assert_refused(tmp_path, conn, bytes(content), match="samples.csv")


def test_a_cut_off_database_is_refused(tmp_path, conn, big_source):
    content = exported(big_source, "sqlite")
    assert_refused(tmp_path, conn, content[: len(content) // 2], match="can't be read")


@pytest.mark.parametrize("name", FORMATS)
def test_a_refused_file_is_no_longer_held_open(tmp_path, conn, source, name):
    """Windows can't delete a file that's open, which would leave the upload behind."""
    with pytest.raises(BadFile):
        run(tmp_path, conn, spoiled(tmp_path, source, name))
    (tmp_path / "upload").unlink()


# Writing alongside the poll loop.


def test_the_lock_is_free_between_batches(tmp_path, conn, source, monkeypatch):
    """The poll loop's save holds up the whole web app while it waits, so it has to get a turn."""
    db.save_samples(source, [db.Sample(TS + 15 * n, {"ppv": n}) for n in range(1, 11)])
    other = sqlite3.connect(export.database_path(conn), timeout=0)  # Fails at once if the lock is held.
    turns = []

    def poll_loop_saves(_seconds):
        db.save_samples(other, [db.Sample(TS - 1000 - len(turns), {"ppv": 1})])
        turns.append(1)

    monkeypatch.setattr(importer, "BATCH_ROWS", 3)
    monkeypatch.setattr(importer.time, "sleep", poll_loop_saves)
    report = run(tmp_path, conn, exported(source, "sqlite"))
    other.close()
    assert len(turns) >= 4  # 12 readings in threes, then the counters.
    assert table(report, "samples")["new"] == 12
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone() == (12 + len(turns),)


def test_a_counter_saved_during_an_import_is_still_covered(tmp_path, conn, monkeypatch):
    """The poll loop can start a day, or move its latest reading on, after the import has looked."""
    older = db.connect(tmp_path / "older.db")
    with older:
        older.executemany("INSERT INTO daily_counters VALUES (?, 'pv', ?, ?)", [("2027-01-01", 1, 2), ("2027-01-02", 14, 15)])
    content = exported(older, "sqlite")
    older.close()

    def poll_loop_saves(_seconds):
        with conn:
            conn.execute("INSERT OR IGNORE INTO daily_counters VALUES ('2027-01-02', 'pv', 12, 20)")

    monkeypatch.setattr(importer, "BATCH_ROWS", 1)
    monkeypatch.setattr(importer.time, "sleep", poll_loop_saves)
    run(tmp_path, conn, content)
    assert conn.execute("SELECT first_value, last_value FROM daily_counters WHERE day = '2027-01-02'").fetchone() == (12, 20)


def test_an_import_that_stops_partway_can_be_run_again(tmp_path, conn, source, monkeypatch):
    db.save_samples(source, [db.Sample(TS + 15 * n, {"ppv": n}) for n in range(1, 11)])
    content = exported(source, "sqlite")

    def fails(_seconds):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(importer, "BATCH_ROWS", 3)
    with monkeypatch.context() as patch:
        patch.setattr(importer.time, "sleep", fails)
        with pytest.raises(sqlite3.OperationalError):
            run(tmp_path, conn, content)
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone() == (3,)
    report = run(tmp_path, conn, content)
    assert table(report, "samples")["new"] == 9
    assert contents(conn)["samples"] == contents(source)["samples"]
    (tmp_path / "upload").unlink()  # Nothing is still holding it open.


# The route.


@pytest.fixture
def collector(conn):
    return Collector("test", 0, conn)


@pytest.fixture
def call(collector):
    app = create_app(collector)

    def request(method, path, **kwargs):
        async def send():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.request(method, path, **kwargs)
        return asyncio.run(send())

    return request


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"content": b"x"}, id="no-type"),
        pytest.param({"json": {}}, id="json"),
        pytest.param({"files": {"file": b"x"}}, id="form"),
        pytest.param({"content": b"x", "headers": {"Content-Type": "text/plain"}}, id="text"),
    ],
)
def test_the_route_refuses_what_another_site_could_send(call, temp_folder, body):
    assert call("POST", "/api/import?apply=true", **body).status_code == 415
    assert list(temp_folder.iterdir()) == []


def test_the_route_previews_unless_told_to_apply(call, conn, source, temp_folder):
    content = exported(source, "sqlite")
    before = contents(conn)
    for path in ("/api/import", "/api/import?apply=false"):
        response = call("POST", path, content=content, headers=BINARY)
        assert response.status_code == 200
        assert response.json()["applied"] is False
        assert contents(conn) == before
    response = call("POST", "/api/import?apply=true", content=content, headers=BINARY)
    assert response.status_code == 200
    assert response.json()["applied"] is True
    assert table(response.json(), "samples")["new"] == 2
    assert len(contents(conn)["samples"]) == 2
    assert list(temp_folder.iterdir()) == []


@pytest.mark.parametrize("name", FORMATS)
def test_the_route_explains_a_bad_file_and_cleans_up(call, tmp_path, conn, source, temp_folder, name):
    before = contents(conn)
    response = call("POST", "/api/import?apply=true", content=spoiled(tmp_path, source, name), headers=BINARY)
    assert response.status_code == 400
    assert "Nothing was imported" in response.json()["detail"]
    assert contents(conn) == before
    assert list(temp_folder.iterdir()) == []


def test_the_route_says_when_saving_failed_and_cleans_up(call, source, temp_folder, monkeypatch):
    def fails(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(importer, "run", fails)
    response = call("POST", "/api/import?apply=true", content=exported(source, "sqlite"), headers=BINARY)
    assert response.status_code == 500
    assert "database is locked" in response.json()["detail"]
    assert "again" in response.json()["detail"]
    assert list(temp_folder.iterdir()) == []


def test_the_route_saves_buffered_readings_first(call, collector, source):
    """So that the preview doesn't count a reading that's only waiting to be saved as new."""
    collector.buffer.append(db.Sample(TS, READING))
    response = call("POST", "/api/import", content=exported(source, "sqlite"), headers=BINARY)
    assert table(response.json(), "samples")["new"] == 1
    assert collector.buffer == []


def test_a_second_import_while_one_runs_is_refused_at_once(collector, source, monkeypatch):
    content = exported(source, "sqlite")
    started = threading.Event()
    release = threading.Event()
    real = importer.run

    def slow(*args, **kwargs):
        started.set()
        release.wait(5)
        return real(*args, **kwargs)

    monkeypatch.setattr(importer, "run", slow)
    app = create_app(collector)

    async def both():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = asyncio.create_task(client.post("/api/import?apply=true", content=content, headers=BINARY))
            await asyncio.to_thread(started.wait, 5)
            try:
                second = await client.post("/api/import?apply=true", content=content, headers=BINARY)
            finally:
                release.set()
            return await first, second

    first, second = asyncio.run(both())
    assert second.status_code == 409
    assert "Another import" in second.json()["detail"]
    assert first.status_code == 200
    assert table(first.json(), "samples")["new"] == 2


def test_importing_needs_the_login(collector, conn, source):
    app = create_app(collector)
    app.add_middleware(LoginRequired, username="someone", password="something")
    response = TestClient(app, follow_redirects=False).post("/api/import?apply=true", content=exported(source, "sqlite"), headers=BINARY)
    assert response.status_code == 401
    assert contents(conn)["samples"] == []


def test_measurements_are_taken_afresh_only_after_something_was_imported(call, source, monkeypatch):
    measured = []

    def measure(samples):
        measured.append(1)
        return {}

    monkeypatch.setattr(planning, "measure_capacity", measure)
    content = exported(source, "sqlite")
    call("GET", "/api/appliances")
    assert len(measured) == 1
    call("POST", "/api/import", content=content, headers=BINARY)
    call("GET", "/api/appliances")
    assert len(measured) == 1
    call("POST", "/api/import?apply=true", content=content, headers=BINARY)
    call("GET", "/api/appliances")
    assert len(measured) == 2


def test_appliances_arrive_where_the_page_reads_them(call, source):
    call("POST", "/api/import?apply=true", content=exported(source, "appliances"), headers=BINARY)
    assert [appliance["name"] for appliance in call("GET", "/api/appliances").json()["appliances"]] == ["Crème fridge", "Kettle"]
