"""The smart meter's half-hours from ESB Networks: importing the file, and shaping it for the Grid meter tab.

Every meter file here is made up, with identifiers that belong to no real meter.
"""

import asyncio
import io
import json
import re
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import date, datetime, timedelta

import httpx
import pytest

from solar_monitor import db, export, importer, meter
from solar_monitor.api import create_app
from solar_monitor.collector import Collector
from solar_monitor.importer import BadFile

MPRN = "10000000000"
SERIAL = "TEST0001"
HEADER = "MPRN,Meter Serial Number,Read Value,Read Type,Read Date and End Time"
IMPORT = "Active Import Interval (kW)"
EXPORT = "Active Export Interval (kW)"
BINARY = {"Content-Type": "application/octet-stream"}
WINTER = date(2026, 1, 14)
SPRING = date(2026, 3, 29)  # The clocks go forward.
AUTUMN = date(2026, 10, 25)  # The clocks go back.
ONE_DAY = timedelta(days=1)


def labels(day):
    """The meter file's labels dated this day, in time order.

    Each is when a half-hour ends, so the one at 00:00 ends the day before.
    """
    slots = [slot for slot in range(48) if not (day == meter.last_sunday(day.year, 3) and slot in (2, 3))]
    if day == meter.last_sunday(day.year, 10):
        slots[4:4] = [2, 3]  # The clocks go back at 02:00, so 01:00 and 01:30 come round again.
    return [f"{day:%d-%m-%Y} {slot // 2:02d}:{slot % 2 * 30:02d}" for slot in slots]


def half_hours(*days, kw=0.5):
    """(label, kW) for every label dated these days, in time order."""
    return [(label, kw) for day in days for label in labels(day)]


def whole_days(*days, kw=0.5):
    """(label, kW) for every half-hour that starts on these days, in time order: 00:30 to the next 00:00."""
    return [(label, kw) for day in days for label in labels(day)[1:] + labels(day + ONE_DAY)[:1]]


def lines(readings, *, mprn=MPRN):
    """The meter file's lines, newest first as ESB Networks writes them."""
    return [HEADER] + [f"{mprn},{SERIAL},{kw},{IMPORT},{label}" for label, kw in reversed(readings)]


def joined(file_lines, *, bom=False):
    return (("﻿" if bom else "") + "\r\n".join(file_lines) + "\r\n").encode()


def esb(readings):
    return joined(lines(readings))


@pytest.fixture(autouse=True)
def temp_folder(tmp_path, monkeypatch):
    folder = tmp_path / "temp"
    folder.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(folder))
    return folder


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "here.db")
    yield connection
    connection.close()


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


def run(tmp_path, conn, content, *, apply=True):
    upload = tmp_path / "upload"
    upload.write_bytes(content)
    return importer.run(upload, export.database_path(conn), apply=apply)


def stored(conn, table="meter_readings"):
    return conn.execute(f"SELECT day, slot, occurrence, kw FROM {table} ORDER BY day, slot, occurrence").fetchall()


def halves(report, table="meter_readings"):
    return next(entry for entry in report["tables"] if entry["id"] == table)


def counts(report, table="meter_readings"):
    entry = halves(report, table)
    return entry["in_file"], entry["new"], entry["already_here"], entry["differ"]


def exported(conn, name):
    path = export.write(export.FORMATS[name], export.database_path(conn))
    try:
        return path.read_bytes()
    finally:
        shutil.rmtree(path.parent)


def local(year, month, day, hour=0, minute=0):
    return int(datetime(year, month, day, hour, minute).timestamp())


# Importing the file.


def test_the_clock_changes_fall_on_the_last_sundays():
    assert meter.last_sunday(2025, 3) == date(2025, 3, 30)
    assert meter.last_sunday(2025, 10) == date(2025, 10, 26)
    assert meter.last_sunday(2026, 3) == SPRING
    assert meter.last_sunday(2026, 10) == AUTUMN
    assert meter.last_sunday(2026, 12) == date(2026, 12, 27)


@pytest.mark.parametrize("bom", [False, True], ids=["plain", "byte-order-mark"])
def test_the_meter_file_is_recognised_by_what_is_in_it(tmp_path, conn, bom):
    report = run(tmp_path, conn, joined(lines(half_hours(WINTER, WINTER + ONE_DAY)), bom=bom))
    assert report["format"] == "esb"
    assert report["label"] == "Smart meter half-hours from ESB Networks"
    assert report["applied"] is True
    assert report["changes"] is True
    assert [entry["id"] for entry in report["tables"]] == ["meter_readings"]
    assert counts(report) == (96, 96, 0, 0)
    assert (halves(report)["first"], halves(report)["last"]) == ("2026-01-14", "2026-01-15")
    assert "The MPRN and the meter's serial number on each row aren't kept." in report["notes"]
    rows = stored(conn)
    assert len(rows) == 96
    assert rows[0] == ("2026-01-14", 0, 0, 0.5)
    assert rows[-1] == ("2026-01-15", 47, 0, 0.5)


