from datetime import datetime

import pytest

from solar_monitor import planning


def estimate(soc=50, power_w=1000, capacity_kwh=10, min_soc=20, target_soc=90):
    return planning.battery_estimate(soc, power_w, capacity_kwh, min_soc, target_soc)


def test_discharge_time_uses_the_energy_above_the_minimum():
    result = estimate(soc=50, power_w=1000, capacity_kwh=10, min_soc=20)
    assert result["state"] == "discharging"
    assert result["limit_soc"] == 20
    assert result["hours"] == pytest.approx(3)


def test_charge_time_uses_the_energy_below_the_target():
    result = estimate(soc=50, power_w=-2000, capacity_kwh=10, target_soc=90)
    assert result["state"] == "charging"
    assert result["limit_soc"] == 90
    assert result["hours"] == pytest.approx(2)


def test_low_power_counts_as_idle_with_no_time():
    assert estimate(power_w=49)["state"] == "idle"
    assert estimate(power_w=49)["hours"] is None
    assert estimate(power_w=-49)["state"] == "idle"


@pytest.mark.parametrize("soc", [20, 15])
def test_already_at_or_below_the_minimum_is_zero_hours(soc):
    assert estimate(soc=soc, power_w=500, min_soc=20)["hours"] == 0


@pytest.mark.parametrize("soc", [90, 95])
def test_already_at_or_above_the_target_is_zero_hours(soc):
    assert estimate(soc=soc, power_w=-500, target_soc=90)["hours"] == 0


def test_unknown_capacity_still_reports_the_direction_and_limit():
    result = estimate(capacity_kwh=None)
    assert result["state"] == "discharging"
    assert result["limit_soc"] == 20
    assert result["hours"] is None


def test_at_the_limit_needs_no_capacity():
    assert estimate(soc=20, capacity_kwh=None, min_soc=20)["hours"] == 0


@pytest.mark.parametrize("soc, power_w", [(None, 500), (50, None)])
def test_missing_reading_gives_no_estimate(soc, power_w):
    assert estimate(soc=soc, power_w=power_w) is None


def test_smoothing_averages_a_short_spike_away():
    assert planning.smoothed_power([500, 500, 3000, 500]) == pytest.approx(1125)


def test_smoothing_ignores_readings_from_before_a_reversal():
    assert planning.smoothed_power([800, 800, -300, -500]) == pytest.approx(-400)


def test_smoothing_keeps_the_direction_through_idle_readings():
    assert planning.smoothed_power([600, 20, 600]) == pytest.approx(1220 / 3)


def test_smoothing_stops_counting_once_the_battery_has_stopped():
    assert planning.smoothed_power([900, 900, 900, 10]) == 10


def test_smoothing_skips_missing_readings_and_handles_having_none():
    assert planning.smoothed_power([None, 400, None]) == pytest.approx(400)
    assert planning.smoothed_power([None]) is None
    assert planning.smoothed_power([]) is None


def stretch(start_ts, power_w, socs, step=60):
    return [(start_ts + i * step, power_w, soc) for i, soc in enumerate(socs)]


