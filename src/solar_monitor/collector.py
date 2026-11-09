"""The single poll loop: the only code that talks to the inverter while the app runs.

Page refreshes only see its latest result, so opening more tabs never adds load on
the inverter. Readings are buffered and saved in batches, to spare the Pi's SD card,
so a crash or power cut can lose up to FLUSH_SECONDS of samples. Polling only reads:
a change someone asks for pauses it and goes through control.py.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import datetime

import goodwe

from solar_monitor import control, db
from solar_monitor.schedule import read_schedule

POLL_SECONDS = 15
FLUSH_SECONDS = 300
SETTINGS_SECONDS = 300  # These change only when someone edits them, in SEMS+ for example.


def timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log(message: str) -> None:
    """Goes to the journal on the Pi. Only state changes are logged, so a long outage is two lines."""
    print(f"{timestamp()} {message}", flush=True)


def _by(by: db.Who | None) -> str:
    """Who asked for a change, for the journal."""
    return f" by {by[1]}" if by else ""


def device_info(inverter: goodwe.Inverter) -> dict[str, object]:
    """Identifying details that are safe to print (no serial number)."""
    return {
        "family": type(inverter).__name__,
        "model": inverter.model_name,
        "rated_power_w": inverter.rated_power,
        "firmware": inverter.firmware,
        "arm_firmware": inverter.arm_firmware,
        "modbus_version": inverter.modbus_version,
    }


async def read_battery_limits(inverter: goodwe.Inverter) -> dict[str, object]:
    """The charge levels the battery stops discharging at, on-grid and during an outage, and
    the level it never charges past, whatever the time periods say.

    Despite the name, the discharge depth registers hold the level it stops at, not how deep it
    may go: the goodwe library takes depth of discharge as 100 minus the value. None if not readable.
    """
    limits: dict[str, object] = {}
    for name, setting in (
        ("min_soc", "battery_discharge_depth"),
        ("reserve_soc", "battery_discharge_depth_offline"),
        ("max_soc", "soc_upper_limit"),
    ):
        try:
            limits[name] = await inverter.read_setting(setting)
        except Exception:  # Not every inverter has these, and they aren't worth failing a poll over.
            limits[name] = None
    if not (isinstance(limits["max_soc"], (int, float)) and 1 <= limits["max_soc"] <= 100):
        limits["max_soc"] = None  # An inverter without it may answer 0 or 65535, which would mean nothing.
    return limits


class Collector:
    def __init__(self, host: str, port: int, conn: sqlite3.Connection) -> None:
        self.host = host
        self.port = port
        self.conn = conn
        self.inverter: goodwe.Inverter | None = None
        self.talking = asyncio.Lock()  # Held for a whole poll or write, so the two never interleave.
        self.buffer: list[db.Sample] = []
        self.storage: dict[str, object] = {"saved": 0, "pending": 0, "saved_at": None, "error": None}
        self.latest: dict[str, object] = {"poll_seconds": POLL_SECONDS, "storage": self.storage}

    def flush(self) -> None:
        """Save the buffer. If that fails, keep it for the next try and report the failure."""
        if not self.buffer:
            return
        try:
            db.save_samples(self.conn, self.buffer)
        except Exception as error:  # A storage problem must not stop the live readings.
            if self.storage["error"] is None:
                log(f"Saving failed, will keep trying: {type(error).__name__}: {error}")
            self.storage["error"] = f"{type(error).__name__}: {error}"
            return
        if self.storage["error"] is not None:
            log("Saving recovered")
        self.storage["saved"] += len(self.buffer)
        self.storage["saved_at"] = timestamp()
        self.storage["pending"] = 0
        self.storage["error"] = None
        self.buffer.clear()

    async def set_period(
        self, slot: int, wanted: control.Wanted, expected: str, by: db.Who | None = None
    ) -> dict[str, object]:
        """Change one TOU period, between polls. Whatever happens, `latest` then shows what it holds."""
        async with self.talking:
            if self.inverter is None:
                raise control.Refused("The inverter isn't connected at the moment, so nothing was sent.")
            try:
                result = await control.set_period(self.inverter, self.conn, slot, wanted, expected, by)
                if result["outcome"] == "confirmed":
                    changes = "; ".join(f"{c['field']} {c['old']} to {c['new']}" for c in result["changes"] or [])
                    log(f"TOU period {slot} changed{_by(by)}: {changes}")
                return result
            except control.Unconfirmed as problem:
                log(f"TOU period {slot} change{_by(by)} wasn't confirmed: {problem}")
                raise
            finally:
                self.latest["schedule"] = {**await read_schedule(self.inverter), "read_at": timestamp()}

    async def set_setting(self, name: str, value: int, expected: int, by: db.Who | None = None) -> dict[str, object]:
        """Change Backup mode or a battery limit, between polls. Whatever happens, `latest` then shows what it holds."""
        async with self.talking:
            if self.inverter is None:
                raise control.Refused("The inverter isn't connected at the moment, so nothing was sent.")
            try:
                result = await control.set_setting(self.inverter, self.conn, name, value, expected, by)
                if result["outcome"] == "confirmed":
                    log(f"Setting {name} changed{_by(by)}: {result['old']} to {result['new']}")
                return result
            except control.Unconfirmed as problem:
                log(f"Setting {name} change{_by(by)} wasn't confirmed: {problem}")
                raise
            finally:
                self.latest["battery_limits"] = await read_battery_limits(self.inverter)
                self.latest["schedule"] = {**await read_schedule(self.inverter), "read_at": timestamp()}

    async def run(self) -> None:
        """Keep `latest` current. After a failure the last good readings stay, with `error` set."""
        flushed_at = time.monotonic()
        settings_read_at = None
        while True:
            try:
                async with self.talking:
                    if self.inverter is None:
                        self.inverter = await goodwe.connect(self.host, port=self.port, timeout=2, retries=3)
                        self.latest["device"] = device_info(self.inverter)
                        settings_read_at = None  # A failed read of them often means a lost connection.
                    if settings_read_at is None or time.monotonic() - settings_read_at >= SETTINGS_SECONDS:
                        limits = await read_battery_limits(self.inverter)
                        if limits != self.latest.get("battery_limits"):
                            log(f"Battery limits (%): {', '.join(f'{name} {value}' for name, value in limits.items())}")
                        self.latest["battery_limits"] = limits
                        self.latest["schedule"] = {**await read_schedule(self.inverter), "read_at": timestamp()}
                        settings_read_at = time.monotonic()
                    values = await self.inverter.read_runtime_data()
                    sensors = {sensor.id_: sensor for sensor in self.inverter.sensors()}
                self.latest["readings"] = [
                    {
                        "id": sensor_id,
                        "name": sensors[sensor_id].name if sensor_id in sensors else "",
                        "unit": sensors[sensor_id].unit if sensor_id in sensors else "",
                        "value": value,
                    }
                    for sensor_id, value in values.items()
                ]
                self.latest["read_at"] = timestamp()
                if self.latest.get("error"):
                    log("Inverter reads recovered")
                self.latest["error"] = None
                self.buffer.append(db.Sample(int(time.time()), values))
                self.storage["pending"] = len(self.buffer)
            except Exception as error:  # The loop must outlive any one failed read.
                self.inverter = None
                if not self.latest.get("error"):
                    log(f"Inverter reads failing: {type(error).__name__}: {error}")
                self.latest["error"] = f"{type(error).__name__}: {error}"
                self.latest["error_at"] = timestamp()
            if time.monotonic() - flushed_at >= FLUSH_SECONDS:
                self.flush()
                flushed_at = time.monotonic()
            await asyncio.sleep(POLL_SECONDS)