def test_a_reading_is_kept_as_the_meter_gives_it(tmp_path, conn):
    readings = half_hours(WINTER)
    readings[37] = (readings[37][0], 3.125)  # 18:30, in kW.
    run(tmp_path, conn, esb(readings))
    assert conn.execute("SELECT kw FROM meter_readings WHERE slot = 37").fetchone() == (3.125,)


def test_nothing_identifying_the_meter_is_kept_or_shown(tmp_path, conn, call):
    report = run(tmp_path, conn, joined(with_export(half_hours(WINTER))))
    assert len(stored(conn, "meter_exports")) == 48
    page = call("GET", "/api/meter").text
    saved = b"".join(path.read_bytes() for path in (tmp_path / "here.db", tmp_path / "here.db-wal") if path.exists())
    csv_export = exported(conn, "csv")
    with zipfile.ZipFile(io.BytesIO(csv_export)) as archive:
        exported_text = "".join(archive.read(name).decode("utf-8-sig") for name in archive.namelist())
    for identifier in (MPRN, SERIAL):
        assert identifier not in json.dumps(report)
        assert identifier not in page
        assert identifier.encode() not in saved
        assert identifier not in exported_text


@pytest.mark.parametrize(
    "header",
    [
        "MPRN,Meter Serial Number,Read Value,Read Type,Read Date and Start Time",
        "MPRN,Meter Serial Number,Read Value,Read Type",
        "MPRN,Meter Serial Number,Read Value,Read Type,Read Date and End Time,Notes",
    ],
)
def test_another_file_from_esb_networks_is_refused(tmp_path, conn, header):
    content = joined([header, f"{MPRN},{SERIAL},0.5,{IMPORT},14-01-2026 00:00"])
    with pytest.raises(BadFile, match="not the one with each half-hour's reading in kW"):
        run(tmp_path, conn, content)
    assert stored(conn) == []


def test_a_csv_from_somewhere_else_is_refused(tmp_path, conn):
    with pytest.raises(BadFile, match="or a meter file from ESB Networks"):
        run(tmp_path, conn, b"Date,kWh\r\n14-01-2026,12\r\n")


def test_importing_the_same_file_again_adds_nothing(tmp_path, conn):
    content = esb(half_hours(WINTER))
    run(tmp_path, conn, content)
    again = run(tmp_path, conn, content)
    assert again["changes"] is False
    assert counts(again) == (48, 0, 48, 0)
    assert len(stored(conn)) == 48


def test_a_newer_file_adds_only_the_half_hours_since(tmp_path, conn):
    run(tmp_path, conn, esb(half_hours(WINTER, WINTER + ONE_DAY)))
    report = run(tmp_path, conn, esb(half_hours(WINTER + ONE_DAY, WINTER + 2 * ONE_DAY)))
    assert counts(report) == (96, 48, 48, 0)
    assert len(stored(conn)) == 144


def test_a_preview_counts_what_applying_adds_and_changes_nothing(tmp_path, conn, monkeypatch):
    monkeypatch.setattr(importer, "BATCH_ROWS", 7)  # So the file is read in several batches.
    run(tmp_path, conn, esb(half_hours(WINTER)))
    before = stored(conn)
    content = esb(half_hours(WINTER, WINTER + ONE_DAY, AUTUMN))
    preview = run(tmp_path, conn, content, apply=False)
    assert preview["applied"] is False
    assert stored(conn) == before
    applied = run(tmp_path, conn, content)
    assert preview["tables"] == applied["tables"]
    assert preview["notes"] == applied["notes"]
    assert counts(applied) == (146, 98, 48, 0)


def test_a_half_hour_that_reads_differently_keeps_the_one_already_here(tmp_path, conn):
    run(tmp_path, conn, esb(half_hours(WINTER)))
    corrected = half_hours(WINTER)
    corrected[10] = (corrected[10][0], 0.9)
    report = run(tmp_path, conn, esb(corrected))
    assert report["changes"] is False
    assert counts(report) == (48, 0, 48, 1)
    assert "Meter half-hours that read differently in the file from the ones already here, which were kept: 1." in report["notes"]
    assert conn.execute("SELECT kw FROM meter_readings WHERE slot = 10").fetchone() == (0.5,)


