"""Accounts, logging in, and what each account may do.

This wraps the app rather than its routes, so nothing can be left unprotected by accident,
including routes added later: each request is checked against ROUTES, and a route that isn't
listed there is for admins only.

Sessions are kept in the database, as a hash of each cookie's token, so reading the database
isn't enough to log in. The account is read afresh on every request, so a change to someone's
permissions, or removing them, applies at once.

The account named admin is the owner. It's always an admin, and can't be removed or demoted, so
there's always someone who can manage the others. If its password is lost, the command line on
the machine that runs the app sets a new one: solar-monitor reset-password admin.

Without any accounts the app is open, as it was before there were any, but only while it listens
on this machine alone. main.py won't start it that way on the network.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from urllib.parse import parse_qs

from starlette.requests import HTTPConnection, Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from solar_monitor import db

COOKIE = "solar_monitor_session"
SESSION_SECONDS = 30 * 24 * 60 * 60
MAX_FORM_BYTES = 4096
PAGE = Path(__file__).with_name("login.html")
WRONG = "Wrong username or password."

OWNER = "admin"
USERNAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}")
MIN_PASSWORD = 8
MAX_PASSWORD = 200

# What can be given or withheld, by the id the page's views name in data-perm. The Dashboard is for everyone.
TABS = {
    "statistics": "History",
    "grid-meter": "Grid meter",
    "counters": "Counters & checks",
    "appliances": "Appliances & tariffs",
    "schedule": "Battery",
    "fields": "All fields",
    "app-data": "App Data",
    "history": "Activity",
}
PERMISSIONS = {
    "inverter": "Write to inverter",  # Includes seeing the Battery tab.
    "edit": "Edit data",  # Appliances, tariffs and planning choices, and applying an import.
    **TABS,
}

# Cost settings for scrypt: about 16 MB and a fraction of a second on a Raspberry Pi.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1


@dataclass(frozen=True)
class User:
    id: int  # 0 without accounts
    name: str
    admin: bool
    permissions: frozenset[str]

    @classmethod
    def from_row(cls, row: dict[str, object]) -> User:
        name = str(row["username"])
        return cls(int(row["id"]), name, bool(row["admin"]) or name == OWNER, frozenset(row["permissions"]))

    @property
    def local(self) -> bool:
        """Without accounts: whoever is at this machine."""
        return self.id == 0

    @property
    def owner(self) -> bool:
        return not self.local and self.name == OWNER

    @property
    def who(self) -> db.Who:
        return (None if self.local else self.id, self.name)

    def may(self, permission: str) -> bool:
        if self.admin:
            return True
        if permission == "schedule" and "inverter" in self.permissions:
            return True
        return permission in self.permissions

    def public(self) -> dict[str, object]:
        """What the page is told, so it can leave out what this person can't use."""
        return {
            "name": self.name,
            "admin": self.admin,
            "owner": self.owner,
            "local": self.local,
            "permissions": [permission for permission in PERMISSIONS if self.may(permission)],
        }


LOCAL = User(0, "local", True, frozenset(PERMISSIONS))


def current_user(request: Request) -> User:
    """Who made the request. Without the login in front, as in tests, it's whoever is at this machine."""
    return getattr(request.state, "user", LOCAL)


def username_problem(name: str) -> str | None:
    if not USERNAME.fullmatch(name):
        return "A username is 1 to 32 letters, digits, dots, dashes or underscores, starting with a letter or digit"
    if name == LOCAL.name:
        return "That name is kept for using the app without accounts"
    return None


def password_problem(password: str) -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"A password needs at least {MIN_PASSWORD} characters"
    if len(password) > MAX_PASSWORD:
        return f"A password can't be longer than {MAX_PASSWORD} characters"
    return None


def hash_password(password: str) -> str:
    """Salted scrypt, from the standard library, as one string that says how it was made."""
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${key.hex()}"


@cache
def _stand_in() -> str:
    return hash_password(secrets.token_urlsafe(16))


def check_password(password: str, stored: str | None) -> bool:
    """Takes as long with no stored hash, so the time doesn't say whether a username exists."""
    try:
        kind, n, r, p, salt, key = (stored or _stand_in()).split("$")
        if kind != "scrypt":
            raise ValueError(kind)
        found = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=len(key) // 2
        )
    except ValueError:
        return False
    return secrets.compare_digest(found.hex(), key) and stored is not None


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _route(methods: str, path: str, *needs: str) -> tuple[frozenset[str], re.Pattern[str], tuple[str, ...]]:
    return frozenset(methods.split()), re.compile(path), needs


