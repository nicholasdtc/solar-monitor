from datetime import date, datetime

import pytest

from solar_monitor import db


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "nested" / "test.db")
    yield connection
    connection.close()


def at(*when) -> int:
    return int(datetime(*when).timestamp())


def totals(conn, counter):
    return {day: kwh for day, name, kwh in db.daily_totals(conn) if name == counter}


def test_uses_wal_mode(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_columns_keep_readings_as_reported(conn):
    values = {"ppv": 700, "load_ptotal": 300, "pbattery1": -350, "battery_soc": 64, "active_power_total": -1400}
    db.save_samples(conn, [db.Sample(1000, values)])
    row = conn.execute("SELECT pv_w, load_w, battery_w, battery_soc, grid_w FROM samples").fetchone()
    assert row == (700, 300, -350, 64, -1400)


def test_raw_keeps_every_reading(conn):
    values = {"ppv": 700, "work_mode_label": "Normal", "timestamp": datetime(2026, 11, 6, 12, 0)}
    db.save_samples(conn, [db.Sample(1000, values)])
    raw = conn.execute("SELECT raw FROM samples").fetchone()[0]
    assert db.decode_raw(raw) == {"ppv": 700, "work_mode_label": "Normal", "timestamp": "2026-11-06 12:00:00"}


def test_missing_or_non_numeric_reading_is_null(conn):
    db.save_samples(conn, [db.Sample(1000, {"ppv": "n/a"})])
    assert conn.execute("SELECT pv_w, load_w FROM samples").fetchone() == (None, None)


def test_saving_a_sample_twice_keeps_one_row(conn):
    sample = db.Sample(1000, {"ppv": 700})
    db.save_samples(conn, [sample])
    db.save_samples(conn, [sample])
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1


def test_failed_batch_saves_nothing(conn):
    conn.execute("CREATE TRIGGER fail BEFORE INSERT ON daily_counters BEGIN SELECT RAISE(ABORT, 'boom'); END")
    with pytest.raises(Exception, match="boom"):
        db.save_samples(conn, [db.Sample(1000, {"ppv": 700, "e_total": 5.0})])
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 0


def test_grid_counters_come_from_the_meter_not_the_inverter_ac_side(conn):
    values = {"meter_e_total_imp": 67.1, "meter_e_total_exp": 7.0, "e_total_imp": 45.2, "e_total_exp": 37.8}
    db.save_samples(conn, [db.Sample(at(2026, 11, 6, 12), values)])
    stored = dict(conn.execute("SELECT counter, last_value FROM daily_counters"))
    assert stored == {"grid_import": 67.1, "grid_export": 7.0, "inverter_ac_in": 45.2, "inverter_ac_out": 37.8}


def test_first_day_counts_from_its_own_first_reading(conn):
    db.save_samples(conn, [
        db.Sample(at(2026, 11, 6, 8), {"e_total": 10.0}),
        db.Sample(at(2026, 11, 6, 16), {"e_total": 12.5}),
    ])
    assert totals(conn, "pv") == {"2026-11-06": 2.5}


def test_next_day_counts_from_the_previous_days_last_reading(conn):
    # The overnight energy between the two readings belongs to the 7th, and isn't lost.
    db.save_samples(conn, [
        db.Sample(at(2026, 11, 6, 16), {"e_total": 12.0}),
        db.Sample(at(2026, 11, 7, 9), {"e_total": 12.4}),
        db.Sample(at(2026, 11, 7, 16), {"e_total": 15.0}),
    ])
    assert totals(conn, "pv")["2026-11-07"] == 3.0


def test_a_gap_in_polling_catches_up_on_the_next_recorded_day(conn):
    db.save_samples(conn, [
        db.Sample(at(2026, 11, 6, 16), {"e_total": 12.0}),
        db.Sample(at(2026, 11, 9, 16), {"e_total": 20.0}),
    ])
    assert totals(conn, "pv")["2026-11-09"] == 8.0


def test_batch_across_midnight_is_split_into_its_local_days(conn):
    db.save_samples(conn, [
        db.Sample(at(2026, 11, 6, 23, 59), {"e_total": 12.0}),
        db.Sample(at(2026, 11, 7, 0, 1), {"e_total": 12.1}),
    ])
    assert dict(conn.execute("SELECT day, last_value FROM daily_counters")) == {
        "2026-11-06": 12.0,
        "2026-11-07": 12.1,
    }


def test_restart_within_a_day_keeps_the_days_first_reading(conn):
    db.save_samples(conn, [db.Sample(at(2026, 11, 6, 8), {"e_total": 10.0})])
    db.save_samples(conn, [db.Sample(at(2026, 11, 6, 9), {"e_total": 11.0})])
    assert conn.execute("SELECT first_value, last_value FROM daily_counters").fetchone() == (10.0, 11.0)


def test_counter_that_drops_counts_as_zero_rather_than_negative(conn):
    db.save_samples(conn, [
        db.Sample(at(2026, 11, 6, 16), {"e_total": 500.0}),
        db.Sample(at(2026, 11, 7, 16), {"e_total": 1.0}),
    ])
    assert totals(conn, "pv")["2026-11-07"] == 0


def power(ts, ppv):
    return db.Sample(ts, {"ppv": ppv})


def test_bucket_grows_with_the_range_and_stays_within_the_point_limit():
    for span in (3600, 6 * 3600, 86400, 7 * 86400, 30 * 86400, 365 * 86400):
        assert span / db.bucket_seconds(span) <= db.MAX_HISTORY_POINTS
    assert db.bucket_seconds(3600) == 60
    assert db.bucket_seconds(30 * 86400) == 3600


def test_history_averages_each_bucket_and_keeps_the_sign(conn):
    db.save_samples(conn, [
        db.Sample(600, {"pbattery1": -100, "ppv": 10}),
        db.Sample(615, {"pbattery1": -300, "ppv": 20}),
        db.Sample(660, {"pbattery1": 50, "ppv": 30}),
    ])
    bucket, points = db.history(conn, 0, 3600)
    assert bucket == 60
    assert [(p["ts"], p["battery_w"], p["pv_w"]) for p in points] == [(600, -200, 15), (660, 50, 30)]


def test_history_only_returns_the_requested_range(conn):
    db.save_samples(conn, [power(100, 1), power(200, 2), power(300, 3)])
    _, points = db.history(conn, 200, 300)
    assert [p["pv_w"] for p in points] == [2]


def test_history_leaves_a_gap_where_there_are_no_readings(conn):
    db.save_samples(conn, [power(0, 1), power(600, 2)])
    _, points = db.history(conn, 0, 3600)
    assert [p["ts"] for p in points] == [0, 600]


def test_history_of_an_empty_range_has_no_points(conn):
    assert db.history(conn, 0, 3600) == (60, [])


def test_history_includes_unsaved_readings(conn):
    db.save_samples(conn, [power(600, 10)])
    _, points = db.history(conn, 0, 3600, [power(615, 30), power(660, 50)])
    assert [(p["ts"], p["pv_w"]) for p in points] == [(600, 20), (660, 50)]


def test_history_ignores_unsaved_readings_outside_the_range(conn):
    _, points = db.history(conn, 0, 3600, [power(3600, 50)])
    assert points == []


def test_history_keeps_a_missing_unsaved_reading_empty_rather_than_zero(conn):
    _, points = db.history(conn, 0, 3600, [db.Sample(600, {"ppv": "n/a"})])
    assert points[0]["pv_w"] is None


def test_history_gives_each_power_reading_its_lowest_and_highest(conn):
    db.save_samples(conn, [
        db.Sample(600, {"pbattery1": -100, "ppv": 10, "battery_soc": 50}),
        db.Sample(615, {"pbattery1": 300, "ppv": 20, "battery_soc": 51}),
    ])
    _, [point] = db.history(conn, 0, 3600, [db.Sample(630, {"pbattery1": 50, "ppv": 5})])
    assert (point["battery_w_min"], point["battery_w_max"]) == (-100, 300)
    assert (point["pv_w_min"], point["pv_w_max"]) == (5, 20)
    assert "battery_soc_min" not in point


def test_history_leaves_the_spread_of_a_missing_reading_empty(conn):
    _, [point] = db.history(conn, 0, 3600, [db.Sample(600, {"ppv": 10})])
    assert (point["grid_w_min"], point["grid_w_max"]) == (None, None)


def test_history_uses_the_bucket_it_is_given(conn):
    db.save_samples(conn, [power(at(2026, 11, 6, 12, minute), minute) for minute in (0, 10, 20, 40)])
    bucket, points = db.history(conn, at(2026, 11, 6, 12), at(2026, 11, 6, 13), bucket=1800)
    assert bucket == 1800
    assert [(p["ts"], p["pv_w"]) for p in points] == [(at(2026, 11, 6, 12), 10), (at(2026, 11, 6, 12, 30), 40)]


def test_daily_buckets_start_at_local_midnight(conn):
    db.save_samples(conn, [
        db.Sample(at(2026, 11, 6, 0, 30), {"pbattery1": -100, "ppv": 0}),
        db.Sample(at(2026, 11, 6, 23, 30), {"pbattery1": 300, "ppv": 4}),
        db.Sample(at(2026, 11, 7, 0, 30), {"ppv": 8}),
    ])
    _, points = db.history(conn, at(2026, 11, 6), at(2026, 11, 8), bucket=86400)
    assert [(p["ts"], p["pv_w"], p["battery_w_min"], p["battery_w_max"]) for p in points] == [
        (at(2026, 11, 6), 2, -100, 300),
        (at(2026, 11, 7), 8, None, None),
    ]


def test_a_day_when_the_clocks_change_is_still_one_bucket(conn):
    # Where the clocks go back on 25 October, that day runs for 25 hours, and the next starts at midnight.
    db.save_samples(conn, [power(at(2026, 10, 25, 0, 30), 1), power(at(2026, 10, 25, 23, 30), 3), power(at(2026, 10, 26, 0, 30), 5)])
    _, points = db.history(conn, at(2026, 10, 24), at(2026, 10, 27), bucket=86400)
    assert [(p["ts"], p["pv_w"]) for p in points] == [(at(2026, 10, 25), 2), (at(2026, 10, 26), 5)]


def test_unsaved_readings_join_the_bucket_of_saved_ones(conn):
    db.save_samples(conn, [power(at(2026, 11, 6, 12), 10)])
    _, [point] = db.history(conn, at(2026, 11, 6), at(2026, 11, 7), [power(at(2026, 11, 6, 20), 30)], bucket=86400)
    assert (point["pv_w"], point["pv_w_min"], point["pv_w_max"]) == (20, 10, 30)


def test_whole_buckets_widens_a_range_to_the_periods_it_touches():
    assert db.whole_buckets(at(2026, 11, 6, 12, 10), at(2026, 11, 6, 13, 10), 1800) == (
        at(2026, 11, 6, 12), at(2026, 11, 6, 13, 30)
    )
    assert db.whole_buckets(at(2026, 11, 6), at(2026, 11, 7), 86400) == (at(2026, 11, 6), at(2026, 11, 7))
    assert db.whole_buckets(at(2026, 10, 25, 12), at(2026, 10, 25, 13), 86400) == (at(2026, 10, 25), at(2026, 10, 26))


def test_first_reading_is_the_oldest_saved_or_unsaved(conn):
    assert db.first_reading(conn) is None
    assert db.first_reading(conn, [power(900, 1)]) == 900
    db.save_samples(conn, [power(600, 1), power(1200, 1)])
    assert db.first_reading(conn, [power(900, 1)]) == 600


def flow(ts, pv=0, load=0, battery=0, grid=0):
    return db.Sample(ts, {"ppv": pv, "load_ptotal": load, "pbattery1": battery, "active_power_total": grid})


def test_daily_flows_send_solar_to_the_house_then_the_battery_then_the_grid(conn):
    start = at(2026, 11, 6, 12)
    # 3 kW of solar, 1 kW to the house, 1.5 kW charging, 0.4 kW exported: 0.1 kW is lost in conversion.
    db.save_samples(conn, [flow(start + 60 * i, pv=3000, load=1000, battery=-1500, grid=400) for i in range(61)])
    assert db.daily_flows(conn, start, start + 7200) == {"2026-11-06": {
        "solar_home": 1.0, "solar_battery": 1.5, "solar_export": 0.4, "battery_home": 0.0, "grid_home": 0.0,
    }}


def test_daily_flows_take_the_house_from_solar_then_the_battery_then_the_grid(conn):
    start = at(2026, 11, 6, 18)
    db.save_samples(conn, [flow(start + 60 * i, pv=200, load=2000, battery=1000, grid=-900) for i in range(61)])
    assert db.daily_flows(conn, start, start + 7200)["2026-11-06"] == {
        "solar_home": 0.2, "solar_battery": 0.0, "solar_export": 0.0, "battery_home": 1.0, "grid_home": 0.8,
    }


def test_daily_flows_count_no_more_than_a_minute_per_reading_across_a_dropout(conn):
    start = at(2026, 11, 6, 12)
    db.save_samples(conn, [flow(start, load=1200, grid=-1200), flow(start + 3600, load=1200, grid=-1200)])
    # The first reading counts for a minute, not the hour until the next. The last has nothing after it.
    assert db.daily_flows(conn, start, start + 7200)["2026-11-06"]["grid_home"] == 0.02


def test_daily_flows_split_at_local_midnight(conn):
    midnight = at(2026, 11, 7)
    db.save_samples(conn, [flow(midnight + 60 * i, load=3600, grid=-3600) for i in range(-2, 3)])
    days = db.daily_flows(conn, midnight - 3600, midnight + 3600)
    assert {day: kwh["grid_home"] for day, kwh in days.items()} == {"2026-11-06": 0.12, "2026-11-07": 0.12}


def test_daily_flows_include_unsaved_readings(conn):
    start = at(2026, 11, 6, 12)
    db.save_samples(conn, [flow(start, pv=3600, load=3600)])
    days = db.daily_flows(conn, start, start + 3600, [flow(start + 60, pv=3600, load=3600)])
    assert days["2026-11-06"]["solar_home"] == 0.06


def test_daily_series_fills_the_calendar_and_shows_days_with_no_readings(conn):
    db.save_samples(conn, [
        db.Sample(at(2026, 11, 4, 8), {"e_total": 10.0}),
        db.Sample(at(2026, 11, 4, 16), {"e_total": 12.0}),
        db.Sample(at(2026, 11, 6, 16), {"e_total": 20.0}),
    ])
    assert db.daily_series(conn, 3, today=date(2026, 11, 6)) == [
        {"day": "2026-11-04", "pv": 2.0},
        {"day": "2026-11-05"},
        {"day": "2026-11-06", "pv": 8.0},
    ]


def test_daily_series_counts_from_a_day_before_its_window(conn):
    db.save_samples(conn, [
        db.Sample(at(2026, 11, 4, 16), {"e_total": 10.0}),
        db.Sample(at(2026, 11, 6, 16), {"e_total": 13.0}),
    ])
    assert db.daily_series(conn, 1, today=date(2026, 11, 6)) == [{"day": "2026-11-06", "pv": 3.0}]


def test_battery_samples_join_saved_and_unsaved_readings_in_order(conn):
    db.save_samples(conn, [db.Sample(100, {"pbattery1": 1, "battery_soc": 50}), db.Sample(200, {"pbattery1": 2})])
    pending = [db.Sample(50, {"pbattery1": 9}), db.Sample(300, {"pbattery1": "n/a", "battery_soc": 49})]
    assert db.battery_samples(conn, 150, pending) == [(200, 2, None), (300, None, 49)]


def test_settings_start_unset(conn):
    assert db.get_settings(conn) == dict.fromkeys(db.SETTINGS)


def test_saving_settings_changes_those_given_and_none_clears_one(conn):
    db.save_settings(conn, {"limit_amps": 16, "target_soc": 90, "tariff_day": 5})
    db.save_settings(conn, {"limit_amps": None, "target_soc": 95})
    assert db.get_settings(conn) == {**dict.fromkeys(db.SETTINGS), "target_soc": 95, "tariff_day": 5}


def appliance(**fields):
    return {"name": "Lamp", "typical_w": 10, "peak_w": None, "surge_w": None, "use_in_outage": False, "notes": "", **fields}


def test_appliance_round_trip_keeps_blanks_and_the_tick(conn):
    new_id = db.add_appliance(conn, appliance(peak_w=20, use_in_outage=True, notes="Hall"))
    assert db.list_appliances(conn) == [{"id": new_id, **appliance(peak_w=20, use_in_outage=True, notes="Hall")}]


def test_appliances_list_in_the_order_they_were_added(conn):
    for name in ("Zebra", "Apple"):
        db.add_appliance(conn, appliance(name=name))
    assert [a["name"] for a in db.list_appliances(conn)] == ["Zebra", "Apple"]


def test_updating_an_appliance_changes_only_that_one(conn):
    first = db.add_appliance(conn, appliance(name="One"))
    second = db.add_appliance(conn, appliance(name="Two"))
    assert db.update_appliance(conn, first, appliance(name="Changed", typical_w=99))
    assert [(a["id"], a["name"], a["typical_w"]) for a in db.list_appliances(conn)] == [(first, "Changed", 99), (second, "Two", 10)]


def test_deleting_an_appliance_removes_it(conn):
    new_id = db.add_appliance(conn, appliance())
    assert db.delete_appliance(conn, new_id)
    assert db.list_appliances(conn) == []


def test_changing_an_appliance_that_is_gone_reports_it(conn):
    assert not db.update_appliance(conn, 404, appliance())
    assert not db.delete_appliance(conn, 404)


def test_new_tables_are_added_to_an_existing_database(tmp_path):
    path = tmp_path / "old.db"
    old = db.connect(path)
    old.executescript("DROP TABLE appliances; DROP TABLE settings;")
    old.close()
    reopened = db.connect(path)
    assert db.list_appliances(reopened) == []
    assert db.get_settings(reopened) == dict.fromkeys(db.SETTINGS)
    reopened.close()


def test_overhead_samples_are_the_four_powers_from_the_start_on(conn):
    values = {"ppv": 1, "load_ptotal": 2, "pbattery1": 3, "active_power_total": 4}
    db.save_samples(conn, [db.Sample(1000, values), db.Sample(2000, {**values, "ppv": 5})])
    assert db.overhead_samples(conn, 1500) == [(5, 2, 3, 4)]
