"""The web app: serves the dashboard page and the latest readings.

The serial number is never sent to the page.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response

from solar_monitor.collector import Collector

PAGE = Path(__file__).with_name("dashboard.html")


def create_app(collector: Collector) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        task = asyncio.create_task(collector.run())
        yield
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        collector.flush()  # Without this, every restart would lose the unsaved readings.

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/")
    def page() -> HTMLResponse:
        return HTMLResponse(PAGE.read_text(encoding="utf-8"))

    # async so it runs on the event loop, never mid-way through a change by the collector.
    @app.get("/api/latest")
    async def api_latest() -> Response:
        return Response(json.dumps(collector.latest, default=str), media_type="application/json")

    return app
