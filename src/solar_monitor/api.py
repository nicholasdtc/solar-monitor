"""The web app: serves the dashboard page, the latest readings, the logged history, appliances, exports and imports.

The serial number is never sent to the page. The routes that change the inverter are the TOU
programme one and the one for Backup mode and the battery's limits, which both hand over to control.py. PUT, JSON bodies and the content type an import insists
on can't be sent cross-site without a preflight, which no other site passes, so a web page
elsewhere can't change anything here.

Who may use which route is decided in auth.py, before a request gets here. Changes to the data are
recorded in the actions table under the person's name, as changes to the inverter are in its logs.
"""

from __future__ import annotations

import asyncio
import html
import importlib.metadata
import json
import platform
import os
import re
import shutil
import sqlite3
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StringConstraints
from starlette.background import BackgroundTask

from solar_monitor import accounts, auth, config, control, db, export, importer, meter, planning, weather
from solar_monitor.collector import Collector
from solar_monitor.inputs import ApplianceIn, DisplayPrefsIn, SettingsIn
from solar_monitor.weather import service as weather_service
from solar_monitor.weather.base import HOUR, Fetch
from solar_monitor.weather.http import HttpFetch
from solar_monitor.schedule import (
    ALL_MONTHS,
    EVERY_DAY,
    TIME_PERIODS,
    decode,
    describe_changes,
    describe_period,
    running_charge_period,
)

PAGE = Path(__file__).with_name("dashboard.html")
ZONE_NAME = re.compile(r"[A-Za-z0-9_+\-]+(?:/[A-Za-z0-9_+\-]+)*")


def system_time_zone() -> str | None:
    """The IANA name of this machine's time zone, such as Europe/Dublin, or None if it can't tell.

    The page shows times in it by default, and needs it for the tariff periods and the history
    buckets, which follow this machine's clock rather than the viewer's.
    """
    candidates = [os.environ.get("TZ", "").lstrip(":")]
    try:
        link = os.readlink("/etc/localtime")
        candidates.append(link.split("zoneinfo/", 1)[1] if "zoneinfo/" in link else "")
    except OSError:
        pass
    try:
        candidates.append(Path("/etc/timezone").read_text(encoding="utf-8").strip())
    except OSError:
        pass
    return next((name for name in candidates if name and ZONE_NAME.fullmatch(name)), None)


REPO = Path(__file__).resolve().parents[2]
COMMIT = re.compile(r"[0-9a-f]{40}")


def code_commit(repo: Path = REPO) -> str | None:
    """The commit the code was checked out at, shortened, or None if it can't tell.

    Read from the .git folder rather than by running git, which on the Pi refuses a checkout owned
    by another user. Handles a worktree, whose .git is a file naming its real folder.
    """
    try:
        git = repo / ".git"
        if git.is_file():
            git = repo / git.read_text(encoding="utf-8").removeprefix("gitdir:").strip()
        common = git / "commondir"
        shared = git / common.read_text(encoding="utf-8").strip() if common.is_file() else git
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        commit = head
        if head.startswith("ref: "):
            ref = head.removeprefix("ref: ")
            loose = shared / ref
            if loose.is_file():
                commit = loose.read_text(encoding="utf-8").strip()
            else:
                packed = (shared / "packed-refs").read_text(encoding="utf-8").splitlines()
                commit = next((line.split()[0] for line in packed if line.endswith(f" {ref}")), "")
    except (OSError, ValueError):
        return None
    return commit[:7] if COMMIT.fullmatch(commit) else None


def machine_uptime() -> float | None:
    """Seconds since this machine started, or None where there's no /proc, as on Windows."""
    try:
        return float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return None


def app_version() -> str | None:
    try:
        return importlib.metadata.version("solar-monitor")
    except importlib.metadata.PackageNotFoundError:
        return None


# Served from here, not a font service, so the page looks the same on a LAN with no internet.
FONTS = Path(__file__).with_name("fonts")
LOOPBACK_NAMES = ("127.0.0.1", "localhost", "::1")

# Measuring reads every sample in the window, so it isn't repeated on every page refresh.
MEASURE_SECONDS = 3600
MEASURE_WINDOW_SECONDS = 30 * 86400
WEATHER_HISTORY_MAX_DAYS = 366  # A year of hours is about 9,000 rows.