# At 1000 W and one percentage point every 6 minutes, each point is 100 Wh: a 10 kWh battery.
def steady_discharge(points, start_ts=0, start_soc=80):
    # One step more than `points`: only the span from the first change to the last one counts.
    socs = [start_soc - i // 6 for i in range((points + 1) * 6 + 1)]
    return stretch(start_ts, 1000, socs)


def test_capacity_is_measured_from_energy_per_percentage_point():
    assert planning.measure_capacity(steady_discharge(12)) == (10.0, 12)


def test_capacity_needs_enough_points_before_it_counts():
    assert planning.measure_capacity(steady_discharge(planning.MIN_MEASURED_POINTS - 1)) is None


def test_capacity_ignores_a_stretch_too_short_to_rise_above_rounding():
    short = stretch(0, 1000, [80, 80, 79, 79, 78, 78])
    assert planning.measure_capacity(short + steady_discharge(12, start_ts=100_000)) == (10.0, 12)


def test_capacity_stretch_ends_at_a_gap_in_logging():
    # A power cut between the two runs must not be counted as time spent discharging.
    first = steady_discharge(6)
    second = steady_discharge(6, start_ts=first[-1][0] + 86400, start_soc=first[-1][2] - 1)
    assert planning.measure_capacity(first + second) == (10.0, 12)


def test_capacity_stretch_ends_when_the_battery_reverses():
    down = steady_discharge(6)
    up = stretch(down[-1][0] + 60, -1000, [soc for _, _, soc in reversed(down)])
    assert planning.measure_capacity(down + up) == (10.0, 12)


def test_capacity_counts_idle_readings_inside_a_stretch():
    samples = steady_discharge(12)
    samples[10] = (samples[10][0], 0, samples[10][2])  # 1 minute at 0 W rather than 1000 W.
    kwh, _ = planning.measure_capacity(samples)
    assert kwh == pytest.approx(9.9)


def test_capacity_skips_missing_readings():
    samples = steady_discharge(12)
    samples.insert(5, (samples[5][0] - 1, None, None))
    assert planning.measure_capacity(samples) == (10.0, 12)


def test_outage_hours_allows_for_conversion_and_standby():
    # 10 kWh above the 0% reserve, drawn at 930 / 0.93 + 40 = 1040 W.
    assert planning.outage_hours(930, 100, 0, 10) == pytest.approx(10_000 / 1040)


def test_outage_hours_with_nothing_above_the_reserve_is_zero():
    assert planning.outage_hours(500, 15, 20, 10) == 0


def test_outage_hours_needs_a_capacity():
    assert planning.outage_hours(500, 100, 20, None) is None


def test_max_load_allows_for_standby_and_conversion():
    # 5 kWh over 5 hours is 1000 W of battery draw, less 40 W standby, times 0.93.
    assert planning.max_load_w(5000, 5) == pytest.approx(892.8)


def test_max_load_is_the_inverse_of_outage_hours():
    load_w = planning.max_load_w(planning.available_wh(10, 90, 30), 7)
    assert planning.outage_hours(load_w, 90, 30, 10) == pytest.approx(7)


def test_max_load_is_zero_when_standby_uses_it_all():
    assert planning.max_load_w(100, 5) == 0


def plan_outage(**overrides):
    options = dict(hours=5, selected_w=500, soc=60, reserve_soc=10, capacity_kwh=10, limit_w=None)
    return planning.outage_plan(**{**options, **overrides})


def test_outage_plan_starts_from_now_and_from_full_down_to_the_reserve():
    plan = plan_outage()
    # 5 kWh above the reserve from 60%, and 9 kWh from full.
    assert plan["from_now"]["max_w"] == pytest.approx(892.8)
    assert plan["from_full"]["max_w"] == pytest.approx(planning.max_load_w(9000, 5))
    assert plan["from_now"]["capped"] is False


def test_outage_plan_compares_with_the_selected_load():
    assert plan_outage(selected_w=500)["from_now"]["spare_w"] == pytest.approx(392.8)
    assert plan_outage(selected_w=1000)["from_now"]["spare_w"] == pytest.approx(-107.2)


def test_outage_plan_is_held_to_the_limit():
    start = plan_outage(limit_w=600)["from_now"]
    assert start["max_w"] == 600
    assert start["capped"] is True
    assert start["spare_w"] == 100


def test_outage_plan_needs_a_capacity_and_a_charge_level():
    assert plan_outage(capacity_kwh=None)["from_full"] is None
    no_soc = plan_outage(soc=None)
    assert no_soc["from_now"] is None
    assert no_soc["from_full"] is not None


@pytest.mark.parametrize("now, minutes, expected", [
    (datetime(2030, 1, 15, 3, 20, 10), 4 * 60 + 30, datetime(2030, 1, 15, 4, 30)),  # Later today.
    (datetime(2030, 1, 15, 21, 0), 4 * 60 + 30, datetime(2030, 1, 16, 4, 30)),  # Already gone.
    (datetime(2030, 1, 15, 4, 30), 4 * 60 + 30, datetime(2030, 1, 16, 4, 30)),  # Just arrived.
    (datetime(2030, 1, 31, 23, 59), 0, datetime(2030, 2, 1, 0, 0)),  # Over a month's end.
])
def test_next_time_comes_after_now(now, minutes, expected):
    assert planning.next_time(now, minutes) == expected


def plan_run(**overrides):
    options = dict(soc=60, min_soc=10, capacity_kwh=10, limit_w=None, selected_w=500, battery_w=300)
    return planning.run_until_plan(datetime(2030, 1, 15, 3, 0), 8 * 60, **{**options, **overrides})


def test_run_until_shares_the_energy_above_the_minimum_over_the_time_left():
    plan = plan_run()
    assert plan["until"] == "2030-01-15T08:00"
    assert plan["hours"] == pytest.approx(5)
    assert plan["energy_kwh"] == pytest.approx(5)
    assert plan["max_battery_w"] == pytest.approx(1000)
    assert plan["max_w"] == pytest.approx(892.8)
    assert plan["spare_w"] == pytest.approx(392.8)
    assert plan["battery_w"] == 300


def test_run_until_is_held_to_the_limit_but_the_battery_figure_is_not():
    plan = plan_run(limit_w=600)
    assert plan["max_w"] == 600
    assert plan["capped"] is True
    assert plan["max_battery_w"] == pytest.approx(1000)


def test_run_until_below_the_minimum_has_nothing_to_give():
    plan = plan_run(soc=5)
    assert plan["energy_kwh"] == 0
    assert plan["max_battery_w"] == 0
    assert plan["max_w"] == 0
    assert plan["spare_w"] == -500


@pytest.mark.parametrize("missing", ["soc", "capacity_kwh"])
def test_run_until_still_reports_the_time_when_a_figure_is_missing(missing):
    plan = plan_run(**{missing: None})
    assert plan["hours"] == pytest.approx(5)
    assert plan["max_w"] is None
    assert plan["spare_w"] is None
    assert plan["max_battery_w"] is None


def test_tariffs_cost_night_energy_after_round_trip_losses():
    values = planning.tariff_values(day=20, peak=30, night=9, export=8)
    assert values["night_kwh_cost"] == pytest.approx(10)
    assert values["saving_day"] == pytest.approx(10)
    assert values["saving_peak"] == pytest.approx(20)
    assert values["gain_export"] == pytest.approx(-2)
    assert values["solar_export"] == 8


def test_tariffs_price_a_leftover_kwh_on_the_way_out_only():
    values = planning.tariff_values(day=None, peak=None, night=5, export=10)
    assert values["leftover_export"] == pytest.approx(10 * planning.CONVERSION_EFFICIENCY - 5)


def test_tariffs_leave_out_what_needs_a_missing_rate():
    values = planning.tariff_values(day=20, peak=None, night=None, export=8)
    assert values["night_kwh_cost"] is None
    assert values["saving_day"] is None
    assert values["saving_peak"] is None
    assert values["gain_export"] is None
    assert values["leftover_export"] is None
    assert values["solar_export"] == 8


def appliance(name="Lamp", typical_w=100, peak_w=None, surge_w=None, use_in_outage=False):
    return {"id": 1, "name": name, "typical_w": typical_w, "peak_w": peak_w, "surge_w": surge_w,
            "use_in_outage": use_in_outage, "notes": ""}


def summary(appliances, **overrides):
    options = dict(volts=200, limit_amps=10, surge_limit_w=None, live_load_w=None, soc=None,
                   reserve_soc=20, capacity_kwh=None)
    return planning.summarize(appliances, **{**options, **overrides})


def test_worst_case_uses_the_peak_or_else_the_typical_draw():
    assert planning.worst_w(appliance(typical_w=100, peak_w=300)) == 300
    assert planning.worst_w(appliance(typical_w=100)) == 100


def test_row_shows_its_current_and_share_of_the_limit():
    row = summary([appliance(typical_w=100, peak_w=400)])["appliances"][0]
    assert row["amps"] == 2
    assert row["share"] == 0.2


def test_can_add_compares_with_what_is_left_after_the_selected_loads():
    rows = summary([
        appliance("Running", typical_w=1000, peak_w=1200, use_in_outage=True),
        appliance("Fits", typical_w=800),
        appliance("Too big", typical_w=801),
    ])["appliances"]
    assert [r["can_add"] for r in rows] == ["in_use", "yes", "no"]


def test_can_add_is_blank_until_a_limit_is_set():
    result = summary([appliance()], limit_amps=None)
    assert result["appliances"][0]["can_add"] is None
    assert result["appliances"][0]["share"] is None
    assert result["cases"]["all_worst"]["headroom_amps"] is None


def test_cases_add_up_the_selected_and_all_appliances():
    cases = summary([
        appliance(typical_w=100, peak_w=500, use_in_outage=True),
        appliance(typical_w=200),
    ])["cases"]
    assert cases["selected_typical"]["watts"] == 100
    assert cases["selected_worst"]["watts"] == 500
    assert cases["all_typical"]["watts"] == 300
    assert cases["all_worst"]["watts"] == 700
    assert cases["all_worst"]["amps"] == 3.5
    assert cases["all_worst"]["headroom_amps"] == 6.5


def test_live_load_is_a_case_of_its_own_when_known():
    assert summary([], live_load_w=400)["cases"]["now"]["amps"] == 2
    assert summary([])["cases"]["now"] is None


def test_runtime_from_now_uses_the_live_charge_and_the_inverter_reserve():
    case = summary([appliance(typical_w=930, use_in_outage=True)], soc=60, reserve_soc=20, capacity_kwh=10)["cases"]["selected_typical"]
    assert case["hours_from_now"] == pytest.approx(4000 / 1040)
    assert case["hours_from_full"] == pytest.approx(8000 / 1040)


def test_surge_adds_only_the_excess_above_what_is_already_counted():
    result = summary([
        appliance(typical_w=100, surge_w=800, use_in_outage=True),
        appliance(typical_w=100, peak_w=500, surge_w=300),  # Surge under its peak adds nothing.
        appliance(typical_w=200),
    ], surge_limit_w=2000)["surge"]
    assert result["selected"] == {"steady_w": 100, "extra_w": 700, "peak_w": 800, "share": 0.4}
    assert result["all"]["peak_w"] == 800 + 500 + 200


def test_no_appliances_sum_to_zero():
    cases = summary([])["cases"]
    assert cases["all_worst"]["watts"] == 0
    assert cases["all_worst"]["headroom_amps"] == 10
