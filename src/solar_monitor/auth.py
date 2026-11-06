"""A login page and session cookie in front of the whole app.

This wraps the app rather than its routes, so nothing can be left unprotected by accident,
including routes added later. Sessions are kept in memory: restarting the app logs everyone out,
and logging out really does end the session rather than just forgetting the cookie.
"""

from __future__ import annotations

import secrets
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
    def __init__(self, app, username: str, password: str) -> None:
        self.app = app
        self.username = username.encode()
        self.password = password.encode()
        self.sessions: dict[str, float] = {}  # Token to expiry time.
        self.page = PAGE.read_text(encoding="utf-8")

    def logged_in(self, token: str | None) -> bool:
        return self.sessions.get(token, 0) > time.time()

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