def test_the_hour_the_clocks_go_back_is_kept_twice(tmp_path, conn):
    # In time order: 01:00 and 01:30 summer time, then 01:00 and 01:30 again in winter time.
    values = {2: 1.0, 3: 1.5, 4: 2.0, 5: 2.5}
    readings = [(label, values.get(n, 0.5)) for n, label in enumerate(labels(AUTUMN))]
    report = run(tmp_path, conn, esb(readings))
    assert counts(report) == (50, 50, 0, 0)
    repeated = conn.execute("SELECT slot, occurrence, kw FROM meter_readings WHERE slot IN (2, 3) ORDER BY slot, occurrence")
    assert repeated.fetchall() == [(2, 0, 2.0), (2, 1, 1.0), (3, 0, 2.5), (3, 1, 1.5)]  # Occurrence 1 is the earlier.


def test_the_day_the_clocks_go_forward_has_no_hour_from_1am(tmp_path, conn):
    report = run(tmp_path, conn, esb(half_hours(SPRING)))
    assert counts(report) == (46, 46, 0, 0)
    assert {slot for _, slot, _, _ in stored(conn)} == set(range(48)) - {2, 3}


def oldest_row_is(file_lines, replacement):
    """The file with its oldest reading, the last line, replaced."""
    return joined([*file_lines[:-1], replacement])


@pytest.mark.parametrize(
    "row, problem",
    [
        (f"{MPRN},{SERIAL},abc,{IMPORT},14-01-2026 00:00", "Read Value isn't a number: abc"),
        (f"{MPRN},{SERIAL},,{IMPORT},14-01-2026 00:00", "Read Value isn't a number"),
        (f"{MPRN},{SERIAL},nan,{IMPORT},14-01-2026 00:00", "Read Value isn't a usable number"),
        (f"{MPRN},{SERIAL},-0.1,{IMPORT},14-01-2026 00:00", "Read Value is below zero: -0.1"),
        (
            f"{MPRN},{SERIAL},0.5,Reactive Import Interval (kVAr),14-01-2026 00:00",
            "Read Type is 'Reactive Import Interval (kVAr)', and only "
            "'Active Import Interval (kW)' and 'Active Export Interval (kW)' can be read",
        ),
        (f"{MPRN},{SERIAL},-0.1,{EXPORT},14-01-2026 00:00", "Read Value is below zero: -0.1"),
        (f"{MPRN},{SERIAL},0.5,{EXPORT},14-01-2026 00:15", "Read Date and End Time isn't on the hour or half past"),
        (f"10000000001,{SERIAL},0.5,{EXPORT},14-01-2026 00:00", "it's for a different meter from the rows above it"),
        (f"{MPRN},{SERIAL},0.5,{IMPORT},2026-01-14 00:00", "Read Date and End Time isn't a time like 31-12-2025 23:30"),
        (f"{MPRN},{SERIAL},0.5,{IMPORT},14/01/2026 00:00", "Read Date and End Time isn't a time like 31-12-2025 23:30"),
        (f"{MPRN},{SERIAL},0.5,{IMPORT},14-01-2026 00:15", "Read Date and End Time isn't on the hour or half past"),
        (f"{MPRN},{SERIAL},0.5,{IMPORT}", "it has 4 columns, not 5"),
        (f"{MPRN},{SERIAL},0.5,{IMPORT},14-01-2026 00:00,extra", "it has 6 columns, not 5"),
        (f"10000000001,{SERIAL},0.5,{IMPORT},14-01-2026 00:00", "it's for a different meter from the rows above it"),
        (f"{MPRN},{SERIAL},0.5,{IMPORT},14-01-2026 00:30", "14-01-2026 00:30 is in the file more than once"),
    ],
)
def test_a_bad_row_refuses_the_whole_file_and_says_which(tmp_path, conn, row, problem):
    run(tmp_path, conn, esb(half_hours(WINTER - ONE_DAY)))
    before = stored(conn)
    file_lines = lines(half_hours(WINTER))
    # Row 49 is where a spreadsheet shows it: the header, then 48 half-hours.
    with pytest.raises(BadFile, match=f"^Meter half-hours, row {len(file_lines)}: {re.escape(problem)}.*Nothing was imported") as refused:
        run(tmp_path, conn, oldest_row_is(file_lines, row))
    assert stored(conn) == before
    assert MPRN not in str(refused.value)
    assert SERIAL not in str(refused.value)


def test_a_half_hour_three_times_is_refused_even_when_the_clocks_go_back(tmp_path, conn):
    readings = half_hours(AUTUMN)
    readings.insert(6, readings[2])  # 01:00 a third time.
    with pytest.raises(BadFile, match="25-10-2026 01:00 is in the file more than once"):
        run(tmp_path, conn, esb(readings))
    assert stored(conn) == []


def with_export(readings, export_kw=0.25):
    """The meter file's lines with an export row beside each import row, newest first. Within a
    label the two come in either order, as they do in ESB Networks' files."""
    file_lines = [HEADER]
    for n, (label, kw) in enumerate(reversed(readings)):
        pair = [f"{MPRN},{SERIAL},{kw},{IMPORT},{label}", f"{MPRN},{SERIAL},{export_kw},{EXPORT},{label}"]
        file_lines += pair if n % 2 else pair[::-1]
    return file_lines


