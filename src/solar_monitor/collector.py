"""The single poll loop: the only code that talks to the inverter while the app runs.

Read-only. Page refreshes only see its latest result, so opening more tabs never
adds load on the inverter. Readings are buffered and saved in batches, to spare the
Pi's SD card, so a crash or power cut can lose up to FLUSH_SECONDS of samples.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import datetime

import goodwe

from solar_monitor import db
from solar_monitor.schedule import read_schedule

POLL_SECONDS = 15
FLUSH_SECONDS = 300
SETTINGS_SECONDS = 300  # These change only when someone edits them, in SEMS+ for example.


def timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log(message: str) -> None:
    """Goes to the journal on the Pi. Only state changes are logged, so a long outage is two lines."""
    print(f"{timestamp()} {message}", flush=True)


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
    """The charge levels the battery stops discharging at, on-grid and during an outage.

    Despite the name, these registers hold the level it stops at, not how deep it may go:
    the goodwe library takes depth of discharge as 100 minus the value. None if not readable.
    """
    limits: dict[str, object] = {}
    for name, setting in (("min_soc", "battery_discharge_depth"), ("reserve_soc", "battery_discharge_depth_offline")):
        try:
            limits[name] = await inverter.read_setting(setting)
        except Exception:  # Not every inverter has these, and they aren't worth failing a poll over.
            limits[name] = None
    return limits


class Collector:
    def __init__(self, host: str, port: int, conn: sqlite3.Connection) -> None:
        self.host = host
        self.port = port
        self.conn = conn
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

    async def run(self) -> None:
        """Keep `latest` current. After a failure the last good readings stay, with `error` set."""
        inverter = None
        flushed_at = time.monotonic()
        settings_read_at = None
        while True:
            try:
                if inverter is None:
                    inverter = await goodwe.connect(self.host, port=self.port, timeout=2, retries=3)
                    self.latest["device"] = device_info(inverter)
                    settings_read_at = None  # A failed read of them often means a lost connection.
                if settings_read_at is None or time.monotonic() - settings_read_at >= SETTINGS_SECONDS:
                    self.latest["battery_limits"] = await read_battery_limits(inverter)
                    self.latest["schedule"] = {**await read_schedule(inverter), "read_at": timestamp()}
                    settings_read_at = time.monotonic()
                values = await inverter.read_runtime_data()
                sensors = {sensor.id_: sensor for sensor in inverter.sensors()}
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
                inverter = None
                if not self.latest.get("error"):
                    log(f"Inverter reads failing: {type(error).__name__}: {error}")
                self.latest["error"] = f"{type(error).__name__}: {error}"
                self.latest["error_at"] = timestamp()
            if time.monotonic() - flushed_at >= FLUSH_SECONDS:
                self.flush()
                flushed_at = time.monotonic()
            await asyncio.sleep(POLL_SECONDS)
