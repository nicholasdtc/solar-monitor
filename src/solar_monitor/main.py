"""Run the collector and the dashboard together, or set an account's password.

Usage:
    solar-monitor                       # then open http://127.0.0.1:8000 (BIND_HOST changes the address)
    solar-monitor --host 1.2.3.4 --port 502
    solar-monitor reset-password admin  # set a password, making the owner's account if there isn't one
"""

from __future__ import annotations

import argparse
import getpass
import os
import sqlite3
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import uvicorn

from solar_monitor import auth, db
from solar_monitor.api import create_app
from solar_monitor.auth import LoginRequired
from solar_monitor.collector import FLUSH_SECONDS, POLL_SECONDS, Collector
from solar_monitor.config import bind_host, db_path, is_local, old_login, resolve_target
from solar_monitor.stop import on_stop_request

BIND_PORT = 8000
CLOSE_EVENTS = {2, 5, 6}  # Windows: console window closed, user logging off, computer shutting down.
CLOSE_WAIT_SECONDS = 10  # Longer than Windows allows (about 5s), so Windows sets the real limit.
COMMAND_LINE: db.Who = (None, "command line")
START_UP: db.Who = (None, "start-up")

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


def add_owner(conn: sqlite3.Connection, password: str, by: db.Who, how: str) -> None:
    """Make the owner's account. The first takes over the choices made before there were accounts."""
    first = not db.has_users(conn)
    owner = db.add_user(conn, auth.OWNER, auth.hash_password(password), True, ())
    if first:
        db.copy_display_prefs(conn, 0, owner)
    db.add_action(conn, by, "users", f"Made the owner's account, {auth.OWNER}, {how}")


def reset_password(username: str, path: Path, ask: Callable[[str], str] = getpass.getpass) -> int:
    """Set an account's password, from the machine that runs the app. For a lost password, and
    to make the owner's account, which is how accounts start."""
    name = username.strip().lower()
    if not path.is_file():
        print(f"There's no database at {path}. Set DB_PATH to the app's database and try again.", file=sys.stderr)
        return 1
    print(f"Database: {path}")
    conn = db.connect(path)
    try:
        row = db.find_user(conn, name)
        if row is None and name != auth.OWNER:
            print(
                f"There's no account named {name}. Admins add accounts on the Users screen. "
                f"To make the owner's account, run: solar-monitor reset-password {auth.OWNER}",
                file=sys.stderr,
            )
            return 1
        password = ask(f"New password for {name}: ")
        problem = auth.password_problem(password)
        if problem:
            print(f"{problem}. Nothing changed.", file=sys.stderr)
            return 1
        if ask("Type it again: ") != password:
            print("The two didn't match. Nothing changed.", file=sys.stderr)
            return 1
        if row is None:
            add_owner(conn, password, COMMAND_LINE, "from the command line")
            print(f"Made the owner's account. Log in as {auth.OWNER}.")
            return 0
        db.set_password_hash(conn, row["id"], auth.hash_password(password))
        ended = db.delete_sessions(conn, row["id"])
        db.add_action(conn, COMMAND_LINE, "users", f"Set a new password for {name}, logging them out ({ended})")
        print(f"Set a new password for {name}, and logged them out everywhere.")
        return 0
    finally:
        conn.close()


def prepare_accounts(conn: sqlite3.Connection, path: Path, host: str) -> bool:
    """Bring the login from before accounts over, and check the app can start. False if it can't."""
    # From before accounts. Everyone logs in again, now with their own account.
    path.with_name("sessions.json").unlink(missing_ok=True)
    old_user, old_password = old_login()
    if old_password and not db.has_users(conn):
        add_owner(conn, old_password, START_UP, "with the password from DASHBOARD_PASSWORD")
        print(f"Made the owner's account, with the password from DASHBOARD_PASSWORD. Log in as {auth.OWNER}.")
        if auth.password_problem(old_password):
            print(f"That password is weak. Change it from the menu, logged in as {auth.OWNER}.", file=sys.stderr)
    elif old_password or old_user:
        print(
            "DASHBOARD_USER and DASHBOARD_PASSWORD in .env aren't used any more. Remove them: "
            f"the owner's account, {auth.OWNER}, has its own password, which reset-password sets.",
            file=sys.stderr,
        )
    if not db.has_users(conn) and not is_local(host):
        print(
            f"BIND_HOST={host} opens the dashboard to the network, so it needs accounts. "
            f"Make the first, the owner's, with: solar-monitor reset-password {auth.OWNER}",
            file=sys.stderr,
        )
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Local inverter monitor")
    parser.add_argument("--host", help="inverter IP (default: INVERTER_IP from .env)")
    parser.add_argument("--port", type=int, help="default: INVERTER_PORT from .env, else 502")
    commands = parser.add_subparsers(dest="command")
    reset = commands.add_parser(
        "reset-password",
        help=f"set an account's password, making the owner's account ({auth.OWNER}) if there isn't one",
    )
    reset.add_argument("username")
    args = parser.parse_args()

    path = db_path()
    if args.command == "reset-password":
        return reset_password(args.username, path)

    host = bind_host()
    conn = db.connect(path)  # Before polling, so a bad path stops the app rather than losing data.
    try:
        if not prepare_accounts(conn, path, host):
            return 1
        target = resolve_target(args.host, args.port)
        if target is None:
            return 1
        accounts = len(db.list_users(conn, auth.OWNER))
        print(f"Polling {target[0]}:{target[1]} every {POLL_SECONDS}s")
        print(f"Saving to {path} every {FLUSH_SECONDS}s")
        print(
            f"Listening on http://{host}:{BIND_PORT}, "
            + (f"{accounts} account{'s' if accounts != 1 else ''}" if accounts else "no accounts, so no login")
        )
        app = create_app(Collector(*target, conn))
        app.add_middleware(LoginRequired, conn=conn, open_without_accounts=is_local(host))
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
            finished.set()  # Last, as Windows ends the process as soon as this is set.
        return 0 if server.started else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