def test_export_rows_are_kept_apart_from_import(tmp_path, conn, monkeypatch):
    monkeypatch.setattr(importer, "BATCH_ROWS", 7)
    content = joined(with_export(half_hours(WINTER, AUTUMN)))
    preview = run(tmp_path, conn, content, apply=False)
    report = run(tmp_path, conn, content)
    assert [entry["id"] for entry in report["tables"]] == ["meter_readings", "meter_exports"]
    assert halves(report, "meter_exports")["label"] == "Meter export half-hours"
    assert counts(report) == (98, 98, 0, 0)
    assert counts(report, "meter_exports") == (98, 98, 0, 0)
    assert preview["tables"] == report["tables"]
    assert preview["notes"] == report["notes"]
    assert stored(conn) == stored_from(tmp_path, esb(half_hours(WINTER, AUTUMN)))
    exports = stored(conn, "meter_exports")
    assert [row[:3] for row in exports] == [row[:3] for row in stored(conn)]
    assert {kw for *_, kw in exports} == {0.25}
    again = run(tmp_path, conn, content)
    assert again["changes"] is False
    assert counts(again, "meter_exports") == (98, 0, 98, 0)


def test_export_in_the_hour_the_clocks_go_back_is_kept_twice(tmp_path, conn):
    # Newest first, so the later of each repeated label comes first.
    seen = set()
    file_lines = []
    for line in with_export(half_hours(AUTUMN)):
        label = line.split(",")[-1]
        if EXPORT in line and label in ("25-10-2026 01:00", "25-10-2026 01:30"):
            line = line.replace(f"0.25,{EXPORT}", f"{0.75 if label in seen else 0.5},{EXPORT}")
            seen.add(label)
        file_lines.append(line)
    run(tmp_path, conn, joined(file_lines))
    repeated = conn.execute("SELECT slot, occurrence, kw FROM meter_exports WHERE slot IN (2, 3) ORDER BY slot, occurrence")
    assert repeated.fetchall() == [(2, 0, 0.5), (2, 1, 0.75), (3, 0, 0.5), (3, 1, 0.75)]  # Occurrence 1 is the earlier.


def stored_from(tmp_path, content):
    other = db.connect(tmp_path / "other.db")
    try:
        run(tmp_path, other, content)
        return stored(other)
    finally:
        other.close()


def test_a_file_of_only_export_rows_adds_only_export(tmp_path, conn):
    file_lines = [line for line in with_export(half_hours(WINTER)) if EXPORT in line or line == HEADER]
    report = run(tmp_path, conn, joined(file_lines))
    assert report["changes"] is True
    assert counts(report) == (0, 0, 0, 0)
    assert counts(report, "meter_exports") == (48, 48, 0, 0)
    assert stored(conn) == []
    assert len(stored(conn, "meter_exports")) == 48


def test_a_file_from_before_export_lists_no_export(tmp_path, conn):
    report = run(tmp_path, conn, esb(half_hours(WINTER)))
    assert [entry["id"] for entry in report["tables"]] == ["meter_readings"]
    assert stored(conn, "meter_exports") == []


def test_importing_a_file_again_adds_the_export_it_left_out_before(tmp_path, conn):
    run(tmp_path, conn, esb(half_hours(WINTER)))  # As the version before this one did: import only.
    report = run(tmp_path, conn, joined(with_export(half_hours(WINTER))))
    assert report["changes"] is True
    assert counts(report) == (48, 0, 48, 0)
    assert counts(report, "meter_exports") == (48, 48, 0, 0)


def test_an_export_half_hour_that_reads_differently_keeps_the_one_already_here(tmp_path, conn):
    run(tmp_path, conn, joined(with_export(half_hours(WINTER))))
    report = run(tmp_path, conn, joined(with_export(half_hours(WINTER), export_kw=0.3)))
    assert report["changes"] is False
    assert counts(report, "meter_exports") == (48, 0, 48, 48)
    note = "Meter export half-hours that read differently in the file from the ones already here, which were kept: 48."
    assert note in report["notes"]
    assert {kw for *_, kw in stored(conn, "meter_exports")} == {0.25}


def test_an_import_half_hour_twice_is_refused_beside_export_rows(tmp_path, conn):
    file_lines = with_export(half_hours(WINTER))
    file_lines.append(f"{MPRN},{SERIAL},0.5,{IMPORT},14-01-2026 00:30")
    with pytest.raises(BadFile, match=f"^Meter half-hours, row {len(file_lines)}: 14-01-2026 00:30 is in the file more than once"):
        run(tmp_path, conn, joined(file_lines))
    assert stored(conn) == []


