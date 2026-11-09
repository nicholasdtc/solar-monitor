"""What a person may enter, checked the same way whether it's typed on the page or imported from a file."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

Watts = Annotated[float, Field(ge=0, le=1_000_000, allow_inf_nan=False)]
Positive = Annotated[float, Field(gt=0, le=1_000_000, allow_inf_nan=False)]
Rate = Annotated[float, Field(ge=0, le=100_000, allow_inf_nan=False)]
# A clock time on the half-hour, in minutes after midnight, because the meter counts in half-hours.
HalfHour = Annotated[int, Field(ge=0, le=1410, multiple_of=30)]


class ApplianceIn(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    typical_w: Watts
    peak_w: Watts | None = None
    surge_w: Watts | None = None
    use_in_outage: bool = False
    notes: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] = ""


class SettingsIn(BaseModel):
    capacity_kwh: Positive | None = None
    limit_amps: Positive | None = None
    surge_limit_w: Positive | None = None
    outage_hours: Annotated[float, Field(ge=0.1, le=8760, allow_inf_nan=False)] | None = None
    run_until_minutes: Annotated[int, Field(ge=0, le=1439)] | None = None
    tariff_day: Rate | None = None
    tariff_peak: Rate | None = None
    tariff_night: Rate | None = None
    tariff_export: Rate | None = None
    band_night_start_minutes: HalfHour | None = None
    band_night_end_minutes: HalfHour | None = None
    band_peak_start_minutes: HalfHour | None = None
    band_peak_end_minutes: HalfHour | None = None


class DisplayPrefsIn(BaseModel):
    """How the page shows things. One left out is on its default. The page checks them again on
    reading, so a choice it no longer offers just falls back to the default."""

    theme: Literal["system", "light", "dark"] | None = None
    date: Literal["auto", "dmy", "mdy", "dd/mm", "mm/dd", "iso"] | None = None
    time: Literal["auto", "24", "12"] | None = None
    # "server", "device" or a time zone's name, such as Europe/Dublin.
    zone: Annotated[str, StringConstraints(max_length=64, pattern=r"^[A-Za-z0-9_+\-]+(?:/[A-Za-z0-9_+\-]+)*$")] | None = None
    currency: Literal["none", "EUR", "GBP", "USD"] | None = None
    week: Literal["mon", "sun"] | None = None
    power: Literal["auto", "W", "kW"] | None = None
    # The section the page opens on, such as grid-meter.
    section: Annotated[str, StringConstraints(max_length=32, pattern=r"^[a-z]+(?:-[a-z]+)*$")] | None = None
    motion: Literal["system", "reduce", "full"] | None = None
