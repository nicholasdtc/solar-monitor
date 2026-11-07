"""Settings from .env, and where the inverter is: arguments, then .env, then a LAN broadcast."""

from __future__ import annotations

import ipaddress
import os
import socket
import sys
from pathlib import Path

from dotenv import load_dotenv

from solar_monitor.password import is_hashed, parse

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PORT = 502  # Modbus TCP. goodwe uses UDP only when the port is 8899.

# goodwe.search_inverters() can't be used here: it passes its timeout in the
# comm_addr position and only enables broadcast on macOS. Same packet, sent directly.
DISCOVERY_MESSAGE = b"WIFIKIT-214028-READ"
DISCOVERY_PORT = 48899
DISCOVERY_WAIT_SECONDS = 3
DISCOVERY_ATTEMPTS = 4


def db_path() -> Path:
    """DB_PATH from .env (relative to the repo root), else data/solar-monitor.db."""
    load_dotenv(REPO_ROOT / ".env")
    return REPO_ROOT / (os.environ.get("DB_PATH", "").strip() or "data/solar-monitor.db")


def bind_host() -> str:
    """BIND_HOST from .env, else this machine only."""
    load_dotenv(REPO_ROOT / ".env")
    return os.environ.get("BIND_HOST", "").strip() or "127.0.0.1"


def is_local(host: str) -> bool:
    """Whether only this machine can reach the address. Anything unrecognised counts as open."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def login() -> tuple[str, str] | None:
    """The dashboard's username and password from .env, or None if neither is set.

    The password is either a hash from `python -m solar_monitor.password` or, as before, the password itself.
    Setting only one is a mistake that would leave the app open, so it's an error, as is a damaged hash.
    """
    load_dotenv(REPO_ROOT / ".env")
    username = os.environ.get("DASHBOARD_USER", "").strip()
    password = os.environ.get("DASHBOARD_PASSWORD", "").strip()
    if not username and not password:
        return None
    if not username or not password:
        raise ValueError("Set both DASHBOARD_USER and DASHBOARD_PASSWORD in .env, or neither.")
    if is_hashed(password):
        try:
            parse(password)
        except ValueError as problem:
            raise ValueError(
                f"DASHBOARD_PASSWORD in .env is a damaged hash ({problem}). "
                "Make it again with: python -m solar_monitor.password"
            ) from None
    return username, password


def discover_host() -> str | None:
    """Broadcast on the LAN and return the IP of the first inverter dongle to reply.

    A reply is occasionally missed, so it takes a few tries before giving up.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(DISCOVERY_WAIT_SECONDS)
        for _ in range(DISCOVERY_ATTEMPTS):
            try:
                sock.sendto(DISCOVERY_MESSAGE, ("255.255.255.255", DISCOVERY_PORT))
                _, (address, _) = sock.recvfrom(1024)
                return address
            except OSError:  # includes TimeoutError
                continue
    return None


def resolve_target(host: str | None, port: int | None) -> tuple[str, int] | None:
    """Work out the inverter's address: arguments, then .env, then broadcast."""
    load_dotenv(REPO_ROOT / ".env")
    host = host or os.environ.get("INVERTER_IP", "").strip()
    port = port or int(os.environ.get("INVERTER_PORT") or DEFAULT_PORT)

    if not host:
        print("No INVERTER_IP set, broadcasting to find the inverter...")
        host = discover_host()
        if not host:
            print(
                "No reply. Set INVERTER_IP in .env (see the router's device list or "
                "the GoodWe app), and check this PC is on the inverter's network.",
                file=sys.stderr,
            )
            return None
    return host, port
