"""A login page and session cookie in front of the whole app.

This wraps the app rather than its routes, so nothing can be left unprotected by accident,
including routes added later. Sessions are kept in memory: restarting the app logs everyone out,
and logging out really does end the session rather than just forgetting the cookie.

After LOCK_AFTER wrong logins within LOCK_SECONDS, every login is refused for LOCK_SECONDS, the right one too,
so guessing gets nowhere. The lock is for everyone rather than for one address, because an address is easy
to change. Restarting the app lifts it. Every login is logged with the address it came from, which on the Pi
goes to the journal. The username typed isn't logged, since it's sometimes the password in the wrong box.
"""

from __future__ import annotations

import asyncio
import math
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs

from starlette.concurrency import run_in_threadpool
from starlette.requests import HTTPConnection, Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from solar_monitor.collector import log
from solar_monitor.password import is_hashed, verify

COOKIE = "solar_monitor_session"
SESSION_SECONDS = 7 * 24 * 60 * 60
MAX_FORM_BYTES = 4096
LOCK_AFTER = 5
LOCK_SECONDS = 15 * 60
PAGE = Path(__file__).with_name("login.html")
ERROR = '<p class="error" role="alert">Wrong username or password.</p>'
LOCKED = '<p class="error" role="alert">Too many wrong logins. Try again in {wait}.</p>'


class LoginRequired:
    def __init__(self, app, username: str, password: str) -> None:
        """`password` is a hash from solar_monitor.password, or the password itself."""
        self.app = app
        self.username = username.encode()
        self.password = password
        self.sessions: dict[str, float] = {}  # Token to expiry time.
        self.failures: list[float] = []  # When each recent wrong login was.
        self.locked_until = 0.0
        self.checking = asyncio.Lock()  # One login at a time, so a burst can't all be checked before the lock starts.
        self.page = PAGE.read_text(encoding="utf-8")

    def logged_in(self, token: str | None) -> bool:
        return self.sessions.get(token, 0) > time.time()

    def correct(self, username: str, password: str) -> bool:
        # Both are always checked, so the time taken doesn't reveal which one was wrong.
        if is_hashed(self.password):
            right_password = verify(password, self.password)
        else:
            right_password = secrets.compare_digest(password.encode(), self.password.encode())
        return secrets.compare_digest(username.encode(), self.username) & right_password

    def login_page(self, message: str = "", status_code: int = 200, headers: dict[str, str] | None = None) -> Response:
        html = self.page.replace("<!--error-->", message)
        return HTMLResponse(html, status_code=status_code, headers={"Cache-Control": "no-store", **(headers or {})})

    def locked_page(self, now: float) -> Response:
        seconds = math.ceil(self.locked_until - now)
        minutes = math.ceil(seconds / 60)
        message = LOCKED.format(wait="1 minute" if minutes == 1 else f"{minutes} minutes")
        return self.login_page(message, status_code=429, headers={"Retry-After": str(seconds)})

    @staticmethod
    async def read_form(request: Request) -> dict[str, list[str]] | None:
        """The login form, or None if it's too big to be one."""
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_FORM_BYTES:
                return None
        return parse_qs(body.decode(errors="replace"))

    async def log_in(self, request: Request) -> Response:
        form = await self.read_form(request)  # Before taking the lock, so a slow upload can't hold up anyone else.
        address = request.client.host if request.client else "an unknown address"
        async with self.checking:
            now = time.time()
            if now < self.locked_until:
                log(f"Login from {address} refused: logins are locked")
                return self.locked_page(now)
            # Checked away from the event loop, because the hash takes long enough to hold up polling.
            right = form is not None and await run_in_threadpool(
                self.correct, form.get("username", [""])[0], form.get("password", [""])[0]
            )
            if not right:
                self.failures = [when for when in self.failures if when > now - LOCK_SECONDS] + [now]
                if len(self.failures) < LOCK_AFTER:
                    log(f"Login from {address} failed: wrong username or password ({len(self.failures)} of {LOCK_AFTER})")
                    return self.login_page(ERROR, status_code=401)
                self.failures = []
                self.locked_until = now + LOCK_SECONDS
                log(
                    f"Login from {address} failed: wrong username or password, {LOCK_AFTER} times in "
                    f"{LOCK_SECONDS // 60} minutes, so all logins are locked for {LOCK_SECONDS // 60} minutes"
                )
                return self.locked_page(now)
            self.failures = []

        log(f"Login from {address} succeeded")
        self.sessions = {token: expiry for token, expiry in self.sessions.items() if expiry > now}
        token = secrets.token_urlsafe(32)
        self.sessions[token] = now + SESSION_SECONDS
        response = RedirectResponse("/", status_code=303)
        # SameSite stops other sites from sending requests that carry the login.
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True, samesite="lax")
        return response

    def log_out(self, token: str | None) -> Response:
        self.sessions.pop(token, None)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE)
        return response

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return

        token = HTTPConnection(scope).cookies.get(COOKIE)
        logged_in = self.logged_in(token)
        route = (scope.get("method"), scope["path"])
        if route == ("POST", "/login"):
            response = await self.log_in(Request(scope, receive))
        elif route == ("POST", "/logout"):
            response = self.log_out(token)
        elif route in (("GET", "/login"), ("HEAD", "/login")):
            response = RedirectResponse("/", status_code=303) if logged_in else self.login_page()
        elif not logged_in:
            response = self.refusal(scope)
        elif route == ("GET", "/api/session"):
            response = Response(status_code=204)  # Tells the dashboard to offer logging out.
        else:
            await self.app(scope, receive, send)
            return
        await response(scope, receive, send)

    @staticmethod
    def refusal(scope) -> Response:
        if scope.get("method") in ("GET", "HEAD") and not scope["path"].startswith("/api/"):
            return RedirectResponse("/login", status_code=303)
        return JSONResponse({"detail": "Login required"}, status_code=401)