def test_a_bad_row_after_export_rows_names_its_own_row(tmp_path, conn):
    file_lines = with_export(half_hours(WINTER))
    file_lines[-3] = re.sub(r",[\d.]+,", ",abc,", file_lines[-3])
    with pytest.raises(BadFile, match=f"^Meter half-hours, row {len(file_lines) - 2}: Read Value isn't a number: abc"):
        run(tmp_path, conn, joined(file_lines))


def test_a_file_too_big_to_be_a_meter_file_is_refused(tmp_path, conn, monkeypatch):
    content = esb(half_hours(WINTER))
    monkeypatch.setattr(importer, "MAX_METER_BYTES", len(content) - 1)
    with pytest.raises(BadFile, match="too big"):
        run(tmp_path, conn, content)


def test_the_route_imports_a_meter_file(call, conn):
    response = call("POST", "/api/import?apply=true", content=esb(half_hours(WINTER)), headers=BINARY)
    assert response.status_code == 200
    assert response.json()["format"] == "esb"
    assert len(stored(conn)) == 48


# Through this app's own exports.


@pytest.mark.parametrize("name", ["csv", "sqlite"])
def test_meter_half_hours_come_back_from_an_export(tmp_path, conn, name):
    values = {2: 1.0, 3: 1.5, 4: 2.0, 5: 2.5}
    readings = half_hours(WINTER) + [(label, values.get(n, 0.25)) for n, label in enumerate(labels(AUTUMN))]
    run(tmp_path, conn, joined(with_export(readings, export_kw=0.125)))
    with conn:
        conn.execute("UPDATE meter_exports SET kw = 0.375 WHERE day = ? AND slot = 2 AND occurrence = 1", (AUTUMN.isoformat(),))
    copy = db.connect(tmp_path / "copy.db")
    try:
        report = run(tmp_path, copy, exported(conn, name))
        assert report["format"] == name
        assert counts(report) == (98, 98, 0, 0)
        assert counts(report, "meter_exports") == (98, 98, 0, 0)
        assert stored(copy) == stored(conn)
        assert stored(copy, "meter_exports") == stored(conn, "meter_exports")
        assert (AUTUMN.isoformat(), 2, 1, 0.375) in stored(copy, "meter_exports")
        again = run(tmp_path, copy, exported(conn, name))
        assert counts(again) == (98, 0, 98, 0)
        assert counts(again, "meter_exports") == (98, 0, 98, 0)
    finally:
        copy.close()


def without_meter_readings(tmp_path, content, name):
    """The export as it was before the app held the meter's half-hours."""
    if name == "csv":
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(content)) as before, zipfile.ZipFile(output, "w") as after:
            for member in before.namelist():
                if member not in ("meter_readings.csv", "meter_exports.csv"):
                    after.writestr(member, before.read(member))
        return output.getvalue()
    path = tmp_path / "edited.db"
    path.write_bytes(content)
    connection = sqlite3.connect(path)
    with connection:
        connection.execute("DROP TABLE meter_readings")
        connection.execute("DROP TABLE meter_exports")
    connection.close()
    edited = path.read_bytes()
    path.unlink()
    return edited


@pytest.mark.parametrize("name", ["csv", "sqlite"])
def test_an_export_from_before_the_meter_tab_still_imports(tmp_path, conn, name):
    source = db.connect(tmp_path / "source.db")
    try:
        db.save_samples(source, [db.Sample(1_800_000_000, {"ppv": 1500, "e_total": 10.5})])
        content = without_meter_readings(tmp_path, exported(source, name), name)
    finally:
        source.close()
    report = run(tmp_path, conn, content)
    assert {"meter_readings", "meter_exports"}.isdisjoint(entry["id"] for entry in report["tables"])
    assert next(entry for entry in report["tables"] if entry["id"] == "samples")["new"] == 1


@pytest.mark.parametrize(
    "row, problem",
    [
        ("2026-01-14,10,1,0.5", "occurrence is 1 for a half-hour that only comes once"),
        ("2026-01-14,48,0,0.5", "slot isn't a whole number from 0 to 47"),
        ("2026-01-14,1.5,0,0.5", "slot isn't a whole number from 0 to 47"),
        ("2026-01-14,10,2,0.5", "occurrence isn't a whole number from 0 to 1"),
        ("14-01-2026,10,0,0.5", "day isn't in the form YYYY-MM-DD"),
        ("2026-01-14,10,0,-1", "kw is below zero"),
    ],
)
def test_a_bad_meter_half_hour_in_an_export_is_refused(tmp_path, conn, row, problem):
    source = db.connect(tmp_path / "source.db")
    try:
        content = exported(source, "csv")
    finally:
        source.close()
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(content)) as before, zipfile.ZipFile(output, "w") as after:
        for member in before.namelist():
            after.writestr(member, f"day,slot,occurrence,kw\r\n{row}\r\n" if member == "meter_readings.csv" else before.read(member))
    with pytest.raises(BadFile, match=f"Meter half-hours, row 2: {re.escape(problem)}"):
        run(tmp_path, conn, output.getvalue())
    assert stored(conn) == []


