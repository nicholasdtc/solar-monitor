"""The app's own weather records and units, which every provider converts to.

Units: °C, %, hPa, mm, m/s, degrees (the compass direction the wind comes from), W/m², and UTC
seconds for the start of the hour a record covers. A field a provider doesn't have is None.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Protocol

HOUR = 3600

# The app's own list of conditions. Each provider maps its codes onto these, and the page's icons
# follow it, so a new provider needs no new icons.
CONDITIONS = (
    "clear",
    "mostly clear",
    "partly cloudy",
    "cloudy",
    "fog",
    "drizzle",
    "rain",
    "heavy rain",
    "sleet",
    "snow",
    "showers",
    "thunder",
)

# The app's own warning levels, lowest first.
LEVELS = ("minor", "moderate", "severe", "extreme")


@dataclass(frozen=True)
class Location:
    """Where to forecast for, and how the panels sit there, for the sunshine on them."""

    latitude: float
    longitude: float
    panel_tilt: float | None = None  # degrees from flat
    panel_facing: float | None = None  # compass degrees: 90 east, 180 south, 270 west


@dataclass(frozen=True)
class Hour:
    hour_start: int
    condition: str | None = None  # one of CONDITIONS
    temperature_c: float | None = None
    feels_like_c: float | None = None
    humidity_pct: float | None = None
    pressure_hpa: float | None = None  # at sea level
    precipitation_mm: float | None = None
    precipitation_chance_pct: float | None = None
    cloud_pct: float | None = None
    cloud_low_pct: float | None = None
    cloud_mid_pct: float | None = None
    cloud_high_pct: float | None = None
    wind_ms: float | None = None
    gust_ms: float | None = None
    wind_from_deg: float | None = None
    ghi_wm2: float | None = None  # sunshine on flat ground: global horizontal irradiance, the hour's mean
    direct_wm2: float | None = None
    diffuse_wm2: float | None = None
    gti_wm2: float | None = None  # sunshine on the panels' tilt and facing
    is_day: bool | None = None


# Every field after hour_start, in order. The tables' columns are made from these.
HOUR_FIELDS = tuple(f.name for f in fields(Hour))[1:]


@dataclass(frozen=True)
class Warning:
    provider: str
    id: str  # the issuer's own id
    level: str  # one of LEVELS
    kind: str  # what it's about, as the issuer says it: Rain, Wind and so on
    headline: str  # as issued: some licences forbid changing it
    description: str  # as issued, which may be HTML. Shown as text, never as HTML.
    onset: int | None  # UTC seconds
    expiry: int | None
    regions: tuple[str, ...]  # the provider's region codes
    link: str  # the issuer's page for it


@dataclass(frozen=True)
class Credit:
    text: str  # such as "Weather data by Open-Meteo.com"
    licence: str  # such as "CC BY 4.0"
    link: str


@dataclass(frozen=True)
class Region:
    code: str
    name: str


class FetchError(Exception):
    """A fetch that didn't give a usable answer. status is the HTTP status, if there was one."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RateLimited(FetchError):
    """The service said to slow down (HTTP 429)."""


class NotModified(Exception):
    """Nothing new since the last fetch, or the service asked not to be asked again yet."""


class Fetch(Protocol):
    """GETs a URL with these query parameters and returns the parsed JSON. Raises FetchError,
    RateLimited or NotModified. Injected, so tests pass saved responses and never touch the network."""

    def __call__(self, url: str, params: dict[str, Any]) -> Any: ...


class ForecastProvider(Protocol):
    name: str
    title: str
    credit: Credit
    has_irradiance: bool
    has_past: bool
    min_interval: int  # seconds between forecast fetches it allows

    def forecast(self, where: Location, fetch: Fetch) -> list[Hour]: ...

    def past(self, where: Location, start: int, end: int, fetch: Fetch) -> list[Hour]: ...


class WarningsProvider(Protocol):
    name: str
    title: str
    credit: Credit
    min_interval: int
    regions: tuple[Region, ...]  # offered on the page, empty if it works from coordinates

    def warnings(self, where: Location, region: str | None, fetch: Fetch) -> list[Warning]: ...


def number(value: object) -> float | None:
    """A JSON number, or None for anything else, such as a gap."""
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def bad_answer(what: str) -> FetchError:
    return FetchError(f"unexpected answer: {what}")
