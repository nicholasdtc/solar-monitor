import asyncio
import csv
import io
import json
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from solar_monitor import auth, db, export
from solar_monitor.api import create_app
from solar_monitor.collector import Collector
from solar_monitor.weather.base import Warning as WeatherWarning

READING = {"ppv": 1500.5, "load_ptotal": 400, "pbattery1": -800, "battery_soc": 62, "active_power_total": 300, "e_total": 10.5}


@pytest.fixture
def collector(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    yield Collector("test", 0, conn)
    conn.close()


@pytest.fixture
def temp_folder(tmp_path, monkeypatch):
    """Where exports are written, so a test can check nothing is left behind."""
    folder = tmp_path / "temp"
    folder.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(folder))
    return folder


@pytest.fixture
def call(collector, temp_folder):
    app = create_app(collector)

    def request(method, path, **kwargs):
        async def send():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.request(method, path, **kwargs)
        return asyncio.run(send())

    return request


@pytest.fixture
def filled(collector):
    db.save_samples(collector.conn, [db.Sample(1_800_000_000, READING)])
    db.add_appliance(
        collector.conn,
        {"name": "Crème fridge", "typical_w": 120, "peak_w": None, "surge_w": 600, "use_in_outage": True, "notes": 'a "cold", one'},
    )
    db.save_settings(collector.conn, {"tariff_day": 0.25, "forecast_provider": "met-norway"})
    warning = WeatherWarning(
        "met-eireann", "7", "moderate", "Rain", "Rain, heavy", "<p>Wet, very</p>", 1_800_000_000, None, ("EI07", "EI10"), "https://x.test"
    )
    db.save_warnings(collector.conn, 1_800_000_100, "met-eireann", [warning])
    with collector.conn:
        collector.conn.execute("INSERT INTO meter_readings (day, slot, occurrence, kw) VALUES ('2026-10-25', 2, 1, 0.75)")
    return collector


def read_csv(archive, name):
    with archive.open(name) as member:
        return list(csv.DictReader(io.TextIOWrapper(member, encoding="utf-8-sig", newline="")))


def write(collector, name):
    return export.write(export.FORMATS[name], export.database_path(collector.conn))


def test_csv_zip_holds_each_table(filled, temp_folder):
    with zipfile.ZipFile(write(filled, "csv")) as archive:
        assert sorted(archive.namelist()) == [
            "README.txt", "actions.csv", "appliances.csv", "daily_counters.csv", "inverter_writes.csv",
            "meter_exports.csv", "meter_readings.csv", "samples.csv", "setting_writes.csv", "settings.csv",
            "text_settings.csv", "weather_hours.csv", "weather_warnings.csv",
        ]
        samples = read_csv(archive, "samples.csv")
        counters = read_csv(archive, "daily_counters.csv")
        appliances = read_csv(archive, "appliances.csv")
        settings = read_csv(archive, "settings.csv")
        meter_readings = read_csv(archive, "meter_readings.csv")
        text_settings = read_csv(archive, "text_settings.csv")
        warnings = read_csv(archive, "weather_warnings.csv")

    assert len(samples) == 1
    assert samples[0]["ts"] == "1800000000"
    assert samples[0]["battery_w"] == "-800.0"
    assert samples[0]["grid_w"] == "300.0"
    assert samples[0]["local_time"] == datetime.fromtimestamp(1_800_000_000).strftime("%Y-%m-%d %H:%M:%S")
    assert "raw" not in samples[0]
    assert counters[0]["counter"] == "pv"
    assert counters[0]["last_value"] == "10.5"
    assert appliances[0]["name"] == "Crème fridge"
    assert appliances[0]["notes"] == 'a "cold", one'
    assert appliances[0]["peak_w"] == ""
    assert settings == [{"key": "tariff_day", "value": "0.25"}]
    assert meter_readings == [{"day": "2026-10-25", "slot": "2", "occurrence": "1", "kw": "0.75"}]
    assert text_settings == [{"key": "forecast_provider", "value": "met-norway"}]
    assert warnings == [
        {
            "provider": "met-eireann",
            "id": "7",
            "level": "moderate",
            "kind": "Rain",
            "headline": "Rain, heavy",
            "description": "<p>Wet, very</p>",
            "onset": "1800000000",
            "expiry": "",
            "regions": '["EI07", "EI10"]',
            "link": "https://x.test",
            "first_seen": "1800000100",
            "last_seen": "1800000100",
        }
    ]


