"""Basic local dashboard for checking the inverter's data by eye.

Read-only: one background loop polls the inverter, and page refreshes only see
its latest result, so opening more tabs never adds load on the inverter.
Binds to localhost only. The serial number is never sent to the page.

Usage:
    python scripts/dashboard.py             # then open http://127.0.0.1:8000
    python scripts/dashboard.py --host 1.2.3.4 --port 502
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import goodwe
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response

from probe import device_info, resolve_target

BIND_HOST = "127.0.0.1"  # Localhost only: the app must never be publicly reachable.
BIND_PORT = 8000
POLL_SECONDS = 15
PAGE = Path(__file__).with_name("dashboard.html")


def timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


async def poll_forever(latest: dict[str, object], host: str, port: int) -> None:
    """Keep `latest` current. After a failure the last good readings stay, with `error` set."""
    inverter = None
    while True:
        try:
            if inverter is None:
                inverter = await goodwe.connect(host, port=port, timeout=2, retries=3)
                latest["device"] = device_info(inverter)
            values = await inverter.read_runtime_data()
            sensors = {sensor.id_: sensor for sensor in inverter.sensors()}
            latest["readings"] = [
                {
                    "id": sensor_id,
                    "name": sensors[sensor_id].name if sensor_id in sensors else "",
                    "unit": sensors[sensor_id].unit if sensor_id in sensors else "",
                    "value": value,
                }
                for sensor_id, value in values.items()
            ]
            latest["read_at"] = timestamp()
            latest["error"] = None
        except Exception as error:  # The loop must outlive any one failed read.
            inverter = None
            latest["error"] = f"{type(error).__name__}: {error}"
            latest["error_at"] = timestamp()
        await asyncio.sleep(POLL_SECONDS)


def create_app(host: str, port: int) -> FastAPI:
    latest: dict[str, object] = {"poll_seconds": POLL_SECONDS}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        task = asyncio.create_task(poll_forever(latest, host, port))
        yield
        task.cancel()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/")
    def page() -> HTMLResponse:
        return HTMLResponse(PAGE.read_text(encoding="utf-8"))

    # async so it runs on the event loop, never mid-way through a change by poll_forever.
    @app.get("/api/latest")
    async def api_latest() -> Response:
        return Response(json.dumps(latest, default=str), media_type="application/json")

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Basic local inverter dashboard")
    parser.add_argument("--host", help="inverter IP (default: INVERTER_IP from .env)")
    parser.add_argument("--port", type=int, help="default: INVERTER_PORT from .env, else 502")
    args = parser.parse_args()

    target = resolve_target(args.host, args.port)
    if target is None:
        return 1

    print(f"Polling {target[0]}:{target[1]} every {POLL_SECONDS}s")
    print(f"Open http://{BIND_HOST}:{BIND_PORT}")
    uvicorn.run(
        create_app(*target),
        host=BIND_HOST,
        port=BIND_PORT,
        log_level="warning",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
