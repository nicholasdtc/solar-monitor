"""Met Éireann's weather warnings for Ireland: free, no key, one JSON list for the whole country.

Its licence is based on CC BY 4.0, adding that the content mustn't be changed, so the headline and
description are kept and shown exactly as issued.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from solar_monitor.weather.base import Credit, Fetch, Location, Region, Warning, bad_answer

URL = "https://www.met.ie/Open_Data/json/warning_IRELAND.json"
LINK = "https://www.met.ie/warnings"

# Its own region codes for the counties.
COUNTIES = (
    Region("EI01", "Carlow"),
    Region("EI02", "Cavan"),
    Region("EI03", "Clare"),
    Region("EI04", "Cork"),
    Region("EI06", "Donegal"),
    Region("EI07", "Dublin"),
    Region("EI10", "Galway"),
    Region("EI11", "Kerry"),
    Region("EI12", "Kildare"),
    Region("EI13", "Kilkenny"),
    Region("EI15", "Laois"),
    Region("EI14", "Leitrim"),
    Region("EI16", "Limerick"),
    Region("EI18", "Longford"),
    Region("EI19", "Louth"),
    Region("EI20", "Mayo"),
    Region("EI21", "Meath"),
    Region("EI22", "Monaghan"),
    Region("EI23", "Offaly"),
    Region("EI24", "Roscommon"),
    Region("EI25", "Sligo"),
    Region("EI26", "Tipperary"),
    Region("EI27", "Waterford"),
    Region("EI29", "Westmeath"),
    Region("EI30", "Wexford"),
    Region("EI31", "Wicklow"),
)

# Its colours, then the CAP severity it also gives, as the app's levels.
COLOURS = {"yellow": "moderate", "orange": "severe", "red": "extreme"}
SEVERITIES = {"minor": "minor", "moderate": "moderate", "severe": "severe", "extreme": "extreme"}


def _seconds(stamp: object) -> int | None:
    try:
        return int(datetime.fromisoformat(str(stamp)).timestamp())
    except ValueError:
        return None


class MetEireann:
    name = "met-eireann"
    title = "Met Éireann"
    credit = Credit(
        # The statements its licence asks for: copyright, source, licence and disclaimer.
        "Copyright Met Éireann. Source: met.ie. Shown as issued. Met Éireann accepts no liability "
        "for errors or omissions in the data, its availability, or any loss from using it.",
        "CC BY 4.0",
        "https://www.met.ie/about-us/specialised-services/open-data",
    )
    min_interval = 20 * 60  # Warnings change faster than forecasts.
    regions = COUNTIES

    def __init__(self, url: str = URL) -> None:
        self.url = url

    def warnings(self, where: Location, region: str | None, fetch: Fetch) -> list[Warning]:
        return warnings_from(fetch(self.url, {}), region)


def warnings_from(answer: Any, region: str | None) -> list[Warning]:
    if not isinstance(answer, list):
        raise bad_answer("not a list of warnings")
    found = []
    for item in answer:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        regions = tuple(str(code) for code in item.get("regions") or ())
        if region and region not in regions:
            continue
        level = COLOURS.get(str(item.get("level", "")).lower()) or SEVERITIES.get(
            str(item.get("severity", "")).lower(), "moderate"
        )
        found.append(
            Warning(
                provider=MetEireann.name,
                id=str(item["id"]),
                level=level,
                kind=str(item.get("type") or ""),
                headline=str(item.get("headline") or ""),
                description=str(item.get("description") or ""),
                onset=_seconds(item.get("onset")),
                expiry=_seconds(item.get("expiry")),
                regions=regions,
                link=LINK,
            )
        )
    return found