# Shaped for the page.


def test_no_half_hours_yet(conn):
    assert meter.readings(conn) == {"days": [], "file": None, "export": None}


def test_each_day_has_its_48_half_hours_in_watts(tmp_path, conn):
    readings = whole_days(WINTER, kw=0.25)
    readings[5] = (readings[5][0], 0)
    run(tmp_path, conn, esb(readings))
    payload = meter.readings(conn)
    [(day, flags, slots, twice)] = payload["days"]
    assert (day, flags, twice) == ("2026-01-14", "", [])
    assert slots == [250] * 5 + [0] + [250] * 42
    assert payload["file"] == {
        "first": "2026-01-14 00:00",
        "last": "2026-01-14 23:30",
        "readings": 48,
        "zeros": 1,
        "missing": 0,
        "springDays": 0,
        "doubledDays": 0,
        "gaps": [],
        "gapCount": 0,
    }
    assert payload["export"] is None


def test_each_half_hour_shows_from_its_start_though_the_file_labels_its_end(tmp_path, conn):
    labelled = dict(whole_days(WINTER))
    labelled["14-01-2026 18:30"] = 3.0  # 18:00 to 18:30.
    labelled["15-01-2026 00:00"] = 2.0  # 23:30 to midnight.
    run(tmp_path, conn, esb(list(labelled.items())))
    payload = meter.readings(conn)
    [(day, flags, slots, _)] = payload["days"]
    assert (day, flags) == ("2026-01-14", "")
    assert (slots[36], slots[47]) == (3000, 2000)
    assert slots.count(500) == 46
    assert (payload["file"]["first"], payload["file"]["last"]) == ("2026-01-14 00:00", "2026-01-14 23:30")


def test_the_label_at_midnight_is_the_day_befores_last_half_hour(tmp_path, conn):
    run(tmp_path, conn, esb([("14-01-2026 00:00", 1.0), ("14-01-2026 00:30", 0.5)]))
    payload = meter.readings(conn)
    before, day = payload["days"]
    assert before[:2] == ["2026-01-13", "b"]
    assert before[2] == [meter.NOT_A_HALF_HOUR] * 47 + [1000]
    assert day[:2] == ["2026-01-14", "p"]
    assert day[2] == [500] + [meter.NOT_A_HALF_HOUR] * 47
    assert (payload["file"]["first"], payload["file"]["last"]) == ("2026-01-13 23:30", "2026-01-14 00:00")


def test_the_half_hours_either_side_of_the_clock_changes(tmp_path, conn):
    spring, autumn = whole_days(SPRING), whole_days(AUTUMN)
    assert (spring[1][0], autumn[5][0]) == ("29-03-2026 02:00", "25-10-2026 02:00")
    spring[1] = (spring[1][0], 1.25)  # 00:30 to 01:00, when the clocks go forward to 02:00.
    autumn[5] = (autumn[5][0], 1.75)  # 01:30 to 02:00 in winter time, after the clocks went back.
    run(tmp_path, conn, esb(spring + autumn))
    days = {day: (slots, twice) for day, _, slots, twice in meter.readings(conn)["days"]}
    assert days["2026-03-29"][0][1:5] == [1250, meter.NOT_A_HALF_HOUR, meter.NOT_A_HALF_HOUR, 500]
    assert days["2026-10-25"][1] == [[2, 500, 500], [3, 500, 1750]]


def test_a_file_that_starts_and_stops_partway_through_a_day(tmp_path, conn):
    readings = whole_days(WINTER, WINTER + ONE_DAY, WINTER + 2 * ONE_DAY)[10:-20]
    del readings[40:43]  # Three half-hours missing on the second day, from 01:00.
    run(tmp_path, conn, esb(readings))
    payload = meter.readings(conn)
    first, second, last = payload["days"]
    assert first[1] == "b"
    assert first[2][:10] == [meter.NOT_A_HALF_HOUR] * 10
    assert first[2][10] == 500
    assert second[1] == ""
    assert second[2][1:6] == [500, None, None, None, 500]
    assert last[1] == "p"
    assert last[2][27] == 500
    assert last[2][28:] == [meter.NOT_A_HALF_HOUR] * 20
    assert payload["file"]["first"] == "2026-01-14 05:00"
    assert payload["file"]["last"] == "2026-01-16 13:30"
    assert payload["file"]["missing"] == 3
    assert payload["file"]["gaps"] == [["2026-01-15 01:00", 3]]
    assert payload["file"]["gapCount"] == 1


