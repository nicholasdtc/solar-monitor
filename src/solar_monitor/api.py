"""The web app: serves the dashboard page, the latest readings, the logged history and appliances.

The serial number is never sent to the page. The only writes are to this app's own database:
nothing here writes to the inverter. JSON bodies can't be sent cross-site without a preflight,
which no other site passes, so a web page elsewhere can't change the appliances.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field, StringConstraints

from solar_monitor import db, planning
from solar_monitor.collector import Collector

PAGE = Path(__file__).with_name("dashboard.html")

# Measuring reads every sample in the window, so it isn't repeated on every page refresh.
MEASURE_SECONDS = 3600
MEASURE_WINDOW_SECONDS = 30 * 86400

Watts = Annotated[float, Field(ge=0, le=1_000_000, allow_inf_nan=False)]
Positive = Annotated[float, Field(gt=0, le=1_000_000, allow_inf_nan=False)]


class ApplianceIn(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    typical_w: Watts
    peak_w: Watts | None = None
    surge_w: Watts | None = None
    use_in_outage: bool = False
    notes: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] = ""


class SettingsIn(BaseModel):
    capacity_kwh: Positive | None = None
    target_soc: Annotated[float, Field(ge=1, le=100, allow_inf_nan=False)] | None = None
    limit_amps: Positive | None = None
    surge_limit_w: Positive | None = None


def _positive(*candidates: object) -> float | None:
    return next((c for c in candidates if isinstance(c, (int, float)) and c > 0), None)


def create_app(collector: Collector) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        task = asyncio.create_task(collector.run())
        yield
        # goodwe turns a cancel during a read into a failed read, which the collector
        # survives, so one cancel isn't always enough to end it.
        while not task.done():
            task.cancel()
            await asyncio.wait([task], timeout=0.1)
        collector.flush()  # Without this, every restart would lose the unsaved readings.

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    measured: tuple[float, int] | None = None
    measured_at: float | None = None

    def readings() -> dict[str, object]:
        return {r["id"]: r["value"] for r in collector.latest.get("readings", [])}

    def battery_limit(name: str) -> float:
        reported = collector.latest.get("battery_limits", {}).get(name)
        return reported if isinstance(reported, (int, float)) else planning.FALLBACK_MIN_SOC

    def capacity() -> dict[str, object]:
        """Measured from the log once there's enough of it, until then what was typed in."""
        nonlocal measured, measured_at
        if measured_at is None or time.monotonic() - measured_at >= MEASURE_SECONDS:
            samples = db.battery_samples(collector.conn, int(time.time()) - MEASURE_WINDOW_SECONDS)
            measured = planning.measure_capacity(samples)
            measured_at = time.monotonic()
        if measured:
            return {"kwh": measured[0], "source": "measured", "points": measured[1]}
        entered = db.get_settings(collector.conn)["capacity_kwh"]
        if entered is None:
            return {"kwh": None, "source": None, "points": 0}
        health = _positive(readings().get("battery_soh")) or 100
        return {"kwh": entered * health / 100, "source": "entered", "points": 0}

    def battery_estimate() -> dict[str, object] | None:
        if collector.latest.get("error"):
            return None
        start = int(time.time()) - planning.SMOOTHING_SECONDS
        recent = db.battery_samples(collector.conn, start, collector.buffer)
        soc = readings().get("battery_soc")
        known = capacity()
        estimate = planning.battery_estimate(
            soc if isinstance(soc, (int, float)) else None,
            planning.smoothed_power([power_w for _, power_w, _ in recent]),
            known["kwh"],
            battery_limit("min_soc"),
            db.get_settings(collector.conn)["target_soc"] or planning.DEFAULT_TARGET_SOC,
        )
        return estimate and {**estimate, "capacity": known}

    def overview() -> dict[str, object]:
        """Everything the appliances tab shows, so that any change can answer with all of it."""
        values = readings()
        settings = db.get_settings(collector.conn)
        known = capacity()
        volts = _positive(values.get("backup_v1"), values.get("vgrid"))
        soc = values.get("battery_soc")
        load = values.get("load_ptotal")
        reserve = battery_limit("reserve_soc")
        return {
            **planning.summarize(
                db.list_appliances(collector.conn),
                volts=volts or planning.FALLBACK_VOLTS,
                limit_amps=settings["limit_amps"],
                surge_limit_w=settings["surge_limit_w"],
                live_load_w=load if isinstance(load, (int, float)) else None,
                soc=soc if isinstance(soc, (int, float)) else None,
                reserve_soc=reserve,
                capacity_kwh=known["kwh"],
            ),
            "settings": settings,
            "capacity": known,
            "volts": volts,
            "fallback_volts": planning.FALLBACK_VOLTS,
            "soc": soc,
            "soh": values.get("battery_soh"),
            "min_soc": battery_limit("min_soc"),
            "reserve_soc": reserve,
            "reserve_reported": collector.latest.get("battery_limits", {}).get("reserve_soc") is not None,
            "conversion_efficiency": planning.CONVERSION_EFFICIENCY,
            "standby_w": planning.STANDBY_W,
        }

    @app.get("/")
    def page() -> HTMLResponse:
        return HTMLResponse(PAGE.read_text(encoding="utf-8"))

    # async so it runs on the event loop, never mid-way through a change by the collector.
    @app.get("/api/latest")
    async def api_latest() -> Response:
        body = {**collector.latest, "battery_estimate": battery_estimate()}
        return Response(json.dumps(body, default=str), media_type="application/json")

    # async too: the database connection belongs to the event loop's thread, and this way a
    # read can't land in the middle of a flush and count the moving readings twice or not at all.
    @app.get("/api/history")
    async def api_history(start: int, end: int) -> dict[str, object]:
        if end <= start:
            raise HTTPException(status_code=400, detail="end must be after start")
        bucket, points = db.history(collector.conn, start, end, collector.buffer)
        return {"start": start, "end": end, "bucket_seconds": bucket, "points": points}

    @app.get("/api/daily")
    async def api_daily(days: int = Query(default=14, ge=1, le=366)) -> dict[str, object]:
        return {"days": db.daily_series(collector.conn, days)}

    @app.get("/api/appliances")
    async def api_appliances() -> dict[str, object]:
        return overview()

    @app.post("/api/appliances", status_code=201)
    async def api_add_appliance(appliance: ApplianceIn) -> dict[str, object]:
        db.add_appliance(collector.conn, appliance.model_dump())
        return overview()

    @app.put("/api/appliances/{appliance_id}")
    async def api_update_appliance(appliance_id: int, appliance: ApplianceIn) -> dict[str, object]:
        if not db.update_appliance(collector.conn, appliance_id, appliance.model_dump()):
            raise HTTPException(status_code=404, detail="no such appliance")
        return overview()

    @app.delete("/api/appliances/{appliance_id}")
    async def api_delete_appliance(appliance_id: int) -> dict[str, object]:
        if not db.delete_appliance(collector.conn, appliance_id):
            raise HTTPException(status_code=404, detail="no such appliance")
        return overview()

    @app.put("/api/settings")
    async def api_settings(settings: SettingsIn) -> dict[str, object]:
        db.save_settings(collector.conn, settings.model_dump())
        return overview()

    return app
