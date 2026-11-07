import asyncio
import re
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from solar_monitor import auth, config, db, main
from solar_monitor.api import create_app
from solar_monitor.auth import COOKIE, LoginRequired
from solar_monitor.collector import Collector
from solar_monitor.password import hash_password

USER = "someone"
PASSWORD = "correct horse+é&="
RIGHT = {"username": USER, "password": PASSWORD}


@pytest.fixture
def client():
    app = FastAPI()

    @app.get("/")
    def page():
        return "the page"

    @app.get("/api/data")
    def data():
        return "the data"

    app.add_middleware(LoginRequired, username=USER, password=PASSWORD)
    return TestClient(app, follow_redirects=False)


def log_in(client):
    response = client.post("/login", data=RIGHT)
    assert response.status_code == 303
    return response


def test_a_page_without_a_login_goes_to_the_login_page(client):
    response = client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_the_api_without_a_login_is_refused_with_no_content(client):
    response = client.get("/api/data")
    assert response.status_code == 401
    assert "the data" not in response.text


def test_the_browsers_own_login_dialog_is_never_triggered(client):
    for response in (client.get("/"), client.get("/api/data"), client.post("/login", data={})):
        assert "www-authenticate" not in response.headers


def test_the_login_page_has_a_form_and_no_error(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert '<form method="post" action="/login">' in response.text
    assert "<!--error-->" not in response.text
    assert auth.ERROR not in response.text


def test_the_right_login_gets_in(client):
    response = log_in(client)
    assert response.headers["location"] == "/"
    assert client.get("/").text == '"the page"'
    assert client.get("/api/data").text == '"the data"'
    assert client.get("/api/session").status_code == 204


def test_the_session_cookie_is_out_of_reach_of_scripts_and_other_sites(client):
    cookie = log_in(client).headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie


def test_logged_in_the_login_page_goes_to_the_dashboard(client):
    log_in(client)
    response = client.get("/login")
    assert response.status_code == 303
    assert response.headers["location"] == "/"


@pytest.mark.parametrize(
    "form",
    [
        {"username": USER, "password": "wrong"},
        {"username": "wrong", "password": PASSWORD},
        {"username": USER, "password": PASSWORD + "x"},
        {"username": USER, "password": ""},
        {"username": USER},
        {"password": PASSWORD},
        {},
        {"username": USER, "password": "x" * (auth.MAX_FORM_BYTES + 1)},
    ],
)
def test_anything_else_shows_the_error_and_gives_no_session(client, form):
    response = client.post("/login", data=form)
    assert response.status_code == 401
    assert auth.ERROR in response.text
    assert COOKIE not in client.cookies
    assert client.get("/api/data").status_code == 401


def test_a_made_up_cookie_is_refused(client):
    client.cookies.set(COOKIE, "made-up")
    assert client.get("/api/data").status_code == 401


def test_a_session_ends_when_it_expires(client, monkeypatch):
    log_in(client)
    now = auth.time.time()
    monkeypatch.setattr(auth.time, "time", lambda: now + auth.SESSION_SECONDS - 1)
    assert client.get("/api/data").status_code == 200
    monkeypatch.setattr(auth.time, "time", lambda: now + auth.SESSION_SECONDS + 1)
    assert client.get("/api/data").status_code == 401


def test_logging_out_ends_the_session_even_if_the_cookie_is_kept(client):
    log_in(client)
    token = client.cookies[COOKIE]
    response = client.post("/logout")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert COOKIE not in client.cookies

    client.cookies.set(COOKIE, token)
    assert client.get("/api/data").status_code == 401


def test_following_a_link_to_logout_does_not_log_out(client):
    log_in(client)
    client.get("/logout")
    assert client.get("/api/data").status_code == 200


def test_every_route_in_the_real_app_is_protected(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    app = create_app(Collector("host", 502, conn))
    routes = [route for route in app.routes if isinstance(route, APIRoute)]
    assert routes
    app.add_middleware(LoginRequired, username=USER, password=PASSWORD)
    # Not as a context manager, so the collector doesn't start.
    client = TestClient(app, follow_redirects=False)
    for route in routes:
        path = re.sub(r"{[^}]+}", "1", route.path)
        for method in route.methods:
            assert client.request(method, path).status_code in (303, 401), f"{method} {path}"
    assert client.get("/no-such-page").status_code == 303
    assert client.get("/api/no-such-route").status_code == 401
    conn.close()


def test_the_collector_still_starts_and_stops_behind_the_login():
    events = []

    @asynccontextmanager
    async def lifespan(_):
        events.append("started")
        yield
        events.append("stopped")

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(LoginRequired, username=USER, password=PASSWORD)
    with TestClient(app):
        pass
    assert events == ["started", "stopped"]


def test_a_session_lasts_a_week():
    assert auth.SESSION_SECONDS == 7 * 24 * 60 * 60


def test_the_cookie_lasts_as_long_as_the_session(client):
    assert f"max-age={auth.SESSION_SECONDS}" in log_in(client).headers["set-cookie"].lower()


def protected_by(stored_password):
    app = FastAPI()

    @app.get("/api/data")
    def data():
        return "the data"

    app.add_middleware(LoginRequired, username=USER, password=stored_password)
    return TestClient(app, follow_redirects=False)


def test_a_hashed_password_lets_the_right_one_in():
    client = protected_by(hash_password(PASSWORD, n=16, r=1, p=1))
    assert client.post("/login", data={"username": USER, "password": "wrong"}).status_code == 401
    assert client.post("/login", data={"username": USER, "password": ""}).status_code == 401
    log_in(client)
    assert client.get("/api/data").status_code == 200


def test_the_hash_itself_is_not_the_password():
    stored = hash_password(PASSWORD, n=16, r=1, p=1)
    client = protected_by(stored)
    assert client.post("/login", data={"username": USER, "password": stored}).status_code == 401


def wrong(client):
    return client.post("/login", data={"username": USER, "password": "a guess"})


@pytest.fixture
def clock(monkeypatch):
    """Stands still until moved, so times are exact."""
    now = [auth.time.time()]
    monkeypatch.setattr(auth.time, "time", lambda: now[0])
    return now


def test_five_wrong_logins_lock_everyone_out_even_with_the_right_one(client):
    for attempt in range(1, auth.LOCK_AFTER):
        assert wrong(client).status_code == 401, attempt
    locking = wrong(client)
    assert locking.status_code == 429
    assert locking.headers["retry-after"] == str(auth.LOCK_SECONDS)
    assert "Try again in 15 minutes" in locking.text

    right = client.post("/login", data=RIGHT)
    assert right.status_code == 429
    assert COOKIE not in client.cookies
    assert client.get("/api/data").status_code == 401


def test_the_lock_lifts_after_fifteen_minutes(client, clock):
    for _ in range(auth.LOCK_AFTER):
        wrong(client)
    clock[0] += auth.LOCK_SECONDS - 60
    response = client.post("/login", data=RIGHT)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"
    assert "Try again in 1 minute." in response.text
    clock[0] += 60
    # The count starts again, rather than locking at the next mistake.
    assert wrong(client).status_code == 401
    log_in(client)


def test_a_right_login_clears_the_count(client):
    for _ in range(auth.LOCK_AFTER - 1):
        wrong(client)
    log_in(client)
    for _ in range(auth.LOCK_AFTER - 1):
        assert wrong(client).status_code == 401


def test_old_mistakes_are_forgotten(client, clock):
    for _ in range(auth.LOCK_AFTER - 1):
        wrong(client)
    clock[0] += auth.LOCK_SECONDS
    assert wrong(client).status_code == 401
    clock[0] += 1
    for _ in range(auth.LOCK_AFTER - 2):
        assert wrong(client).status_code == 401
    assert wrong(client).status_code == 429


def test_a_burst_of_guesses_at_once_gets_no_more_than_five_checked():
    async def page(scope, receive, send):
        await JSONResponse("the data")(scope, receive, send)

    login = LoginRequired(page, username=USER, password=PASSWORD)
    checked = []
    real_check = login.correct
    login.correct = lambda *form: checked.append(form) or real_check(*form)

    async def burst():
        transport = httpx.ASGITransport(app=login)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            guesses = [client.post("/login", data={"username": USER, "password": f"guess {n}"}) for n in range(30)]
            return await asyncio.gather(*guesses)

    statuses = [response.status_code for response in asyncio.run(burst())]
    assert len(checked) == auth.LOCK_AFTER
    assert statuses.count(401) == auth.LOCK_AFTER - 1
    assert statuses.count(429) == 30 - (auth.LOCK_AFTER - 1)


def test_every_login_is_logged_with_its_address_but_not_the_username(client, capsys):
    wrong(client)
    log_in(client)
    for _ in range(auth.LOCK_AFTER):
        wrong(client)
    client.post("/login", data=RIGHT)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == auth.LOCK_AFTER + 3
    assert all("Login from testclient" in line for line in lines)
    assert "failed: wrong username or password (1 of 5)" in lines[0]
    assert "succeeded" in lines[1]
    assert "all logins are locked for 15 minutes" in lines[-2]
    assert "refused: logins are locked" in lines[-1]
    assert not any(USER in line or "guess" in line for line in lines)


@pytest.fixture
def settings(monkeypatch):
    """main() with nothing from the real .env, stopping just before it would poll."""
    monkeypatch.setattr(config, "load_dotenv", lambda *_: None)
    monkeypatch.setattr(main.sys, "argv", ["solar-monitor"])
    for name in ("BIND_HOST", "DASHBOARD_USER", "DASHBOARD_PASSWORD"):
        monkeypatch.delenv(name, raising=False)

    def stop(*_):
        raise AssertionError("got past the login check")

    monkeypatch.setattr(main, "resolve_target", stop)
    return monkeypatch


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.10", "pi.example"])
def test_the_app_wont_open_to_the_network_without_a_login(settings, capsys, host):
    settings.setenv("BIND_HOST", host)
    assert main.main() == 1
    assert "DASHBOARD_PASSWORD" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["DASHBOARD_USER", "DASHBOARD_PASSWORD"])
def test_the_app_wont_start_with_half_a_login(settings, capsys, name):
    settings.setenv(name, "value")
    assert main.main() == 1
    assert "both" in capsys.readouterr().err


def test_a_damaged_hash_stops_the_app(settings, capsys):
    settings.setenv("DASHBOARD_USER", USER)
    settings.setenv("DASHBOARD_PASSWORD", "scrypt:16:1:1:00")
    assert main.main() == 1
    assert "python -m solar_monitor.password" in capsys.readouterr().err


def test_a_plain_password_still_works_but_is_warned_about(settings, capsys):
    settings.setenv("DASHBOARD_USER", USER)
    settings.setenv("DASHBOARD_PASSWORD", PASSWORD)
    with pytest.raises(AssertionError, match="got past the login check"):
        main.main()
    assert "python -m solar_monitor.password" in capsys.readouterr().err


def test_a_hashed_password_is_not_warned_about(settings, capsys):
    settings.setenv("DASHBOARD_USER", USER)
    settings.setenv("DASHBOARD_PASSWORD", hash_password(PASSWORD, n=16, r=1, p=1))
    with pytest.raises(AssertionError, match="got past the login check"):
        main.main()
    assert capsys.readouterr().err == ""


def test_only_this_machine_is_believed_about_who_is_asking(settings, tmp_path):
    served = []

    class Server:
        started = True

        def __init__(self, server_config):
            served.append(server_config)

        def run(self):
            pass

    settings.setattr(main, "resolve_target", lambda *_: ("test", 0))
    settings.setattr(main, "db_path", lambda: tmp_path / "test.db")
    settings.setattr(main, "on_stop_request", lambda *_: None)
    settings.setattr(main, "stop_when_window_closes", lambda *_: None)
    settings.setattr(main.uvicorn, "Server", Server)
    assert main.main() == 0
    assert served[0].proxy_headers is True
    assert served[0].forwarded_allow_ips == ["127.0.0.1", "::1"]
