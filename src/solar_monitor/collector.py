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

POLL_SECONDS = 15
FLUSH_SECONDS = 300


def timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


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
            self.storage["error"] = f"{type(error).__name__}: {error}"
            return
        self.storage["saved"] += len(self.buffer)
        self.storage["saved_at"] = timestamp()
        self.storage["pending"] = 0
        self.storage["error"] = None
        self.buffer.clear()

    async def run(self) -> None:
        """Keep `latest` current. After a failure the last good readings stay, with `error` set."""
        inverter = None
        flushed_at = time.monotonic()
        while True:
            try:
                if inverter is None:
                    inverter = await goodwe.connect(self.host, port=self.port, timeout=2, retries=3)
                    self.latest["device"] = device_info(inverter)
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
                self.latest["error"] = None
                self.buffer.append(db.Sample(int(time.time()), values))
                self.storage["pending"] = len(self.buffer)
            except Exception as error:  # The loop must outlive any one failed read.
                inverter = None
                self.latest["error"] = f"{type(error).__name__}: {error}"
                self.latest["error_at"] = timestamp()
            if time.monotonic() - flushed_at >= FLUSH_SECONDS:
                self.flush()
                flushed_at = time.monotonic()
            await asyncio.sleep(POLL_SECONDS)
