"""A login page and session cookie in front of the whole app.

This wraps the app rather than its routes, so nothing can be left unprotected by accident,
including routes added later. The app keeps the list of sessions, so logging out really does end
the session rather than just forgetting the cookie. Given a file, it saves the list there, so a
restart or reboot keeps everyone logged in.

The file holds only a keyed hash of each session, so reading it isn't enough to log in. The key is
the username and password, so changing either one in .env logs everyone out.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs

from starlette.requests import HTTPConnection, Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

COOKIE = "solar_monitor_session"
SESSION_SECONDS = 30 * 24 * 60 * 60
MAX_FORM_BYTES = 4096
PAGE = Path(__file__).with_name("login.html")
ERROR = '<p class="error" role="alert">Wrong username or password.</p>'


class LoginRequired:
    def __init__(self, app, username: str, password: str, sessions_file: Path | None = None) -> None:
        self.app = app
        self.username = username.encode()
        self.password = password.encode()
        self.key = self.username + b"\0" + self.password  # Settings can't hold a \0, so no other login has this key.
        self.sessions_file = sessions_file
        self.sessions: dict[str, float] = self.load()  # Hash of the token to expiry time.
        self.page = PAGE.read_text(encoding="utf-8")

    def hash(self, token: str) -> str:
        return hmac.new(self.key, token.encode(), hashlib.sha256).hexdigest()

    def logged_in(self, token: str | None) -> bool:
        return token is not None and self.sessions.get(self.hash(token), 0) > time.time()

    def load(self) -> dict[str, float]:
        """The saved sessions that haven't expired. A missing or damaged file just means logging in again."""
        if self.sessions_file is None:
            return {}
        try:
            saved = json.loads(self.sessions_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as problem:
            print(f"Couldn't read the saved logins, so everyone will log in again: {problem}", file=sys.stderr)
            return {}
        if not isinstance(saved, dict):
            return {}
        now = time.time()
        return {key: expiry for key, expiry in saved.items() if isinstance(expiry, (int, float)) and expiry > now}

    def save(self) -> None:
        if self.sessions_file is None:
            return
        temporary = self.sessions_file.with_name(self.sessions_file.name + ".tmp")
        try:
            # Readable by this app's user only, and swapped in whole, so a crash or power cut
            # leaves either the old list or the new one.
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with open(descriptor, "w", encoding="utf-8") as file:
                json.dump(self.sessions, file)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.sessions_file)
        except OSError as problem:
            print(f"Couldn't save the logins, so a restart will log everyone out: {problem}", file=sys.stderr)

    def correct(self, username: str, password: str) -> bool:
        # Both are always compared, so the response time doesn't reveal which one was wrong.
        return secrets.compare_digest(username.encode(), self.username) & secrets.compare_digest(
            password.encode(), self.password
        )

    def login_page(self, error: bool = False) -> Response:
        html = self.page.replace("<!--error-->", ERROR if error else "")
        return HTMLResponse(html, status_code=401 if error else 200, headers={"Cache-Control": "no-store"})

    async def log_in(self, request: Request) -> Response:
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_FORM_BYTES:
                return self.login_page(error=True)
        form = parse_qs(body.decode(errors="replace"))
        if not self.correct(form.get("username", [""])[0], form.get("password", [""])[0]):
            return self.login_page(error=True)

        now = time.time()
        self.sessions = {key: expiry for key, expiry in self.sessions.items() if expiry > now}
        token = secrets.token_urlsafe(32)
        self.sessions[self.hash(token)] = now + SESSION_SECONDS
        self.save()
        response = RedirectResponse("/", status_code=303)
        # SameSite stops other sites from sending requests that carry the login.
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True, samesite="lax")
        return response

    def log_out(self, token: str | None) -> Response:
        if token is not None and self.sessions.pop(self.hash(token), None) is not None:
            self.save()
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
