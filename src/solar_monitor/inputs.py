"""What a person may enter, checked the same way whether it's typed on the page or imported from a file."""

from __future__ import annotations

from typing import Annotated

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
