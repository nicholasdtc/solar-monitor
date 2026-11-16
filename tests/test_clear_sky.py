import random
from datetime import UTC, datetime

from solar_monitor import clear_sky, db
from solar_monitor.weather.base import Location

DAY = 86400
# An invented place with panels facing two ways.
TWO_WAYS = Location(45.0, 10.0, panel_tilt=30, panel_facing=100, panel_facing_2=260)


def at(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp())


START = at("2026-06-01T00:00")


def samples(where, factors, days=10, seed=1):
    """A sample a minute: each string makes its factor times the clear sunshine on its facing, less
    the day's cloud, with a clear spell most days."""
    rng = random.Random(seed)
    rows = []
    for d in range(days):
        cloud = [rng.uniform(0.2, 1.0) for _ in range(24 * 6)]
        for spell in rng.sample(range(24 * 6), 30):
            cloud[spell] = 1.0
        for minute in range(0, DAY, 60):
            ts = START + d * DAY + minute
            s = clear_sky.sky(where.latitude, where.longitude, ts)
            values = {}
            for reading, facing, factor in factors:
                values[reading] = factor * clear_sky.on_panels(s, where.panel_tilt, facing) * cloud[minute // 600]
            values["ppv"] = sum(values.values())
            if values["ppv"] > 0:
                rows.append((ts, values))
    return rows


def test_the_clear_sky_is_brightest_at_noon_and_dark_at_night():
    noon = clear_sky.sky(45.0, 10.0, at("2026-06-21T11:20"))
    assert 850 < noon.ghi < 1000 and noon.dni > 700
    assert clear_sky.sky(45.0, 10.0, at("2026-06-21T23:00")).ghi == 0


def test_panels_facing_the_morning_sun_get_more_of_it_than_ones_facing_away():
    morning = clear_sky.sky(45.0, 10.0, at("2026-06-21T07:00"))
    east = clear_sky.on_panels(morning, 30, 100)
    west = clear_sky.on_panels(morning, 30, 260)
    assert east > 2 * west > 0
    assert clear_sky.on_panels(morning, None, None) == morning.ghi


def test_each_string_is_matched_to_its_facing_and_learns_its_best():
    # ppv1 faces west and ppv2 east, the other way round from their order in the settings.
    rows = samples(TWO_WAYS, [("ppv1", 260, 5.0), ("ppv2", 100, 4.0)])
    calibration = clear_sky.fit(rows, TWO_WAYS, START + 10 * DAY)
    assert calibration is not None
    terms = {term.readings: term for term in calibration.terms}
    assert terms[("ppv1",)].facings == (260,) and abs(terms[("ppv1",)].factor - 5.0) < 0.1
    assert terms[("ppv2",)].facings == (100,) and abs(terms[("ppv2",)].factor - 4.0) < 0.1
    assert calibration.days == 10


def test_one_facing_compares_the_total():
    where = Location(45.0, 10.0, panel_tilt=30, panel_facing=180)
    rows = samples(where, [("ppv1", 180, 3.0), ("ppv2", 180, 3.0)])
    calibration = clear_sky.fit(rows, where, START + 10 * DAY)
    assert calibration is not None
    (term,) = calibration.terms
    assert term.readings == ("ppv1", "ppv2") and abs(term.factor - 6.0) < 0.1


def test_without_strings_the_total_is_compared_with_the_mean_of_the_facings():
    rows = [(ts, {"ppv": values["ppv"]}) for ts, values in samples(TWO_WAYS, [("ppv1", 260, 5.0), ("ppv2", 100, 4.0)])]
    calibration = clear_sky.fit(rows, TWO_WAYS, START + 10 * DAY)
    assert calibration is not None
    (term,) = calibration.terms
    assert term.readings == ("ppv",) and term.facings == (100, 260)


def test_too_little_to_go_on_gives_no_fit():
    rows = samples(TWO_WAYS, [("ppv1", 260, 5.0), ("ppv2", 100, 4.0)], days=1)[:60]
    assert clear_sky.fit(rows, TWO_WAYS, START + DAY) is None
    assert clear_sky.fit([], TWO_WAYS, START) is None


def test_now_gives_the_percentage_of_clear_sky():
    rows = samples(TWO_WAYS, [("ppv1", 260, 5.0), ("ppv2", 100, 4.0)])
    calibration = clear_sky.fit(rows, TWO_WAYS, START + 10 * DAY)
    noon = at("2026-06-11T11:20")
    s = clear_sky.sky(45.0, 10.0, noon)
    full = {"ppv1": 5.0 * clear_sky.on_panels(s, 30, 260), "ppv2": 4.0 * clear_sky.on_panels(s, 30, 100)}
    result = clear_sky.now(calibration, noon, full)
    assert 97 <= result["pct"] <= 101
    half = clear_sky.now(calibration, noon, {k: v / 2 for k, v in full.items()})
    assert 48 <= half["pct"] <= 51 and half["clear_w"] == result["clear_w"]
    assert [part["readings"] for part in result["parts"]] == [["ppv2"], ["ppv1"]]
    assert all(97 <= part["pct"] <= 101 for part in result["parts"])
    assert clear_sky.now(calibration, at("2026-06-11T23:00"), full) is None
    assert clear_sky.now(calibration, noon, {"ppv1": 100}) is None  # Missing a string.
    assert clear_sky.now(None, noon, full) is None


def test_a_facing_in_too_little_sunshine_has_no_share_of_its_own():
    rows = samples(TWO_WAYS, [("ppv1", 260, 5.0), ("ppv2", 100, 4.0)])
    calibration = clear_sky.fit(rows, TWO_WAYS, START + 10 * DAY)
    morning = next(
        ts for ts in range(at("2026-06-11T03:00"), at("2026-06-11T09:00"), 600)
        if clear_sky.on_panels(clear_sky.sky(45.0, 10.0, ts), 30, 100) >= 300
    )
    s = clear_sky.sky(45.0, 10.0, morning)
    assert clear_sky.on_panels(s, 30, 260) < clear_sky.MIN_SUNSHINE  # The sun is on the east panels only.
    result = clear_sky.now(calibration, morning, {"ppv1": 400, "ppv2": 4.0 * clear_sky.on_panels(s, 30, 100)})
    east, west = result["parts"]
    assert 97 <= east["pct"] <= 101 and west["pct"] is None
    assert west["clear_w"] > 0 and result["pct"] is not None


def test_a_fit_is_kept_and_only_used_for_the_same_place_and_panels(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    rows = samples(TWO_WAYS, [("ppv1", 260, 5.0), ("ppv2", 100, 4.0)], days=3)
    db.save_samples(conn, [db.Sample(ts, values) for ts, values in rows])
    found = clear_sky.decode(clear_sky.read(conn, START + 3 * DAY))
    assert len(found) == len(rows)  # Already one a minute.
    calibration = clear_sky.fit(found, TWO_WAYS, START + 3 * DAY)
    clear_sky.save(conn, calibration)
    assert clear_sky.load(conn, TWO_WAYS) == calibration
    assert clear_sky.load(conn, Location(45.0, 10.0, panel_tilt=30, panel_facing=100)) is None
    assert clear_sky.load(conn, None) is None
    clear_sky.save(conn, None)
    assert clear_sky.load(conn, TWO_WAYS) is None


def test_the_summary_says_which_factors_are_still_rough():
    def rough(days):
        rows = samples(TWO_WAYS, [("ppv1", 260, 5.0), ("ppv2", 100, 4.0)], days=days)
        found = clear_sky.summary(clear_sky.fit(rows, TWO_WAYS, START + days * DAY))
        assert found["days"] == days
        return [p["rough"] for p in found["parts"]]

    assert rough(2) == [True, True]
    assert rough(10) == [False, False]
    assert clear_sky.summary(None) is None