def test_days_missing_altogether_are_there_with_no_readings(tmp_path, conn):
    run(tmp_path, conn, esb(whole_days(WINTER, WINTER + 3 * ONE_DAY)))
    payload = meter.readings(conn)
    assert [day[0] for day in payload["days"]] == ["2026-01-14", "2026-01-15", "2026-01-16", "2026-01-17"]
    assert payload["days"][1][2] == [None] * 48
    assert payload["file"]["missing"] == 96
    assert payload["file"]["gaps"] == [["2026-01-15 00:00", 96]]


def test_gaps_past_the_listed_ones_are_only_counted(tmp_path, conn, monkeypatch):
    monkeypatch.setattr(meter, "MAX_GAPS", 2)
    readings = whole_days(WINTER)
    del readings[30], readings[20], readings[10]
    run(tmp_path, conn, esb(readings))
    file = meter.readings(conn)["file"]
    assert file["gaps"] == [["2026-01-14 05:00", 1], ["2026-01-14 10:00", 1]]
    assert file["gapCount"] == 3


def test_the_clock_change_days_are_marked(tmp_path, conn):
    # In time order, from 01:00 summer time: 01:00 and 01:30, then the same again in winter time.
    values = {2: 1.0, 3: 1.5, 4: 2.0, 5: 2.5}
    autumn = [(label, values.get(n, 0.5)) for n, (label, _) in enumerate(whole_days(AUTUMN))]
    run(tmp_path, conn, esb(whole_days(SPRING) + autumn))
    payload = meter.readings(conn)
    days = {day: (flags, slots, twice) for day, flags, slots, twice in payload["days"]}
    flags, slots, twice = days["2026-03-29"]
    assert (flags, twice) == ("s", [])
    assert slots[2:4] == [meter.NOT_A_HALF_HOUR] * 2
    flags, slots, twice = days["2026-10-25"]
    assert flags == "a"
    assert twice == [[2, 1000, 2000], [3, 1500, 2500]]  # The earlier, then the later.
    assert slots[2:4] == [1500, 2000]  # Their average.
    assert (payload["file"]["springDays"], payload["file"]["doubledDays"]) == (1, 1)
    assert payload["file"]["readings"] == 96
    assert payload["file"]["missing"] == 48 * ((AUTUMN - SPRING).days - 1)  # Every day between.
    assert payload["file"]["gaps"] == [["2026-03-30 00:00", 48 * ((AUTUMN - SPRING).days - 1)]]


def test_export_is_shaped_like_import_from_when_the_meter_starts_recording_it(tmp_path, conn):
    imported = whole_days(WINTER, WINTER + ONE_DAY)
    sent = dict(imported[30:])  # Export from 15:00 on the first day.
    sent["14-01-2026 18:30"] = 2.5  # 18:00 to 18:30.
    del sent["15-01-2026 01:00"]  # 00:30 to 01:00 missing.
    file_lines = [HEADER] + [f"{MPRN},{SERIAL},{kw},{IMPORT},{label}" for label, kw in reversed(imported)]
    file_lines += [f"{MPRN},{SERIAL},{kw},{EXPORT},{label}" for label, kw in reversed(list(sent.items()))]
    run(tmp_path, conn, joined(file_lines))
    payload = meter.readings(conn)
    assert len(payload["days"]) == 2
    sent_out = payload["export"]
    assert (sent_out["first"], sent_out["last"], sent_out["readings"]) == ("2026-01-14 15:00", "2026-01-15 23:30", 65)
    assert list(sent_out["days"]) == ["2026-01-14", "2026-01-15"]
    slots, twice = sent_out["days"]["2026-01-14"]
    assert slots[:30] == [meter.NOT_A_HALF_HOUR] * 30
    assert (slots[30], slots[36], twice) == (500, 2500, [])
    slots, _ = sent_out["days"]["2026-01-15"]
    assert slots[:3] == [500, None, 500]


def test_export_in_the_hour_the_clocks_go_back_shows_both(tmp_path, conn):
    values = {2: 1.0, 3: 1.5, 4: 2.0, 5: 2.5}
    readings = [(label, values.get(n, 0.5)) for n, (label, _) in enumerate(whole_days(AUTUMN))]
    run(tmp_path, conn, joined(with_export(readings, export_kw=0.125)))
    slots, twice = meter.readings(conn)["export"]["days"]["2026-10-25"]
    assert twice == [[2, 125, 125], [3, 125, 125]]
    assert meter.NOT_A_HALF_HOUR not in slots


# The tariff's bands, and this app's own readings to compare with.


def test_the_usual_bands_until_the_households_own_are_entered():
    assert meter.tariff({}) == {
        "rates": {"day": None, "peak": None, "night": None, "export": None},
        "bands": {"night_start": 1380, "night_end": 480, "peak_start": 1020, "peak_end": 1140},
        "bandsSet": False,
    }


