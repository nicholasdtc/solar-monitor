"""Sunrise and sunset, worked out from the location: no service needed.

NOAA's solar calculator method, good to about a minute away from the poles. Rise and set are when
the sun's centre is 0.833° below the horizon, which allows for refraction and the sun's radius.
"""

from __future__ import annotations

import math

DAY = 86400
ZENITH = 90.833


def _events(lat: float, lon: float, day_start: int) -> tuple[int | None, int | None]:
    """The rise and set for the UTC day starting at `day_start`, or None when the sun doesn't rise
    or doesn't set that day."""
    julian_day = day_start / DAY + 2440587.5 + 0.5  # At noon UTC.
    t = (julian_day - 2451545.0) / 36525.0
    mean_long = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360
    anomaly = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    eccentricity = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    m = math.radians(anomaly)
    centre = (
        math.sin(m) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + math.sin(2 * m) * (0.019993 - 0.000101 * t)
        + math.sin(3 * m) * 0.000289
    )
    omega = math.radians(125.04 - 1934.136 * t)
    apparent = math.radians(mean_long + centre - 0.00569 - 0.00478 * math.sin(omega))
    obliquity = math.radians(
        23 + (26 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60) / 60 + 0.00256 * math.cos(omega)
    )
    declination = math.asin(math.sin(obliquity) * math.sin(apparent))
    y = math.tan(obliquity / 2) ** 2
    l0 = math.radians(mean_long)
    equation_of_time = 4 * math.degrees(
        y * math.sin(2 * l0)
        - 2 * eccentricity * math.sin(m)
        + 4 * eccentricity * y * math.sin(m) * math.cos(2 * l0)
        - 0.5 * y * y * math.sin(4 * l0)
        - 1.25 * eccentricity * eccentricity * math.sin(2 * m)
    )
    phi = math.radians(lat)
    cos_angle = math.cos(math.radians(ZENITH)) / (math.cos(phi) * math.cos(declination)) - math.tan(phi) * math.tan(
        declination
    )
    if not -1 <= cos_angle <= 1:
        return None, None
    hour_angle = math.degrees(math.acos(cos_angle))
    noon = 720 - 4 * lon - equation_of_time  # Minutes after midnight UTC.
    return (
        day_start + round((noon - 4 * hour_angle) * 60),
        day_start + round((noon + 4 * hour_angle) * 60),
    )


def times(lat: float, lon: float, start: int, end: int) -> dict[str, list[int]]:
    """Every sunrise and sunset from `start` up to `end`, in UTC seconds, soonest first. Wherever
    the location, each falls in its own local day, so the page can match them to days in any zone."""
    rises: list[int] = []
    sets: list[int] = []
    day = start // DAY * DAY - DAY  # A day either side, for longitudes far from Greenwich.
    while day < end + DAY:
        rise, set_ = _events(lat, lon, day)
        if rise is not None and start <= rise < end:
            rises.append(rise)
        if set_ is not None and start <= set_ < end:
            sets.append(set_)
        day += DAY
    return {"rises": rises, "sets": sets}
