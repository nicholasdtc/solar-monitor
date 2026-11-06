"""The web app: serves the dashboard page, the latest readings, the logged history, appliances and exports.

The serial number is never sent to the page. The one route that changes the inverter is the TOU
power one, which hands over to control.py. PUT and JSON bodies can't be sent cross-site without a
preflight, which no other site passes, so a web page elsewhere can't change anything here.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StringConstraints
from starlette.background import BackgroundTask

from solar_monitor import config, control, db, export, planning
from solar_monitor.collector import Collector
from solar_monitor.schedule import TIME_PERIODS

PAGE = Path(__file__).with_name("dashboard.html")
# Served from here, not a font service, so the page looks the same on a LAN with no internet.
FONTS = Path(__file__).with_name("fonts")
LOOPBACK_NAMES = ("127.0.0.1", "localhost", "::1")

# Measuring reads every sample in the window, so it isn't repeated on every page refresh.
MEASURE_SECONDS = 3600
MEASURE_WINDOW_SECONDS = 30 * 86400

Watts = Annotated[float, Field(ge=0, le=1_000_000, allow_inf_nan=False)]
Positive = Annotated[float, Field(gt=0, le=1_000_000, allow_inf_nan=False)]
Rate = Annotated[float, Field(ge=0, le=100_000, allow_inf_nan=False)]


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
    outage_hours: Annotated[float, Field(ge=0.1, le=8760, allow_inf_nan=False)] | None = None
    run_until_minutes: Annotated[int, Field(ge=0, le=1439)] | None = None
    tariff_day: Rate | None = None
    tariff_peak: Rate | None = None
    tariff_night: Rate | None = None
    tariff_export: Rate | None = None


class PeriodPowerIn(BaseModel):
    percent: Annotated[int, Field(strict=True, ge=control.LOWEST_PERCENT, le=control.HIGHEST_PERCENT)]
    expected: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{24}$")]  # The period's bytes, as the page saw them.


def _positive(*candidates: object) -> float | None:
    return next((c for c in candidates if isinstance(c, (int, float)) and c > 0), None)


def _may_change_inverter(request: Request) -> bool:
    """With a login, the login has already let this request through. Without one, only this machine may.

    Checking the name it was reached by stops a web page that points its own domain name at this
    machine, which would count as the same site and so pass the browser's cross-site protection.
    """
    if config.login() is not None:
        return True
    return config.is_local(config.bind_host()) and request.url.hostname in LOOPBACK_NAMES


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
    measured_overhead: dict[str, object] = {}
    measured_overhead_at: float | None = None

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

    def overhead() -> dict[str, object]:
        nonlocal measured_overhead, measured_overhead_at
        if measured_overhead_at is None or time.monotonic() - measured_overhead_at >= MEASURE_SECONDS:
            samples = db.overhead_samples(collector.conn, int(time.time()) - MEASURE_WINDOW_SECONDS)
            measured_overhead = planning.measure_overhead(samples)
            measured_overhead_at = time.monotonic()
        return measured_overhead

    def overhead_now() -> dict[str, float | None] | None:
        if collector.latest.get("error"):
            return None
        values = readings()
        return planning.overhead_now(
            values.get("ppv"), values.get("load_ptotal"), values.get("pbattery1"), values.get("active_power_total")
        )

    def smoothed_battery_w() -> float | None:
        if collector.latest.get("error"):
            return None
        start = int(time.time()) - planning.SMOOTHING_SECONDS
        recent = db.battery_samples(collector.conn, start, collector.buffer)
        return planning.smoothed_power([power_w for _, power_w, _ in recent])

    def battery_estimate() -> dict[str, object] | None:
        if collector.latest.get("error"):
            return None
        soc = readings().get("battery_soc")
        known = capacity()
        estimate = planning.battery_estimate(
            soc if isinstance(soc, (int, float)) else None,
            smoothed_battery_w(),
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
        known_overhead = overhead()
        allowance = planning.Overhead(known_overhead["standby_w"], known_overhead["efficiency"])
        volts = _positive(values.get("backup_v1"), values.get("vgrid"))
        soc = values.get("battery_soc")
        load = values.get("load_ptotal")
        reserve = battery_limit("reserve_soc")
        known_soc = soc if isinstance(soc, (int, float)) else None
        volts_used = volts or planning.FALLBACK_VOLTS
        limit_w = None if settings["limit_amps"] is None else settings["limit_amps"] * volts_used
        summary = planning.summarize(
            db.list_appliances(collector.conn),
            volts=volts_used,
            limit_amps=settings["limit_amps"],
            surge_limit_w=settings["surge_limit_w"],
            live_load_w=load if isinstance(load, (int, float)) else None,
            soc=known_soc,
            reserve_soc=reserve,
            capacity_kwh=known["kwh"],
            overhead=allowance,
        )
        selected_w = summary["cases"]["selected_typical"]["watts"]
        outage_hours, run_until = settings["outage_hours"], settings["run_until_minutes"]
        return {
            **summary,
            "outage_plan": None
            if outage_hours is None
            else planning.outage_plan(outage_hours, selected_w, known_soc, reserve, known["kwh"], limit_w, allowance),
            "run_plan": None
            if run_until is None
            else planning.run_until_plan(
                datetime.now(),
                int(run_until),
                soc=known_soc,
                min_soc=battery_limit("min_soc"),
                capacity_kwh=known["kwh"],
                limit_w=limit_w,
                selected_w=selected_w,
                battery_w=smoothed_battery_w(),
                overhead=allowance,
            ),
            "tariffs": planning.tariff_values(
                settings["tariff_day"], settings["tariff_peak"], settings["tariff_night"], settings["tariff_export"]
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
            "overhead": known_overhead,
        }

    @app.get("/")
    def page() -> HTMLResponse:
        return HTMLResponse(PAGE.read_text(encoding="utf-8"))

    # async so it runs on the event loop, never mid-way through a change by the collector.
    @app.get("/api/latest")
    async def api_latest() -> Response:
        body = {
            **collector.latest,
            "battery_estimate": battery_estimate(),
            "overhead": {**overhead(), "now": overhead_now()},
        }
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
        db.save_settings(collector.conn, settings.model_dump(exclude_unset=True))
        return overview()

    @app.get("/api/tou/writes")
    async def api_tou_writes() -> dict[str, object]:
        return {"writes": db.list_inverter_writes(collector.conn)}

    @app.put("/api/tou/{slot}/power")
    async def api_set_period_power(slot: int, change: PeriodPowerIn, request: Request) -> dict[str, object]:
        if not _may_change_inverter(request):
            raise HTTPException(status_code=403, detail="Changing the inverter needs the app's login, so nothing was sent.")
        if not 1 <= slot <= TIME_PERIODS:
            raise HTTPException(status_code=404, detail="no such programme")
        try:
            result = await collector.set_period_power(slot, change.percent, change.expected)
        except control.Refused as refused:
            raise HTTPException(status_code=409, detail=str(refused)) from None
        except control.Unconfirmed as problem:
            raise HTTPException(status_code=502, detail=str(problem)) from None
        return result

    @app.get("/api/export")
    async def api_export() -> dict[str, object]:
        return {
            "formats": [
                {"id": name, "label": choice.label, "description": choice.description}
                for name, choice in export.FORMATS.items()
            ],
            "contents": export.summary(collector.conn),
        }

    @app.get("/api/export/download")
    async def api_export_download(format: str) -> FileResponse:
        choice = export.FORMATS.get(format)
        if choice is None:
            raise HTTPException(status_code=404, detail="no such format")
        collector.flush()  # So the file reaches the latest reading. It's saved early, nothing more.
        path = await asyncio.to_thread(export.write, choice, export.database_path(collector.conn))
        return FileResponse(
            path,
            media_type=choice.media_type,
            filename=path.name,
            headers={"Cache-Control": "no-store"},
            background=BackgroundTask(shutil.rmtree, path.parent, ignore_errors=True),
        )

    app.mount("/fonts", StaticFiles(directory=FONTS), name="fonts")

    return app
