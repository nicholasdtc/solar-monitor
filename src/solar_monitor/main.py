"""Run the collector and the dashboard together.

Usage:
    solar-monitor                       # then open http://127.0.0.1:8000 (BIND_HOST changes the address)
    solar-monitor --host 1.2.3.4 --port 502
"""

from __future__ import annotations

import argparse
import sys

import uvicorn

from solar_monitor import db
from solar_monitor.api import create_app
from solar_monitor.collector import FLUSH_SECONDS, POLL_SECONDS, Collector
from solar_monitor.config import bind_host, db_path, resolve_target

BIND_PORT = 8000


def main() -> int:
    parser = argparse.ArgumentParser(description="Local inverter monitor")
    parser.add_argument("--host", help="inverter IP (default: INVERTER_IP from .env)")
    parser.add_argument("--port", type=int, help="default: INVERTER_PORT from .env, else 502")
    args = parser.parse_args()

    target = resolve_target(args.host, args.port)
    if target is None:
        return 1

    path = db_path()
    conn = db.connect(path)  # Before polling, so a bad path stops the app rather than losing data.
    print(f"Polling {target[0]}:{target[1]} every {POLL_SECONDS}s")
    print(f"Saving to {path} every {FLUSH_SECONDS}s")
    host = bind_host()
    print(f"Listening on http://{host}:{BIND_PORT}")
    try:
        uvicorn.run(
            create_app(Collector(*target, conn)),
            host=host,
            port=BIND_PORT,
            log_level="warning",
            access_log=False,
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
