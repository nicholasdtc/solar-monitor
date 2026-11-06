"""Stop every running copy of the app on Windows, letting each one save its readings first.

A copy running in the background may have no console, so Ctrl+C can't reach it. Each copy
listens on a named event instead, which works with or without a console.

Usage (or double-click stop-dashboard.bat):
    python -m solar_monitor.stop
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable

SAVE_WAIT_SECONDS = 15
EVENT_MODIFY_STATE = 0x0002
INFINITE = 0xFFFFFFFF

MODULE_RUN = re.compile(r"-m\s+solar_monitor\.main(\s|$)")
INSTALLED_COMMAND_RUN = re.compile(r"""[\\/]Scripts[\\/]solar-monitor(\.exe)?"?(\s|$)""", re.IGNORECASE)
PROCESS_QUERY = (
    "ConvertTo-Json -Compress -InputObject @("
    "Get-CimInstance Win32_Process -Filter \"Name like 'python%' or Name = 'solar-monitor.exe'\""
    " | Select-Object ProcessId,Name,CommandLine)"
)


def _kernel32():
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    return kernel32


def _event_name(pid: int) -> str:
    return f"Local\\solar-monitor-stop-{pid}"


def on_stop_request(pid: int, callback: Callable[[], None]) -> None:
    """Calls back, on its own thread, once ask_to_stop has been called for this process."""
    if sys.platform != "win32":
        return  # Elsewhere the app is stopped with signals, which uvicorn handles.
    kernel32 = _kernel32()
    event = kernel32.CreateEventW(None, False, False, _event_name(pid))

    def wait() -> None:
        kernel32.WaitForSingleObject(event, INFINITE)
        callback()

    threading.Thread(target=wait, daemon=True).start()


def ask_to_stop(pid: int) -> bool:
    """False if this process isn't listening, such as a copy started before this was added."""
    kernel32 = _kernel32()
    event = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, _event_name(pid))
    if not event:
        return False
    kernel32.SetEvent(event)
    kernel32.CloseHandle(event)
    return True


def is_server(name: str, command_line: str | None) -> bool:
    """Deliberately narrow: this decides which processes get stopped, possibly by force."""
    if name.lower() == "solar-monitor.exe":
        return True
    if not name.lower().startswith("python") or not command_line:
        return False
    return bool(MODULE_RUN.search(command_line) or INSTALLED_COMMAND_RUN.search(command_line))


def running_servers() -> list[int]:
    found = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", PROCESS_QUERY],
        capture_output=True,
        text=True,
        errors="replace",
        check=True,
    )
    processes = json.loads(found.stdout or "[]")
    return [p["ProcessId"] for p in processes if is_server(p["Name"], p["CommandLine"])]


def force_stop(pid: int) -> None:
    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)


def stop_all(wait_seconds: float = SAVE_WAIT_SECONDS) -> int:
    left = running_servers()
    if not left:
        print("No dashboard is running.")
        return 0

    if [pid for pid in left if ask_to_stop(pid)]:
        print("Asked the dashboard to save its readings and stop...")
        deadline = time.monotonic() + wait_seconds
        while left and time.monotonic() < deadline:
            time.sleep(0.5)
            left = running_servers()
    if not left:
        print("Stopped. All readings saved.")
        return 0

    for pid in left:
        force_stop(pid)
    print("Force-stopped a copy that didn't respond. Readings it hadn't saved yet are lost.")
    time.sleep(1)  # taskkill returns before the process has gone.
    left = running_servers()
    if left:
        print(f"Could not stop process {', '.join(map(str, left))}. It may belong to another user.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(stop_all())