def test_csv_zip_of_an_empty_database_has_just_the_headers(collector, temp_folder):
    with zipfile.ZipFile(write(collector, "csv")) as archive:
        assert archive.read("settings.csv").decode("utf-8-sig").strip() == "key,value"


def test_csv_zip_holds_the_changes_sent_to_the_inverter(collector, temp_folder):
    write_id = db.add_inverter_write(collector.conn, 1, "aa", "bb", 40, 30)
    db.finish_inverter_write(collector.conn, write_id, "confirmed", "bb")
    with zipfile.ZipFile(write(collector, "csv")) as archive:
        row = read_csv(archive, "inverter_writes.csv")[0]
    assert (row["slot"], row["old_percent"], row["new_percent"]) == ("1", "40", "30")
    assert (row["outcome"], row["old_bytes"], row["new_bytes"], row["held_bytes"]) == ("confirmed", "aa", "bb", "bb")
    assert row["username"] == ""  # From before there were accounts.


def test_csv_zip_says_who_did_what(collector, temp_folder):
    carol = db.add_user(collector.conn, "carol", auth.hash_password("a long password"), True, ())
    db.add_inverter_write(collector.conn, 1, "aa", "bb", 40, 30, by=(carol, "carol"))
    db.add_setting_write(collector.conn, "min_soc", 20, 10, by=(carol, "carol"))
    db.add_action(collector.conn, (carol, "carol"), "data", "Added Kettle to the appliances")
    db.add_action(collector.conn, (None, ""), "login", "Failed log-in")
    with zipfile.ZipFile(write(collector, "csv")) as archive:
        inverter = read_csv(archive, "inverter_writes.csv")
        settings = read_csv(archive, "setting_writes.csv")
        actions = read_csv(archive, "actions.csv")
        readme = archive.read("README.txt").decode()
    assert inverter[0]["username"] == settings[0]["username"] == "carol"
    assert [(row["username"], row["kind"], row["summary"]) for row in actions] == [
        ("carol", "data", "Added Kettle to the appliances"),
        ("", "login", "Failed log-in"),
    ]
    assert list(actions[0]) == ["id", "ts", "local_time", "username", "kind", "summary"]
    assert readme.startswith("Solar monitor export, CSV layout 1\n")  # Older copies of the app can still read it.


def test_sqlite_export_leaves_the_accounts_behind(filled, temp_folder):
    conn = filled.conn
    password_hash = auth.hash_password("a long password")
    carol = db.add_user(conn, "carol", password_hash, True, ())
    token_hash = auth.token_hash("a session token")
    db.add_session(conn, token_hash, carol, 2_000_000_000)
    db.add_action(conn, (carol, "carol"), "login", "Logged in")
    path = write(filled, "sqlite")
    content = path.read_bytes()
    salt, key = password_hash.split("$")[-2:]
    for secret in (password_hash, salt, key, token_hash):
        assert secret.encode() not in content
        assert bytes.fromhex(secret.split("$")[-1]) not in content
    assert [item.name for item in path.parent.iterdir()] == [path.name]  # No journal or WAL beside it.
    copy = sqlite3.connect(path)
    try:
        assert copy.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert copy.execute("SELECT (SELECT COUNT(*) FROM users), (SELECT COUNT(*) FROM sessions)").fetchone() == (0, 0)
        assert [row["username"] for row in db.list_history(copy)] == ["carol"]  # The history stays.
        assert db.list_appliances(copy)[0]["name"] == "Crème fridge"
    finally:
        copy.close()
    assert db.find_user(conn, "carol") is not None  # The app's own accounts are untouched.


