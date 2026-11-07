"""Run the collector and the dashboard together.

Usage:
    solar-monitor                       # then open http://127.0.0.1:8000 (BIND_HOST changes the address)
    solar-monitor --host 1.2.3.4 --port 502
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from collections.abc import Callable

import uvicorn

from solar_monitor import db
from solar_monitor.api import create_app
from solar_monitor.auth import LoginRequired
from solar_monitor.collector import FLUSH_SECONDS, POLL_SECONDS, Collector
from solar_monitor.config import bind_host, db_path, is_local, login, resolve_target
from solar_monitor.stop import on_stop_request

BIND_PORT = 8000
CLOSE_EVENTS = {2, 5, 6}  # Windows: console window closed, user logging off, computer shutting down.
CLOSE_WAIT_SECONDS = 10  # Longer than Windows allows (about 5s), so Windows sets the real limit.

_registered_handler: object = None


def close_handler(server: uvicorn.Server, finished: threading.Event) -> Callable[[int], bool]:
    """Windows ends the process as soon as a handler returns, so wait here until the app has saved."""

    def handle(event: int) -> bool:
        if event not in CLOSE_EVENTS:
            return False  # Ctrl+C and Ctrl+Break carry on to uvicorn's own handling.
        server.should_exit = True
        finished.wait(CLOSE_WAIT_SECONDS)
        return True

    return handle


def stop_when_window_closes(server: uvicorn.Server, finished: threading.Event) -> None:
    global _registered_handler
    if sys.platform != "win32":
        return  # Elsewhere the app is stopped with signals, which uvicorn handles.
    import ctypes

    # Kept in a global because Windows would crash calling it after it was garbage collected.
    _registered_handler = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)(close_handler(server, finished))
    ctypes.windll.kernel32.SetConsoleCtrlHandler(_registered_handler, True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Local inverter monitor")
    parser.add_argument("--host", help="inverter IP (default: INVERTER_IP from .env)")
    parser.add_argument("--port", type=int, help="default: INVERTER_PORT from .env, else 502")
    args = parser.parse_args()

    host = bind_host()
    try:
        credentials = login()
    except ValueError as problem:
        print(problem, file=sys.stderr)
        return 1
    if credentials is None and not is_local(host):
        print(
            f"BIND_HOST={host} opens the dashboard to the network, so it needs a login. "
            "Set DASHBOARD_USER and DASHBOARD_PASSWORD in .env.",
            file=sys.stderr,
        )
        return 1

    target = resolve_target(args.host, args.port)
    if target is None:
        return 1

    path = db_path()
    conn = db.connect(path)  # Before polling, so a bad path stops the app rather than losing data.
    print(f"Polling {target[0]}:{target[1]} every {POLL_SECONDS}s")
    print(f"Saving to {path} every {FLUSH_SECONDS}s")
    print(f"Listening on http://{host}:{BIND_PORT}, login {'required' if credentials else 'off'}")
    app = create_app(Collector(*target, conn))
    if credentials:
        # Beside the database, the one folder the Pi's service lets the app write to.
        sessions_file = path.with_name("sessions.json")
        app.add_middleware(LoginRequired, username=credentials[0], password=credentials[1], sessions_file=sessions_file)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=BIND_PORT, log_level="warning", access_log=False))
    finished = threading.Event()
    stop_when_window_closes(server, finished)
    on_stop_request(os.getpid(), lambda: setattr(server, "should_exit", True))
    try:
        server.run()
    except KeyboardInterrupt:
        pass  # uvicorn has already shut down and saved, and raises this again on its way out.
    finally:
        conn.close()
        finished.set()
    return 0 if server.started else 1


if __name__ == "__main__":
    sys.exit(main())