# What each route needs, as permissions every one of which the account must have. Anything not
# listed is for admins only, which includes the accounts themselves.
ROUTES = (
    _route("GET", r"/"),
    _route("GET HEAD", r"/fonts/.+"),
    _route("GET", r"/api/session"),
    _route("GET", r"/api/latest"),
    _route("GET PUT", r"/api/prefs"),
    _route("GET", r"/api/about"),
    _route("PUT", r"/api/password"),
    # The Dashboard's charts, which everyone has, use these as well as Statistics.
    _route("GET", r"/api/history"),
    _route("GET", r"/api/daily"),
    _route("GET", r"/api/weather"),
    _route("GET", r"/api/weather/providers"),
    _route("GET", r"/api/weather/history"),
    _route("GET", r"/api/meter", "grid-meter"),
    _route("GET", r"/api/appliances", "appliances"),
    _route("POST", r"/api/appliances", "appliances", "edit"),
    _route("PUT DELETE", r"/api/appliances/[^/]+", "appliances", "edit"),
    _route("PUT", r"/api/settings", "appliances", "edit"),
    _route("GET", r"/api/tou/writes", "schedule"),
    _route("GET", r"/api/inverter-settings/writes", "schedule"),
    _route("PUT", r"/api/tou/[^/]+", "schedule", "inverter"),
    _route("PUT", r"/api/inverter-settings/[^/]+", "schedule", "inverter"),
    _route("GET", r"/api/export", "app-data"),
    _route("GET", r"/api/export/download", "app-data"),
    _route("POST", r"/api/import", "app-data"),  # Applying one needs Edit data too, see needs().
    _route("GET", r"/api/actions", "history"),
)
ADMIN_ONLY = ("admin",)  # No account holds this as a permission, so only admins pass.
NOT_TRUE = {"0", "off", "f", "false", "n", "no"}  # What FastAPI reads as false.


def needs(method: str, path: str, query: bytes = b"") -> tuple[str, ...]:
    for methods, pattern, wanted in ROUTES:
        if method in methods and pattern.fullmatch(path):
            if path == "/api/import":
                values = parse_qs(query.decode(errors="replace"), keep_blank_values=True).get("apply", [])
                # Anything that isn't plainly false counts as applying, to be safe.
                if any(value.strip().lower() not in NOT_TRUE for value in values):
                    return (*wanted, "edit")
            return wanted
    return ADMIN_ONLY