def test_sqlite_export_is_a_complete_working_copy(filled, temp_folder):
    copy = sqlite3.connect(write(filled, "sqlite"))
    try:
        assert copy.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.decode_raw(copy.execute("SELECT raw FROM samples").fetchone()[0]) == READING
        assert db.list_appliances(copy)[0]["name"] == "Crème fridge"
        assert db.get_settings(copy)["tariff_day"] == 0.25
    finally:
        copy.close()


def test_appliances_json_holds_only_the_appliances(filled, temp_folder):
    text = write(filled, "appliances").read_text(encoding="utf-8")
    fridge = {"name": "Crème fridge", "typical_w": 120, "peak_w": None, "surge_w": 600, "use_in_outage": True, "notes": 'a "cold", one'}
    assert json.loads(text) == {"format": "solar-monitor-appliances", "version": 1, "appliances": [fridge]}
    assert "Crème" in text  # Readable, rather than escaped.


def test_appliances_json_can_be_added_straight_back(filled, temp_folder):
    appliance = json.loads(write(filled, "appliances").read_text(encoding="utf-8"))["appliances"][0]
    new_id = db.add_appliance(filled.conn, appliance)
    assert db.list_appliances(filled.conn)[-1] == {"id": new_id, **appliance}


def test_appliances_json_of_an_empty_database_has_an_empty_list(collector, temp_folder):
    assert json.loads(write(collector, "appliances").read_text(encoding="utf-8"))["appliances"] == []


def test_a_failed_export_leaves_no_files(filled, temp_folder):
    def broken(source, path):
        path.write_text("partial")
        raise OSError("disk full")

    with pytest.raises(OSError):
        export.write(export.Format("", "", "zip", "", broken), export.database_path(filled.conn))
    assert list(temp_folder.iterdir()) == []


def test_summary_describes_what_an_export_would_hold(filled):
    db.save_samples(filled.conn, [db.Sample(1_800_000_060, READING)])
    contents = export.summary(filled.conn)
    assert contents["first_ts"] == 1_800_000_000
    assert contents["last_ts"] == 1_800_000_060
    assert contents["appliances"] == 1
    assert contents["settings"] == 1
    assert contents["meter_readings"] == 1
    assert contents["size_bytes"] > 0


def test_summary_of_an_empty_database(collector):
    contents = export.summary(collector.conn)
    assert contents["first_ts"] is None
    assert contents["last_ts"] is None
    assert contents["appliances"] == 0
    assert contents["meter_readings"] == 0


def test_api_lists_every_format(call):
    body = call("GET", "/api/export").json()
    assert [f["id"] for f in body["formats"]] == list(export.FORMATS)
    assert all(f["label"] and f["description"] for f in body["formats"])
    assert body["contents"]["appliances"] == 0


@pytest.mark.parametrize(
    ("name", "media_type", "extension"),
    [("csv", "application/zip", ".zip"), ("sqlite", "application/vnd.sqlite3", ".db"), ("appliances", "application/json", ".json")],
)
def test_download_is_an_attachment_and_cleans_up_after_itself(call, filled, temp_folder, name, media_type, extension):
    response = call("GET", "/api/export/download", params={"format": name})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(media_type)
    assert response.headers["cache-control"] == "no-store"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment")
    assert f'{extension}"' in disposition
    assert response.content
    assert list(temp_folder.iterdir()) == []


def test_download_includes_readings_not_saved_yet(call, collector):
    collector.buffer.append(db.Sample(1_800_000_000, READING))
    response = call("GET", "/api/export/download", params={"format": "csv"})
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert read_csv(archive, "samples.csv")[0]["ts"] == "1800000000"
        assert read_csv(archive, "daily_counters.csv")[0]["last_value"] == "10.5"
    assert collector.buffer == []


def test_download_of_an_unknown_format_is_refused(call):
    assert call("GET", "/api/export/download", params={"format": "exe"}).status_code == 404
    assert call("GET", "/api/export/download").status_code == 422


def test_database_path_finds_the_file(collector, tmp_path):
    assert export.database_path(collector.conn) == Path(tmp_path / "test.db")
