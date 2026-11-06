"""The web app: serves the dashboard page, the latest readings and the logged history.

The serial number is never sent to the page.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response

from solar_monitor import db
from solar_monitor.collector import Collector

PAGE = Path(__file__).with_name("dashboard.html")


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

    @app.get("/")
    def page() -> HTMLResponse:
        return HTMLResponse(PAGE.read_text(encoding="utf-8"))

    # async so it runs on the event loop, never mid-way through a change by the collector.
    @app.get("/api/latest")
    async def api_latest() -> Response:
        return Response(json.dumps(collector.latest, default=str), media_type="application/json")

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

    return app