def test_the_households_own_bands_and_rates():
    settings = {
        "tariff_day": 0.3,
        "tariff_night": 0.15,
        "band_night_start_minutes": 0,
        "band_night_end_minutes": 420,
        "band_peak_start_minutes": 960,
    }
    result = meter.tariff(settings)
    assert result["rates"] == {"day": 0.3, "peak": None, "night": 0.15, "export": None}
    assert result["bands"] == {"night_start": 0, "night_end": 420, "peak_start": 960, "peak_end": 1140}
    assert result["bandsSet"] is True  # Any one entered is the household's own, and the rest stay usual.
    assert meter.tariff({**settings, "band_peak_end_minutes": 1080.0})["bands"]["peak_end"] == 1080
    assert meter.tariff({"band_peak_start_minutes": 960})["bandsSet"] is True
    assert meter.tariff({key: None for key in meter.DEFAULT_BANDS})["bandsSet"] is False


@pytest.mark.parametrize("minutes", [0, 30, 480, 1410])
def test_a_band_time_on_the_half_hour_is_saved(call, minutes):
    response = call("PUT", "/api/settings", json={"band_night_start_minutes": minutes})
    assert response.status_code == 200
    assert call("GET", "/api/meter").json()["bands"]["night_start"] == minutes


@pytest.mark.parametrize("minutes", [15, 1440, -30, 1410.5, "23:00"])
def test_a_band_time_off_the_half_hour_is_refused(call, minutes):
    assert call("PUT", "/api/settings", json={"band_peak_end_minutes": minutes}).status_code == 422


def test_a_band_time_can_be_cleared_back_to_the_usual_one(call):
    call("PUT", "/api/settings", json={"band_peak_end_minutes": 1200})
    assert call("GET", "/api/meter").json()["bandsSet"] is True
    call("PUT", "/api/settings", json={"band_peak_end_minutes": None})
    body = call("GET", "/api/meter").json()
    assert body["bands"]["peak_end"] == 1140
    assert body["bandsSet"] is False


def grid_at(ts, kwh):
    return db.Sample(ts, {"meter_e_total_imp": kwh})


def test_this_apps_whole_days_need_readings_just_before_both_midnights(conn):
    db.save_samples(
        conn,
        [
            grid_at(local(2026, 1, 9, 23, 55), 100.0),
            grid_at(local(2026, 1, 10, 12), 104.0),
            grid_at(local(2026, 1, 10, 23, 55), 110.0),  # The 10th is whole: 10 kWh.
            grid_at(local(2026, 1, 11, 23, 30), 113.0),  # Too long before midnight, so the 11th isn't.
            grid_at(local(2026, 1, 12, 23, 52), 120.0),  # Nor the 12th, which starts from that reading.
            grid_at(local(2026, 1, 13, 23, 58), 126.0),  # The 13th is whole.
            grid_at(local(2026, 1, 14, 23, 58), 131.0),  # Today, not over yet.
        ],
    )
    result = meter.app_days(conn, today=date(2026, 1, 14))
    assert result["days"] == [["2026-01-10", 10.0], ["2026-01-13", 6.0]]
    assert result["first"] == "2026-01-09 23:55"
    assert result["last"] == "2026-01-14 23:58"


def test_no_readings_of_this_apps_own(conn):
    assert meter.app_days(conn) == {"first": None, "last": None, "days": []}


# The route.


def test_the_route_before_any_meter_file(call):
    body = call("GET", "/api/meter").json()
    assert body["days"] == []
    assert body["file"] is None
    assert body["bandsSet"] is False
    assert body["app"] == {"first": None, "last": None, "days": []}


def test_the_route_shows_a_meter_file_as_soon_as_it_is_imported(call, conn):
    assert call("GET", "/api/meter").json()["days"] == []  # Remembered until something is imported.
    preview = call("POST", "/api/import", content=esb(half_hours(WINTER)), headers=BINARY)
    assert preview.json()["applied"] is False
    assert call("GET", "/api/meter").json()["days"] == []
    call("POST", "/api/import?apply=true", content=esb(whole_days(WINTER)), headers=BINARY)
    body = call("GET", "/api/meter").json()
    assert [day[0] for day in body["days"]] == ["2026-01-14"]
    assert body["file"]["readings"] == 48


def test_the_route_includes_readings_not_saved_yet(call, collector):
    collector.buffer.append(grid_at(1_800_000_000, 50.0))
    body = call("GET", "/api/meter").json()
    assert body["app"]["first"] == datetime.fromtimestamp(1_800_000_000).strftime("%Y-%m-%d %H:%M")
    assert collector.buffer == []


def test_the_route_sends_settings_changed_since_it_last_sent(call):
    call("GET", "/api/meter")
    call("PUT", "/api/settings", json={"tariff_day": 0.31})
    assert call("GET", "/api/meter").json()["rates"]["day"] == 0.31
