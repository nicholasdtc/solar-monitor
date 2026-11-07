"""The dashboard password as a scrypt hash, so .env doesn't hold the password itself.

Make one with:
    python -m solar_monitor.password

The hash holds its own cost settings and salt: scrypt:<n>:<r>:<p>:<salt hex>:<key hex>.
It has no $ or #, so .env reads it as it is, without quotes.
"""

from __future__ import annotations

import getpass
import hashlib
import hmac
import secrets
import sys

PREFIX = "scrypt"
# The cost OWASP recommends: 128 MiB and about 0.4s per login on a Pi 5. Logins are rare, so that's cheap.
N, R, P = 2**17, 8, 1
SALT_BYTES = 16
KEY_BYTES = 32
MAX_MEMORY = 2**30  # Stops a mistyped cost in .env from asking for more than the Pi has.


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int, size: int) -> bytes:
    # OpenSSL's default limit is 32 MiB, below the cost above, so the memory it needs is passed in.
    memory = 128 * r * (n + p + 2)
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=memory, dklen=size)


def hash_password(password: str, n: int = N, r: int = R, p: int = P) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    key = _scrypt(password, salt, n, r, p, KEY_BYTES)
    return f"{PREFIX}:{n}:{r}:{p}:{salt.hex()}:{key.hex()}"


def is_hashed(stored: str) -> bool:
    return stored.startswith(PREFIX + ":")


def parse(stored: str) -> tuple[int, int, int, bytes, bytes]:
    """The cost settings, salt and key from a hash. Raises ValueError if it isn't one this module made."""
    parts = stored.split(":")
    if len(parts) != 6 or parts[0] != PREFIX:
        raise ValueError("not a scrypt hash")
    try:
        n, r, p = (int(part) for part in parts[1:4])
        salt, key = bytes.fromhex(parts[4]), bytes.fromhex(parts[5])
    except ValueError:
        raise ValueError("not a scrypt hash") from None
    if n < 2 or n & (n - 1) or r < 1 or p < 1 or 128 * r * (n + p + 2) > MAX_MEMORY:
        raise ValueError("scrypt cost out of range")
    if len(salt) < SALT_BYTES or len(key) < KEY_BYTES:
        raise ValueError("scrypt salt or key too short")
    return n, r, p, salt, key


def verify(password: str, stored: str) -> bool:
    n, r, p, salt, key = parse(stored)
    return hmac.compare_digest(_scrypt(password, salt, n, r, p, len(key)), key)


def main() -> int:
    password = getpass.getpass("New dashboard password: ")
    if not password.strip():
        print("The password can't be blank.", file=sys.stderr)
        return 1
    if getpass.getpass("Type it again: ") != password:
        print("The two didn't match.", file=sys.stderr)
        return 1
    print("Put this line in .env in place of the old one:")
    print(f"DASHBOARD_PASSWORD={hash_password(password)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
