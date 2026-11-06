"""Read-only inverter probe: identify the inverter and print one set of readings.

Only calls the goodwe library's read methods; nothing is written to the inverter.

The serial number is left out of the terminal output, so the output is safe to
paste elsewhere. The full reading is saved under captures/, which is gitignored.

Usage:
    python scripts/probe.py                 # host from INVERTER_IP, else broadcast
    python scripts/probe.py --host 1.2.3.4  # explicit host
    python scripts/probe.py --port 8899     # UDP, for dongles without Modbus TCP
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
from datetime import datetime
from pathlib import Path

import goodwe
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
CAPTURE_DIR = REPO_ROOT / "captures"
DEFAULT_PORT = 502  # Modbus TCP. goodwe uses UDP only when the port is 8899.

# goodwe.search_inverters() can't be used here: it passes its timeout in the
# comm_addr position and only enables broadcast on macOS. Same packet, sent directly.
DISCOVERY_MESSAGE = b"WIFIKIT-214028-READ"
DISCOVERY_PORT = 48899
DISCOVERY_WAIT_SECONDS = 3


def discover_host() -> str | None:
    """Broadcast on the LAN and return the IP of the first inverter dongle to reply."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(DISCOVERY_WAIT_SECONDS)
        try:
            sock.sendto(DISCOVERY_MESSAGE, ("255.255.255.255", DISCOVERY_PORT))
            _, (address, _) = sock.recvfrom(1024)
        except OSError:  # includes TimeoutError
            return None
    return address


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


def print_readings(inverter: goodwe.Inverter, readings: dict[str, object]) -> None:
    sensors = {sensor.id_: sensor for sensor in inverter.sensors()}
    width = max(map(len, readings), default=0)
    for sensor_id, value in readings.items():
        sensor = sensors.get(sensor_id)
        unit = sensor.unit if sensor else ""
        name = sensor.name if sensor else ""
        print(f"  {sensor_id:<{width}}  {f'{value} {unit}'.strip():<24}  {name}")


def save_capture(
    host: str, port: int, inverter: goodwe.Inverter, readings: dict[str, object]
) -> Path:
    now = datetime.now().astimezone()
    capture = {
        "captured_at": now.isoformat(timespec="seconds"),
        "host": host,
        "port": port,
        "serial_number": inverter.serial_number,
        **device_info(inverter),
        "readings": readings,
    }
    CAPTURE_DIR.mkdir(exist_ok=True)
    path = CAPTURE_DIR / f"probe-{now:%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(capture, indent=2, default=str), encoding="utf-8")
    return path


async def probe(host: str, port: int) -> Path:
    inverter = await goodwe.connect(host, port=port, timeout=2, retries=3)
    print("Inverter:")
    for key, value in device_info(inverter).items():
        print(f"  {key}: {value}")

    readings = await inverter.read_runtime_data()
    print(f"\nReadings ({len(readings)}):")
    print_readings(inverter, readings)
    return save_capture(host, port, inverter, readings)


def main() -> int:
    # Inverters can return non-ASCII strings, which the Windows console can't always encode.
    sys.stdout.reconfigure(errors="backslashreplace")

    parser = argparse.ArgumentParser(description="Read-only GoodWe inverter probe")
    parser.add_argument("--host", help="inverter IP (default: INVERTER_IP from .env)")
    parser.add_argument("--port", type=int, help="default: INVERTER_PORT from .env, else 502")
    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env")
    host = args.host or os.environ.get("INVERTER_IP", "").strip()
    port = args.port or int(os.environ.get("INVERTER_PORT") or DEFAULT_PORT)

    if not host:
        print("No INVERTER_IP set, broadcasting to find the inverter...")
        host = discover_host()
        if not host:
            print(
                "No reply. Set INVERTER_IP in .env (see the router's device list or "
                "the GoodWe app), and check this PC is on the inverter's network.",
                file=sys.stderr,
            )
            return 1

    print(f"Connecting to {host}:{port}...\n")
    try:
        capture_path = asyncio.run(probe(host, port))
    except (goodwe.InverterError, OSError) as error:
        print(f"Could not read the inverter at {host}:{port}: {error}", file=sys.stderr)
        return 1

    print(f"\nFull capture, including serial number: {capture_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
