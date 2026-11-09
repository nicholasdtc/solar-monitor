"""Weather: pluggable forecast and warnings providers, and the app's own records they convert to.

To add a provider, write a module with a class like those here, add it to FORECASTS or WARNINGS,
and test it with a saved answer. Nothing outside this package knows about any one service.
"""

from __future__ import annotations

from solar_monitor.weather.base import ForecastProvider, WarningsProvider
from solar_monitor.weather.met_eireann import MetEireann
from solar_monitor.weather.met_norway import MetNorway
from solar_monitor.weather.open_meteo import OpenMeteo

FORECASTS: dict[str, ForecastProvider] = {p.name: p for p in (OpenMeteo(), MetNorway())}
WARNINGS: dict[str, WarningsProvider] = {p.name: p for p in (MetEireann(),)}

DEFAULT_FORECAST = "open-meteo"
