"""The web app: serves the dashboard page, the latest readings, the logged history, appliances, exports and imports.

The serial number is never sent to the page. The one route that changes the inverter is the TOU
power one, which hands over to control.py. PUT, JSON bodies and the content type an import insists
on can't be sent cross-site without a preflight, which no other site passes, so a web page
elsewhere can't change anything here.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import tempfile
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

from solar_monitor import config, control, db, export, importer, meter, planning
from solar_monitor.collector import Collector
from solar_monitor.inputs import ApplianceIn, SettingsIn
from solar_monitor.schedule import TIME_PERIODS, running_charge_period

PAGE = Path(__file__).with_name("dashboard.html")
# Served from here, not a font service, so the page looks the same on a LAN with no internet.
FONTS = Path(__file__).with_name("fonts")
LOOPBACK_NAMES = ("127.0.0.1", "localhost", "::1")

# Measuring reads every sample in the window, so it isn't repeated on every page refresh.
MEASURE_SECONDS = 3600
MEASURE_WINDOW_SECONDS = 30 * 86400


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
    # Where the energy went on days that have ended, which only an import can change.
    # Working it out reading by reading takes a noticeable moment on the Pi, so it's done once.
    finished_flows: dict[str, dict[str, float]] = {}
    # The meter's half-hours, as the page wants them. Only an import changes them.
    meter_readings: dict[str, object] | None = None
    importing = asyncio.Lock()

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

    def charge_limit() -> tuple[float, dict[str, object]]:
        """Where charging stops now, and what sets it: the inverter's overall limit, or the
        cut-off of the TOU charge period running now if that's lower."""
        upper = collector.latest.get("battery_limits", {}).get("max_soc")
        limit, source = (upper, {"source": "inverter"}) if upper is not None else (
            planning.FALLBACK_MAX_SOC, {"source": "fallback"}
        )
        period = running_charge_period(collector.latest.get("schedule"), datetime.now())
        if period and period["cutoff_soc"] <= limit:
            limit, source = period["cutoff_soc"], {"source": "period", "slot": period["slot"], "end": period["end"]}
        return limit, source

    def battery_estimate() -> dict[str, object] | None:
        if collector.latest.get("error"):
            return None
        soc = readings().get("battery_soc")
        known = capacity()
        max_soc, limit_from = charge_limit()
        estimate = planning.battery_estimate(
            soc if isinstance(soc, (int, float)) else None,
            smoothed_battery_w(),
            known["kwh"],
            battery_limit("min_soc"),
            max_soc,
        )
        if estimate is None:
            return None
        return {**estimate, "limit_from": limit_from if estimate["state"] == "charging" else None, "capacity": known}

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
    async def api_history(start: int, end: int, bucket: int | None = None) -> dict[str, object]:
        """With a bucket, the range is widened to whole buckets, and the answer says by how much."""
        if end <= start:
            raise HTTPException(status_code=400, detail="end must be after start")
        if bucket is not None:
            if bucket not in db.BUCKET_SECONDS:
                raise HTTPException(status_code=400, detail=f"bucket must be one of {list(db.BUCKET_SECONDS)}")
            start, end = db.whole_buckets(start, end, bucket)
            if (end - start) / bucket > db.MAX_BUCKETS_PER_REQUEST:
                raise HTTPException(status_code=400, detail=f"at most {db.MAX_BUCKETS_PER_REQUEST} buckets at once")
        bucket, points = db.history(collector.conn, start, end, collector.buffer, bucket)
        return {
            "start": start,
            "end": end,
            "bucket_seconds": bucket,
            "first_ts": db.first_reading(collector.conn, collector.buffer),
            "points": points,
        }

    @app.get("/api/daily")
    async def api_daily(days: int = Query(default=14, ge=1, le=366)) -> dict[str, object]:
        series = db.daily_series(collector.conn, days)
        today = series[-1]["day"]
        first = next(day["day"] for day in series if day["day"] not in finished_flows or day["day"] == today)
        flows = db.daily_flows(
            collector.conn, int(datetime.fromisoformat(first).timestamp()), int(time.time()) + 1, collector.buffer
        )
        for day in series:
            if first <= day["day"] < today:
                finished_flows[day["day"]] = flows.get(day["day"], {})
        flows = {**finished_flows, **flows}
        return {"days": [{**day, **flows.get(day["day"], {})} for day in series]}

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

    @app.get("/api/meter")
    async def api_meter() -> Response:
        """The meter's half-hours from ESB Networks, the tariff to price them, and this app's own grid import."""
        nonlocal meter_readings
        if meter_readings is None:
            meter_readings = meter.readings(collector.conn)
        collector.flush()  # So this app's record runs to the latest reading.
        body = {
            **meter_readings,
            **meter.tariff(db.get_settings(collector.conn)),
            "app": meter.app_days(collector.conn),
        }
        return Response(json.dumps(body, separators=(",", ":")), media_type="application/json")

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

    @app.post("/api/import")
    async def api_import(request: Request, apply: bool = False) -> dict[str, object]:
        """The body is the export file. Says what it would add, and adds it only if `apply` is set."""
        nonlocal measured_at, measured_overhead_at, meter_readings
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/octet-stream":
            raise HTTPException(status_code=415, detail="Send the file itself, as application/octet-stream.")
        if importing.locked():
            raise HTTPException(status_code=409, detail="Another import is running. Try again when it has finished.")
        async with importing:
            folder = Path(tempfile.mkdtemp(prefix="solar-monitor-import-"))
            try:
                path = folder / "upload"
                with path.open("wb") as upload:
                    async for chunk in request.stream():
                        upload.write(chunk)
                collector.flush()  # So what it says is already here includes the latest readings.
                report = await asyncio.to_thread(importer.run, path, export.database_path(collector.conn), apply=apply)
            except importer.BadFile as bad:
                raise HTTPException(status_code=400, detail=str(bad)) from None
            except (sqlite3.Error, OSError) as error:
                raise HTTPException(
                    status_code=500,
                    detail=f"The import couldn't finish: {error}. Anything saved so far stays, and importing the same file again adds the rest.",
                ) from None
            finally:
                shutil.rmtree(folder, ignore_errors=True)
        if apply:
            measured_at = measured_overhead_at = None  # So the measurements include what just arrived.
            finished_flows.clear()
            meter_readings = None
        return report

    app.mount("/fonts", StaticFiles(directory=FONTS), name="fonts")

    return app
