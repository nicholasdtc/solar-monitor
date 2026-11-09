"""The expected solar output, learned from made-up logs and weather."""

import asyncio
import time

import httpx
import pytest

from solar_monitor import db, solar_forecast, weather
from solar_monitor.solar_forecast import DAY, HOUR, Fit, Pair, fit
from solar_monitor.weather import service
from solar_monitor.weather.base import Hour

NOW = 1_750_000_000  # June 2025, away from a change of the clocks.
PANELS = "30/180"


def true_factor(hour_of_day):
    """Made-up panels that make more of their sunshine at noon UTC than at either end of the day."""
    return 3.0 - 0.1 * abs(hour_of_day - 12)


def sun_for(day, hour_of_day):
    return 200 + 50 * ((day * 7 + hour_of_day * 3) % 9)  # 200 to 600 W/m², varied


def made_up_pairs(days, hours=range(8, 17)):
    start = NOW // DAY * DAY - days * DAY
    return [
        Pair(start + d * DAY + h * HOUR, sun_for(d, h), true_factor(h) * sun_for(d, h)) for d in range(days) for h in hours
    ]


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    yield connection
    connection.close()


def test_the_fit_learns_each_hours_factor():
    found = fit(made_up_pairs(60), None, "gti", PANELS, None, NOW)
    for hour in range(8, 17):
        assert found.factors[hour] == pytest.approx(true_factor(hour), rel=0.01)
    assert found.status == "ready" and found.days == 60 and found.hours == 60 * 9
    assert found.expected_w(NOW // DAY * DAY + 12 * HOUR, 400) == pytest.approx(3.0 * 400, rel=0.01)
    assert found.expected_w(NOW // DAY * DAY + 3 * HOUR, 100) == pytest.approx(found.factor * 100)  # an hour not seen
    assert found.expected_w(NOW, None) is None


def test_output_never_goes_over_the_ceiling():
    found = fit(made_up_pairs(20), None, "gti", PANELS, 1000.0, NOW)
    assert found.expected_w(NOW // DAY * DAY + 12 * HOUR, 900) == 1000.0


def test_hours_held_down_by_the_ceiling_or_too_dim_are_left_out():
    pairs = made_up_pairs(20)
    held = [Pair(p.hour_start + DAY * 100, 900.0, 1990.0) for p in pairs]  # 1990 W of 2000: clipped
    dim = [Pair(p.hour_start + DAY * 200, 30.0, 0.0) for p in pairs]
    alone = fit(pairs, None, "gti", PANELS, 2000.0, NOW)
    assert fit(pairs + held + dim, None, "gti", PANELS, 2000.0, NOW).factors == alone.factors


def test_the_rating_gives_a_guess_until_there_is_data():
    guess = fit([], 4.0, "gti", PANELS, 4000.0, NOW)
    assert guess.factor == pytest.approx(4.0 * solar_forecast.START_RATIO)
    assert guess.ratio == pytest.approx(solar_forecast.START_RATIO)
    assert guess.status == "learning" and guess.factors == {}
    assert fit([], None, "gti", PANELS, None, NOW) is None
    # The data outweighs the guess as it comes.
    learned = fit(made_up_pairs(60), 4.0, "gti", PANELS, 4000.0, NOW)
    assert learned.factors[12] == pytest.approx(true_factor(12), rel=0.02)
    assert learned.ratio == pytest.approx(learned.factor / 4.0)
    assert fit(made_up_pairs(60), 4.0, "ghi", None, 4000.0, NOW).ratio is None  # flat-ground sunshine isn't the panels'


def test_learning_until_enough_days():
    assert fit(made_up_pairs(solar_forecast.READY_DAYS - 1), None, "gti", PANELS, None, NOW).status == "learning"
    assert fit(made_up_pairs(solar_forecast.READY_DAYS), None, "gti", PANELS, None, NOW).status == "ready"
    # Days with too few usable hours don't count.
    assert fit(made_up_pairs(30, hours=range(11, 13)), None, "gti", PANELS, None, NOW).days == 0


def test_a_fit_survives_the_database():
    found = fit(made_up_pairs(20), 4.0, "gti", PANELS, 3000.0, NOW)
    assert Fit.from_json(found.to_json()) == found


# From the database.


def placed(conn, kwp=4.0, tilt=30):
    db.save_settings(
        conn, {"latitude": 10.0, "longitude": 20.0, "panel_tilt_deg": tilt, "panel_azimuth_deg": 180, "pv_capacity_kwp": kwp}
    )


def log_hour(conn, hour_start, watts, every=300, span=HOUR, counter=None):
    """Readings through the hour at a steady output, with the lifetime counter running on from `counter`."""
    samples = []
    for i, ts in enumerate(range(hour_start, hour_start + span, every)):
        values = {"ppv": watts}
        if counter is not None:
            values["e_total"] = counter + watts * i * every / 3_600_000
        samples.append(db.Sample(ts, values))
    db.save_samples(conn, samples)


def log_days(conn, first_day, days, panels=PANELS, gti=True, hours=range(8, 17)):
    """Days of made-up weather and output, each hour making true_factor × its sunshine."""
    counter = 100.0
    weather_hours = []
    for d in range(days):
        for h in hours:
            start = first_day + d * DAY + h * HOUR
            sun = sun_for(d, h)
            log_hour(conn, start, true_factor(h) * sun, counter=counter)
            counter += true_factor(h) * sun / 1000
            weather_hours.append(Hour(hour_start=start, gti_wm2=sun if gti else None, ghi_wm2=sun if not gti else None))
    db.save_past(conn, first_day + days * DAY, "stub", weather_hours, panels)


def test_refit_learns_from_the_log(conn):
    placed(conn)
    log_days(conn, NOW // DAY * DAY - 20 * DAY, 20)
    found = solar_forecast.refit(conn, NOW, PANELS)
    assert found.basis == "gti" and found.days == 20 and found.kwp == 4.0
    assert found.factors[12] == pytest.approx(true_factor(12), rel=0.02)
    assert found.ceiling_w == pytest.approx(true_factor(12) * 600)  # the most made, under the rating
    assert solar_forecast.load_fit(conn) == found


def test_refit_leaves_out_hours_with_few_readings_and_other_panels(conn):
    placed(conn, kwp=None)
    day = NOW // DAY * DAY - 5 * DAY
    log_days(conn, day, 3)
    log_hour(conn, day + 4 * DAY + 12 * HOUR, 9999, span=HOUR // 2)  # only half an hour logged
    db.save_past(conn, NOW, "stub", [Hour(hour_start=day + 4 * DAY + 12 * HOUR, gti_wm2=500)], PANELS)
    log_hour(conn, day + 3 * DAY + 12 * HOUR, 50)
    db.save_past(conn, NOW, "stub", [Hour(hour_start=day + 3 * DAY + 12 * HOUR, gti_wm2=500)], "45/90")  # other panels
    found = solar_forecast.refit(conn, NOW, PANELS)
    assert found.hours == 3 * 9


def test_flat_ground_sunshine_without_a_tilt(conn):
    db.save_settings(conn, {"latitude": 10.0, "longitude": 20.0, "pv_capacity_kwp": 4.0})
    log_days(conn, NOW // DAY * DAY - 20 * DAY, 20, panels=None, gti=False)
    found = solar_forecast.refit(conn, NOW, None)
    assert found.basis == "ghi" and found.ratio is None
    assert found.factors[12] == pytest.approx(true_factor(12), rel=0.02)
    assert found.sun_of({"gti_wm2": 1.0, "ghi_wm2": 2.0}) == 2.0


def test_refit_forgets_a_fit_with_nothing_to_go_on(conn):
    placed(conn)
    assert solar_forecast.refit(conn, NOW, PANELS) is not None
    db.save_settings(conn, {"pv_capacity_kwp": None})
    assert solar_forecast.refit(conn, NOW, PANELS) is None
    assert solar_forecast.load_fit(conn) is None


def forecast_hours(first, count, gti=500.0):
    return [Hour(hour_start=first + i * HOUR, gti_wm2=gti) for i in range(count)]


def test_the_last_forecast_before_midnight_is_kept_for_tomorrow(conn):
    guess = fit([], 4.0, "gti", PANELS, None, NOW)
    today = solar_forecast.day_start(NOW)
    tomorrow = solar_forecast.next_day_start(today)
    after = solar_forecast.next_day_start(tomorrow)
    hours = forecast_hours(today, 72)
    solar_forecast.record_day_ahead(conn, 1, today + 6 * HOUR, hours, guess)
    expected = solar_forecast.day_ahead(conn, 0, 2**40)
    assert sorted(expected) == list(range(tomorrow, after, HOUR))  # neither today nor the day after
    assert list(expected.values()) == [pytest.approx(500 * 4.0 * solar_forecast.START_RATIO)] * len(expected)

    solar_forecast.record_day_ahead(conn, 2, tomorrow - HOUR, forecast_hours(today, 72, gti=100.0), guess)
    assert list(solar_forecast.day_ahead(conn, 0, 2**40).values()) == [pytest.approx(100 * 4.0 * solar_forecast.START_RATIO)] * len(expected)

    # After midnight a forecast is for the next day, and leaves the one just begun as it was.
    solar_forecast.record_day_ahead(conn, 3, tomorrow + HOUR, forecast_hours(today, 72, gti=300.0), guess)
    kept = solar_forecast.day_ahead(conn, 0, 2**40)
    assert {round(kept[h]) for h in range(tomorrow, after, HOUR)} == {round(100 * 4.0 * solar_forecast.START_RATIO)}
    assert {round(kept[h]) for h in range(after, solar_forecast.next_day_start(after), HOUR)} == {round(300 * 4.0 * solar_forecast.START_RATIO)}
    solar_forecast.record_day_ahead(conn, 4, NOW, hours, None)  # with no fit, nothing


def test_scored_against_what_was_made(conn):
    today = solar_forecast.day_start(NOW)
    yesterday = solar_forecast.day_start(today - HOUR)
    before = solar_forecast.day_start(yesterday - HOUR)
    placed(conn)
    log_days(conn, before, 2)
    # Expected a fifth more than was made each hour of yesterday; the day before had no forecast.
    made = solar_forecast.hourly_made(conn, yesterday, today)
    rows = [(h, 1, 1.2 * made.get(h, 0.0)) for h in range(yesterday, today, HOUR)]
    conn.executemany("INSERT INTO solar_expected VALUES (?, ?, ?)", rows)
    scores = solar_forecast.score(conn, NOW)
    assert [d["day"] for d in scores["days"]] == [solar_forecast.local_date(yesterday)]
    day = scores["days"][0]
    assert day["expected_kwh"] == pytest.approx(1.2 * day["made_kwh"], rel=0.01)
    assert scores["day_error_pct"] == pytest.approx(20, abs=0.5)
    assert scores["bias_pct"] == pytest.approx(20, abs=0.5)
    assert scores["hour_error_pct"] == pytest.approx(20, abs=0.5)

    # A day missing an hour of what was expected isn't scored.
    conn.execute("DELETE FROM solar_expected WHERE hour_start = ?", (yesterday + 3 * HOUR,))
    assert solar_forecast.score(conn, NOW)["days"] == []


def test_the_summary_for_the_page(conn):
    assert solar_forecast.summary(conn, NOW, []) == {"status": "none"}
    placed(conn)
    solar_forecast.refit(conn, NOW, PANELS)
    hours = [{"hour_start": NOW // HOUR * HOUR, "gti_wm2": 500.0, "ghi_wm2": None}, {"hour_start": NOW // HOUR * HOUR + HOUR, "gti_wm2": None}]
    found = solar_forecast.summary(conn, NOW, hours)
    assert found["status"] == "learning" and found["basis"] == "gti" and found["ready_days"] == solar_forecast.READY_DAYS
    assert hours[0]["expected_w"] == pytest.approx(500 * 4.0 * solar_forecast.START_RATIO) and hours[1]["expected_w"] is None
    assert found["yesterday"] is None and found["score"]["days"] == 0 and found["score"]["day_error_pct"] is None


# The fetch loop and the page's route.


class StubForecast:
    name = "stub-solar"
    title = "Stub"
    credit = weather.base.Credit("Stub", "none", "https://example.test")
    has_irradiance = True
    has_past = False
    min_interval = HOUR

    def forecast(self, where, fetch):
        start = solar_forecast.day_start(NOW)
        return forecast_hours(start, 72)


def test_the_loop_fits_and_keeps_tomorrows_expected_output(conn, monkeypatch):
    monkeypatch.setitem(weather.FORECASTS, "stub-solar", StubForecast())
    placed(conn)
    db.save_settings(conn, {"forecast_provider": "stub-solar"})
    state = service.State(minute=0)
    asyncio.run(service.run_once(conn, NOW, None, state))
    assert solar_forecast.load_fit(conn).kwp == 4.0
    tomorrow = solar_forecast.next_day_start(NOW)
    assert sorted(solar_forecast.day_ahead(conn, 0, 2**40)) == list(range(tomorrow, solar_forecast.next_day_start(tomorrow), HOUR))
    assert state.solar.due == service.next_night(NOW, 0)

    # A change of rating refits at once.
    db.save_settings(conn, {"pv_capacity_kwp": 6.0})
    asyncio.run(service.run_once(conn, NOW + 60, None, state))
    assert solar_forecast.load_fit(conn).kwp == 6.0


def test_the_route_has_the_solar_forecast(conn, monkeypatch):
    from solar_monitor.api import create_app
    from solar_monitor.collector import Collector

    app = create_app(Collector("test", 0, conn))
    monkeypatch.setattr(time, "time", lambda: NOW)

    def get(path):
        async def send():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                return await client.get(path)

        return asyncio.run(send())

    assert get("/api/weather").json()["solar"] == {"status": "none"}
    placed(conn)
    solar_forecast.refit(conn, NOW, PANELS)
    db.save_forecast(conn, NOW, "stub", forecast_hours(NOW // HOUR * HOUR, 3), PANELS)
    found = get("/api/weather").json()
    assert found["solar"]["status"] == "learning"
    assert [h["expected_w"] for h in found["forecast"]["hours"]] == [pytest.approx(500 * 4.0 * solar_forecast.START_RATIO)] * 3