Clock = Annotated[str, StringConstraints(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]


class PeriodIn(BaseModel):
    """A TOU programme as the page wants it. Its type and whether it charges or discharges can't be sent."""

    expected: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{24}$")]  # The period's bytes, as the page saw them.
    on: Annotated[bool, Field(strict=True)]
    start: Clock
    end: Clock
    day_bits: Annotated[int, Field(strict=True, ge=1, le=EVERY_DAY)]  # Bit 0 is Sunday.
    month_bits: Annotated[int, Field(strict=True, ge=1, le=ALL_MONTHS)]  # Bit 0 is January.
    cutoff_soc: Annotated[int, Field(strict=True, ge=0, le=100)]
    percent: Annotated[int, Field(strict=True, ge=control.LOWEST_PERCENT, le=control.HIGHEST_PERCENT)]

    def wanted(self) -> control.Wanted:
        def clock(text: str) -> tuple[int, int]:
            hour, minute = text.split(":")
            return int(hour), int(minute)

        return control.Wanted(
            on=self.on,
            start=clock(self.start),
            end=clock(self.end),
            day_bits=self.day_bits,
            month_bits=self.month_bits,
            cutoff_soc=self.cutoff_soc,
            percent=self.percent,
        )


class SettingIn(BaseModel):
    """A new value for Backup mode (0 or 1) or a battery limit (%), and the value the page saw."""

    expected: Annotated[int, Field(strict=True, ge=0, le=100)]
    value: Annotated[int, Field(strict=True, ge=0, le=100)]


def _write_details(write: dict[str, object]) -> dict[str, object]:
    """What a write changed, and the programme as it was before, so the page can offer to put it back."""
    old, new = bytes.fromhex(write["old_bytes"]), bytes.fromhex(write["new_bytes"])
    try:
        before = decode(old)
    except ValueError:
        return {**write, "changes": None, "old": None}
    return {**write, "changes": describe_changes(old, new), "old": describe_period(write["slot"], before)}


def _positive(*candidates: object) -> float | None:
    return next((c for c in candidates if isinstance(c, (int, float)) and c > 0), None)


def _may_change_inverter(request: Request) -> bool:
    """With accounts, the account needs Write to inverter, which auth.py has already checked.
    Without any, only this machine may.

    Checking the name it was reached by stops a web page that points its own domain name at this
    machine, which would count as the same site and so pass the browser's cross-site protection.
    """
    user = auth.current_user(request)
    if not user.local:
        return user.may("inverter")
    return config.is_local(config.bind_host()) and request.url.hostname in LOOPBACK_NAMES


# How the actions table names the planning settings, as the page labels them.
SETTING_LABELS = {
    "capacity_kwh": "Starting battery capacity (kWh)",
    "limit_amps": "Current limit (A)",
    "surge_limit_w": "Surge limit (W)",
    "outage_hours": "Length of power cut (hours)",
    "run_until_minutes": "Run until",
    "tariff_day": "Day rate",
    "tariff_peak": "Peak rate",
    "tariff_night": "Night rate",
    "tariff_export": "Export rate",
    "band_night_start_minutes": "Night starts",
    "band_night_end_minutes": "Night ends",
    "band_peak_start_minutes": "Peak starts",
    "band_peak_end_minutes": "Peak ends",
    "pv_capacity_kwp": "Panels' total rating (kWp)",
    "panel_tilt_deg": "Panel tilt (°)",
    "panel_azimuth_deg": "Panels face (°)",
    "panel_azimuth_2_deg": "Second facing (°)",
    "latitude": "Latitude",
    "longitude": "Longitude",
    "forecast_provider": "Forecast from",
    "warnings_provider": "Warnings from",
    "warnings_region": "Warnings region",
}
# The location is personal, so the history says only that it changed.
PRIVATE_SETTINGS = ("latitude", "longitude")
APPLIANCE_LABELS = {
    "name": "name",
    "typical_w": "typical W",
    "peak_w": "peak W",
    "surge_w": "surge W",
    "use_in_outage": "in a power cut",
    "notes": "notes",
}


def _shown(name: str, value: object) -> str:
    if value is None or value == "":
        return "blank"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if name.endswith("_minutes") and isinstance(value, (int, float)):
        return f"{int(value) // 60:02d}:{int(value) % 60:02d}"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _settings_changes(old: dict[str, object], new: dict[str, object]) -> list[str]:
    return [
        SETTING_LABELS.get(name, name)
        if name in PRIVATE_SETTINGS
        else f"{SETTING_LABELS.get(name, name)} {_shown(name, old.get(name))} → {_shown(name, value)}"
        for name, value in new.items()
        if old.get(name) != value
    ]


def _appliance_changes(old: dict[str, object], new: dict[str, object]) -> list[str]:
    changes = []
    for name, label in APPLIANCE_LABELS.items():
        if old.get(name) == new.get(name):
            continue
        # Notes can be long, so only that they changed.
        changes.append(f"{label} changed" if name == "notes" else f"{label} {_shown(name, old.get(name))} → {_shown(name, new.get(name))}")
    return changes


def _import_summary(report: dict[str, object]) -> str:
    added = [f"{table['new']} {str(table['label']).lower()}" for table in report["tables"] if table["new"]]
    return f"Imported {report['label']}: {', '.join(added) or 'nothing new'}"


def _history_summary(row: dict[str, object]) -> str:
    """What a row of the history says. Writes to the inverter are described here, from their log."""
    if row["source"] == "tou":
        changes = None
        try:
            changes = describe_changes(bytes.fromhex(row["old"]), bytes.fromhex(row["new"]))
        except (TypeError, ValueError):
            pass
        described = "; ".join(f"{c['field']} {c['old']} → {c['new']}" for c in changes) if changes else "changed"
        return f"TOU programme {row['slot']}: {described}"
    if row["source"] == "setting":
        setting = control.SETTINGS.get(row["setting"])
        label = setting.label.removeprefix("The ").capitalize() if setting else str(row["setting"])
        if row["setting"] == "backup":
            return f"{label} {'on' if row['old'] else 'off'} → {'on' if row['new'] else 'off'}"
        return f"{label} {row['old']}% → {row['new']}%"
    return str(row["summary"])


def _credit(provider: object) -> dict[str, str]:
    credit = provider.credit  # type: ignore[attr-defined]
    return {"title": provider.title, "text": credit.text, "licence": credit.licence, "link": credit.link}  # type: ignore[attr-defined]


def create_app(collector: Collector, fetch: Fetch | None = None) -> FastAPI:
    """fetch is how the weather is asked for: the real one unless a test passes saved answers."""

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        task = asyncio.create_task(collector.run())
        weather_task = asyncio.create_task(weather_service.run(collector.conn, fetch or HttpFetch()))
        yield
        weather_task.cancel()
        await asyncio.wait([weather_task])
        # goodwe turns a cancel during a read into a failed read, which the collector
        # survives, so one cancel isn't always enough to end it.
        while not task.done():
            task.cancel()
            await asyncio.wait([task], timeout=0.1)
        collector.flush()  # Without this, every restart would lose the unsaved readings.

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    started = time.monotonic()
    measured: tuple[float, int] | None = None
    measured_at: float | None = None
    measured_overhead: dict[str, object] = {}
    measured_overhead_at: float | None = None
    # Where the energy went on days that have ended, which only an import can change.
    # Working it out reading by reading takes a noticeable moment on the Pi, so it's done once.
    finished_flows: dict[str, dict[str, float]] = {}
    flows_bands: dict[str, int] = {}  # The rate bands finished_flows split the import by.
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

    # Not async, as it reads a file, so it mustn't touch the database: the user was read by auth.py.
    @app.get("/")
    def page(request: Request) -> HTMLResponse:
        zone = html.escape(system_time_zone() or "")
        user = html.escape(json.dumps(auth.current_user(request).public()))
        body = PAGE.read_text(encoding="utf-8")
        body = body.replace('<meta name="server-time-zone" content="">', f'<meta name="server-time-zone" content="{zone}">', 1)
        body = body.replace('<meta name="session-user" content="">', f'<meta name="session-user" content="{user}">', 1)
        return HTMLResponse(body, headers={"Cache-Control": "no-store"})

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
        nonlocal flows_bands
        series = db.daily_series(collector.conn, days)
        tariff = meter.tariff(db.get_settings(collector.conn))
        if tariff["bands"] != flows_bands:
            finished_flows.clear()
            flows_bands = tariff["bands"]
        today = series[-1]["day"]
        first = next(day["day"] for day in series if day["day"] not in finished_flows or day["day"] == today)
        flows = db.daily_flows(
            collector.conn,
            int(datetime.fromisoformat(first).timestamp()),
            int(time.time()) + 1,
            collector.buffer,
            flows_bands,
        )
        for day in series:
            if first <= day["day"] < today:
                finished_flows[day["day"]] = flows.get(day["day"], {})
        flows = {**finished_flows, **flows}
        return {"days": [{**day, **flows.get(day["day"], {})} for day in series], "tariff": tariff}

    @app.get("/api/appliances")
    async def api_appliances() -> dict[str, object]:
        return overview()

    def find_appliance(appliance_id: int) -> dict[str, object] | None:
        return next((row for row in db.list_appliances(collector.conn) if row["id"] == appliance_id), None)

    def record(request: Request, summary: str) -> None:
        db.add_action(collector.conn, auth.current_user(request).who, "data", summary)

    @app.post("/api/appliances", status_code=201)
    async def api_add_appliance(appliance: ApplianceIn, request: Request) -> dict[str, object]:
        db.add_appliance(collector.conn, appliance.model_dump())
        record(request, f"Added appliance {appliance.name}")
        return overview()

    @app.put("/api/appliances/{appliance_id}")
    async def api_update_appliance(appliance_id: int, appliance: ApplianceIn, request: Request) -> dict[str, object]:
        old = find_appliance(appliance_id)
        if old is None or not db.update_appliance(collector.conn, appliance_id, appliance.model_dump()):
            raise HTTPException(status_code=404, detail="no such appliance")
        changes = _appliance_changes(old, appliance.model_dump())
        if changes:
            record(request, f"Changed appliance {old['name']}: {'; '.join(changes)}")
        return overview()

    @app.delete("/api/appliances/{appliance_id}")
    async def api_delete_appliance(appliance_id: int, request: Request) -> dict[str, object]:
        old = find_appliance(appliance_id)
        if old is None or not db.delete_appliance(collector.conn, appliance_id):
            raise HTTPException(status_code=404, detail="no such appliance")
        record(request, f"Removed appliance {old['name']}")
        return overview()

    @app.put("/api/settings")
    async def api_settings(settings: SettingsIn, request: Request) -> dict[str, object]:
        old = db.get_settings(collector.conn)
        new = settings.model_dump(exclude_unset=True)
        db.save_settings(collector.conn, new)
        changes = _settings_changes(old, new)
        if changes:
            record(request, f"Changed {'; '.join(changes)}")
        return overview()

    @app.get("/api/weather")
    async def api_weather() -> dict[str, object]:
        """The latest forecast from the start of this hour on, the chosen warnings that haven't
        ended, the sources' credits, and how each kind's last fetch went. Not the location."""
        now = int(time.time())
        settings = db.get_settings(collector.conn)
        forecaster = weather.FORECASTS.get(str(settings["forecast_provider"] or weather.DEFAULT_FORECAST))
        warner = weather.WARNINGS.get(str(settings["warnings_provider"])) if settings["warnings_provider"] else None
        return {
            "located": weather_service.location(settings) is not None,
            "forecast": db.latest_forecast(collector.conn, now - now % HOUR),
            "warnings": db.current_warnings(collector.conn, warner.name, now) if warner else [],
            "credits": [_credit(p) for p in (forecaster, warner) if p is not None],
            "fetches": db.last_weather_fetches(collector.conn),
            "history": _weather_history_status(settings, forecaster, now),
        }

    def _weather_history_status(settings: dict[str, object], forecaster: object, now: int) -> dict[str, object] | None:
        """How much of the log has its weather: the hours from the log's start, and how many are filled.
        With a record of past weather, up to the start of today and only from that record."""
        where = weather_service.location(settings)
        start = db.log_start(collector.conn)
        if where is None or forecaster is None or start is None:
            return None
        has_past = bool(forecaster.has_past)  # type: ignore[attr-defined]
        end = now // weather_service.DAY * weather_service.DAY if has_past else now - now % HOUR
        end = max(end, start)
        if has_past:
            missing = db.weather_hours_missing(collector.conn, start, end, weather_service.panels_of(where))
            filled = (end - start) // HOUR - len(missing)
        else:
            filled = db.weather_hours_count(collector.conn, start, end)
        return {"start": start, "end": end, "hours": (end - start) // HOUR, "filled": filled, "from_past": has_past}

    @app.get("/api/weather/history")
    async def api_weather_history(start: int, end: int) -> dict[str, object]:
        """The weather for each hour from `start` up to `end`, as far as it's kept."""
        if end <= start:
            raise HTTPException(status_code=400, detail="end must be after start")
        if end - start > WEATHER_HISTORY_MAX_DAYS * weather_service.DAY:
            raise HTTPException(status_code=400, detail=f"at most {WEATHER_HISTORY_MAX_DAYS} days at once")
        return {"start": start, "end": end, "hours": db.weather_history(collector.conn, start, end)}

    @app.get("/api/weather/providers")
    async def api_weather_providers() -> dict[str, object]:
        """What the settings can choose from, from the registry."""
        return {
            "default_forecast": weather.DEFAULT_FORECAST,
            "forecasts": [
                {"name": p.name, "title": p.title, "has_irradiance": p.has_irradiance, "has_past": p.has_past}
                for p in weather.FORECASTS.values()
            ],
            "warnings": [
                {"name": p.name, "title": p.title, "regions": [{"code": r.code, "name": r.name} for r in p.regions]}
                for p in weather.WARNINGS.values()
            ],
        }

    # Each person's own. Not recorded: they change nothing anyone else sees.
    @app.get("/api/prefs")
    async def api_prefs(request: Request) -> dict[str, object]:
        return {"prefs": db.get_display_prefs(collector.conn, auth.current_user(request).id)}

    @app.put("/api/prefs")
    async def api_save_prefs(prefs: DisplayPrefsIn, request: Request) -> dict[str, object]:
        """Replaces the whole set, so the page always sends every choice."""
        user_id = auth.current_user(request).id
        db.save_display_prefs(collector.conn, user_id, prefs.model_dump(exclude_none=True))
        return {"prefs": db.get_display_prefs(collector.conn, user_id)}

    @app.get("/api/about")
    async def api_about() -> dict[str, object]:
        """For the page's About box. Leaves out the inverter's address and serial, and where files are kept."""
        database = export.database_path(collector.conn)
        size = 0
        for path in (database, database.with_name(database.name + "-wal")):
            try:
                size += path.stat().st_size
            except OSError:
                pass
        try:
            disk = shutil.disk_usage(database.parent)
            free, total = disk.free, disk.total
        except OSError:
            free = total = None
        return {
            "version": app_version(),
            "commit": code_commit(),
            "python": platform.python_version(),
            "app_seconds": time.monotonic() - started,
            "machine_seconds": machine_uptime(),
            "time_zone": system_time_zone(),
            "poll_seconds": collector.latest.get("poll_seconds"),
            "first_reading": db.first_reading(collector.conn, collector.buffer),
            "database_bytes": size,
            "disk_free_bytes": free,
            "disk_total_bytes": total,
        }

    @app.get("/api/tou/writes")
    async def api_tou_writes() -> dict[str, object]:
        return {"writes": [_write_details(write) for write in db.list_inverter_writes(collector.conn)]}

    @app.put("/api/tou/{slot}")
    async def api_set_period(slot: int, change: PeriodIn, request: Request) -> dict[str, object]:
        if not _may_change_inverter(request):
            raise HTTPException(status_code=403, detail="Changing the inverter needs the app's login, so nothing was sent.")
        if not 1 <= slot <= TIME_PERIODS:
            raise HTTPException(status_code=404, detail="no such programme")
        try:
            result = await collector.set_period(slot, change.wanted(), change.expected, auth.current_user(request).who)
        except control.Refused as refused:
            raise HTTPException(status_code=409, detail=str(refused)) from None
        except control.Unconfirmed as problem:
            raise HTTPException(status_code=502, detail=str(problem)) from None
        return result

    @app.get("/api/inverter-settings/writes")
    async def api_setting_writes() -> dict[str, object]:
        return {"writes": db.list_setting_writes(collector.conn)}

    @app.put("/api/inverter-settings/{name}")
    async def api_set_setting(name: str, change: SettingIn, request: Request) -> dict[str, object]:
        if not _may_change_inverter(request):
            raise HTTPException(status_code=403, detail="Changing the inverter needs the app's login, so nothing was sent.")
        if name not in control.SETTINGS:
            raise HTTPException(status_code=404, detail="no such setting")
        try:
            result = await collector.set_setting(name, change.value, change.expected, auth.current_user(request).who)
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
    async def api_export_download(format: str, request: Request) -> FileResponse:
        choice = export.FORMATS.get(format)
        if choice is None:
            raise HTTPException(status_code=404, detail="no such format")
        collector.flush()  # So the file reaches the latest reading. It's saved early, nothing more.
        path = await asyncio.to_thread(export.write, choice, export.database_path(collector.conn))
        record(request, f"Exported {choice.label}")
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
            record(request, _import_summary(report))
        return report

    @app.get("/api/actions")
    async def api_actions(
        before: int | None = None,
        user: str | None = Query(default=None, max_length=64),
        kind: Literal["inverter", "data", "users", "login"] | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, object]:
        """The Action history tab: what each person did, latest first. `before` includes that second, so
        the page drops the rows it already has, by source and id."""
        rows = db.list_history(collector.conn, before=before, limit=limit, username=user, kind=kind)
        return {
            "rows": [
                {
                    "source": row["source"],
                    "id": row["id"],
                    "ts": row["ts"],
                    "username": row["username"],
                    "kind": row["kind"],
                    "summary": _history_summary(row),
                    "outcome": row["outcome"],
                }
                for row in rows
            ],
            "more": len(rows) == limit,
            "names": db.history_names(collector.conn),
        }

    accounts.add_routes(app, collector.conn)

    app.mount("/fonts", StaticFiles(directory=FONTS), name="fonts")

    return app
