from datetime import UTC, datetime

from solar_monitor.weather import sun

DAY = 86400


def at(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp())


def test_the_equator_has_about_twelve_hours_of_day():
    start = at("2026-03-20T00:00")
    found = sun.times(0.0, 0.0, start, start + DAY)
    (rise,), (set_,) = found["rises"], found["sets"]
    assert abs(rise - at("2026-03-20T06:04")) < 5 * 60
    assert 12 * 3600 < set_ - rise < 12 * 3600 + 15 * 60


def test_rise_comes_before_set_each_day_at_ten_twenty():
    start = at("2026-06-01T00:00")
    found = sun.times(10.0, 20.0, start, start + 7 * DAY)
    assert len(found["rises"]) == len(found["sets"]) == 7
    for rise, set_ in zip(found["rises"], found["sets"], strict=True):
        assert rise < set_ < rise + DAY
        # Local noon at 20°E is about 10:40 UTC; days at 10°N in June are about 12h40m.
        assert abs((rise + set_) / 2 % DAY - (10 * 3600 + 40 * 60)) < 10 * 60
        assert 12 * 3600 + 20 * 60 < set_ - rise < 13 * 3600


def test_far_east_and_west_still_give_one_of_each_per_day():
    start = at("2026-01-10T00:00")
    for lon in (-170.0, 170.0):
        found = sun.times(-30.0, lon, start, start + 5 * DAY)
        assert 4 <= len(found["rises"]) <= 5 and 4 <= len(found["sets"]) <= 5
        assert all(b - a > 23 * 3600 for a, b in zip(found["rises"], found["rises"][1:]))


def test_no_rise_or_set_in_the_polar_night_or_midnight_sun():
    start = at("2026-12-15T00:00")
    assert sun.times(80.0, 20.0, start, start + 3 * DAY) == {"rises": [], "sets": []}
    start = at("2026-06-15T00:00")
    assert sun.times(80.0, 20.0, start, start + 3 * DAY) == {"rises": [], "sets": []}
