"""Shared by the tests of accounts: accounts to log in as, the app as main.py builds it, and a
browser that keeps its session."""

import asyncio
from http.cookies import SimpleCookie

import httpx

from solar_monitor import auth, db
from solar_monitor.api import create_app
from solar_monitor.auth import COOKIE, LoginRequired
from solar_monitor.collector import Collector

PASSWORD = "correct horse+é&="


def add_account(conn, name, admin=False, permissions=(), password=PASSWORD):
    return db.add_user(conn, name, auth.hash_password(password), admin, permissions)


def real_app(conn, open_without_accounts=False):
    """The app as main.py builds it. Nothing runs its lifespan, so the collector never starts."""
    app = create_app(Collector("test", 0, conn))
    app.add_middleware(LoginRequired, conn=conn, open_without_accounts=open_without_accounts)
    return app


class Browser:
    """One browser at one address, keeping its session cookie. Requests run in this thread, as
    the database connection needs, so it can't use TestClient."""

    def __init__(self, app, address="192.0.2.1", base_url="http://solar.lan:8000", token=None):
        self.app = app
        self.address = address
        self.base_url = base_url
        self.token = token

    def request(self, method, path, headers=None, **kwargs):
        headers = dict(headers or {})
        if self.token:
            headers.setdefault("Cookie", f"{COOKIE}={self.token}")

        async def send():
            transport = httpx.ASGITransport(app=self.app, client=(self.address, 50000))
            async with httpx.AsyncClient(transport=transport, base_url=self.base_url) as client:
                return await client.request(method, path, headers=headers, **kwargs)

        response = asyncio.run(send())
        for header in response.headers.get_list("set-cookie"):
            cookie = SimpleCookie(header)
            if COOKIE in cookie:
                morsel = cookie[COOKIE]
                self.token = None if morsel["max-age"] == "0" or not morsel.value else morsel.value
        return response

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)

    def put(self, path, **kwargs):
        return self.request("PUT", path, **kwargs)

    def delete(self, path, **kwargs):
        return self.request("DELETE", path, **kwargs)

    def log_in(self, name, password=PASSWORD):
        response = self.post("/login", data={"username": name, "password": password})
        assert response.status_code == 303, response.text
        assert self.token
        return self

    def elsewhere(self):
        """Another browser on the same app, with none of this one's session."""
        return Browser(self.app, self.address, self.base_url)

    def with_token(self, token):
        return Browser(self.app, self.address, self.base_url, token)