class Throttle:
    """Wrong passwords from one address make it wait longer and longer before the next try.

    Waiting rather than locking out, so someone guessing can't lock the real person out. Kept in
    memory, so a restart forgets it, which costs a guesser more than it gains them.
    """

    FREE_TRIES = 5
    LONGEST_WAIT = 15 * 60
    FORGET_AFTER = 24 * 60 * 60
    MOST_ADDRESSES = 10_000

    def __init__(self) -> None:
        self.failures: dict[str, tuple[int, float]] = {}  # Address -> (wrong tries in a row, when the last was).

    def wait(self, address: str, now: float | None = None) -> float:
        """Seconds until this address may try again."""
        now = time.time() if now is None else now
        count, last = self.failures.get(address, (0, 0.0))
        if count < self.FREE_TRIES:
            return 0.0
        return max(0.0, last + min(2 ** (count - self.FREE_TRIES), self.LONGEST_WAIT) - now)

    def failed(self, address: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if len(self.failures) >= self.MOST_ADDRESSES:
            self.failures = {key: value for key, value in self.failures.items() if now - value[1] < self.FORGET_AFTER}
            while len(self.failures) >= self.MOST_ADDRESSES:
                del self.failures[next(iter(self.failures))]  # The oldest first.
        count, last = self.failures.pop(address, (0, now))
        if now - last >= self.FORGET_AFTER:
            count = 0
        self.failures[address] = (count + 1, now)

    def succeeded(self, address: str) -> None:
        self.failures.pop(address, None)


class LoginRequired:
    def __init__(self, app, conn: sqlite3.Connection, open_without_accounts: bool) -> None:
        """`conn` must be the event loop's connection. `open_without_accounts` is whether the app
        may be used without logging in while there are no accounts, which is only safe when it
        listens on this machine alone."""
        self.app = app
        self.conn = conn
        self.open_without_accounts = open_without_accounts
        self.page = PAGE.read_text(encoding="utf-8")
        self.throttle = Throttle()
        self.checking: set[str] = set()  # Addresses with a password being checked now.
        self.hashing = asyncio.Semaphore(2)  # Each check takes 16 MB, and the Pi has little to spare.
        self.accounts = False  # Once there are some, there always are: the owner can't be removed.

    def has_accounts(self) -> bool:
        self.accounts = self.accounts or db.has_users(self.conn)
        return self.accounts

    def user_for(self, token: str | None) -> User | None:
        if not token:
            return None
        row = db.session_user(self.conn, token_hash(token))
        return None if row is None else User.from_row(row)

    def login_page(self, error: str | None = None, status: int = 200, headers: dict[str, str] | None = None) -> Response:
        message = f'<p class="error" role="alert">{error}</p>' if error else ""
        return HTMLResponse(
            self.page.replace("<!--error-->", message),
            status_code=status,
            headers={"Cache-Control": "no-store", **(headers or {})},
        )

    @staticmethod
    async def read_form(request: Request) -> dict[str, list[str]] | None:
        """The posted form, or None if it's too big to be one of ours."""
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_FORM_BYTES:
                return None
        return parse_qs(body.decode(errors="replace"))

    async def log_in(self, request: Request) -> Response:
        address = request.client.host if request.client else ""
        wait = self.throttle.wait(address)
        if wait > 0 or address in self.checking:
            seconds = max(1, math.ceil(wait))
            plural = "" if seconds == 1 else "s"
            return self.login_page(
                f"Too many tries. Wait {seconds} second{plural}, then try again.", 429, {"Retry-After": str(seconds)}
            )
        self.checking.add(address)
        try:
            form = await self.read_form(request) or {}
            name = form.get("username", [""])[0].strip().lower()
            password = form.get("password", [""])[0]
            row = db.find_user(self.conn, name) if USERNAME.fullmatch(name) else None
            async with self.hashing:
                right = await asyncio.to_thread(check_password, password, row["password_hash"] if row else None)
            row = db.find_user(self.conn, name) if row else None  # In case they were removed meanwhile.
            if not (right and row):
                self.throttle.failed(address)
                # The name only if it's someone's, so a password typed in the wrong box isn't kept.
                if row:
                    db.add_action(self.conn, (row["id"], row["username"]), "login", f"Wrong password, from {address}")
                else:
                    db.add_action(self.conn, (None, ""), "login", f"Log-in with an unknown name, from {address}")
                return self.login_page(WRONG, 401)
        finally:
            self.checking.discard(address)

        self.throttle.succeeded(address)
        token = secrets.token_urlsafe(32)
        db.add_session(self.conn, token_hash(token), row["id"], int(time.time()) + SESSION_SECONDS)
        db.add_action(self.conn, (row["id"], row["username"]), "login", f"Logged in from {address}")
        response = RedirectResponse("/", status_code=303)
        # SameSite stops other sites from sending requests that carry the login.
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True, samesite="lax")
        return response

    async def log_out(self, request: Request, token: str | None, user: User | None) -> Response:
        form = await self.read_form(request) or {}
        # Only a logged-in session may end the person's others. The cookie isn't sent with a form
        # posted from another site, so no other site can do it.
        if form.get("everywhere") == ["1"] and user is not None:
            db.delete_sessions(self.conn, user.id)
            db.add_action(self.conn, user.who, "login", "Logged out everywhere")
        elif token:
            db.delete_session(self.conn, token_hash(token))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE)
        return response

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method, path = scope.get("method"), scope["path"]
        if not self.has_accounts():
            if not self.open_without_accounts:
                response = PlainTextResponse(
                    "There are no accounts yet. On the machine that runs the app, run: solar-monitor reset-password admin",
                    status_code=503,
                )
            elif path in ("/login", "/logout"):
                response = RedirectResponse("/", status_code=303)
            else:
                await self.pass_on(LOCAL, scope, receive, send)
                return
            await response(scope, receive, send)
            return

        token = HTTPConnection(scope).cookies.get(COOKIE)
        user = self.user_for(token)
        if (method, path) == ("POST", "/login"):
            response = await self.log_in(Request(scope, receive))
        elif (method, path) == ("POST", "/logout"):
            response = await self.log_out(Request(scope, receive), token, user)
        elif path == "/login" and method in ("GET", "HEAD"):
            response = RedirectResponse("/", status_code=303) if user else self.login_page()
        elif user is None:
            response = self.refusal(scope)
        elif not all(user.may(permission) for permission in needs(method, path, scope.get("query_string", b""))):
            response = JSONResponse(
                {"detail": "Your account isn't allowed to do this. An admin can change that under Users."},
                status_code=403,
                # Tells the page its permissions changed since it loaded, unlike a route's own refusals.
                headers={"X-Not-Allowed": "1"},
            )
        else:
            await self.pass_on(user, scope, receive, send)
            return
        await response(scope, receive, send)

    async def pass_on(self, user: User, scope, receive, send) -> None:
        scope.setdefault("state", {})["user"] = user
        await self.app(scope, receive, send)

    @staticmethod
    def refusal(scope) -> Response:
        if scope.get("method") in ("GET", "HEAD") and not scope["path"].startswith("/api/"):
            return RedirectResponse("/login", status_code=303)
        return JSONResponse({"detail": "Login required"}, status_code=401)
