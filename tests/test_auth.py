import json
import re
import stat
import sys
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from solar_monitor import auth, config, db, main
from solar_monitor.api import create_app
from solar_monitor.auth import COOKIE, LoginRequired
from solar_monitor.collector import Collector

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


def saving_to(sessions_file, password=PASSWORD):
    """A fresh app, as after a restart, that keeps its sessions in sessions_file."""
    app = FastAPI()

    @app.get("/api/data")
    def data():
        return "the data"

    app.add_middleware(LoginRequired, username=USER, password=password, sessions_file=sessions_file)
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def sessions_file(tmp_path):
    return tmp_path / "sessions.json"


def logged_in_token(sessions_file):
    client = saving_to(sessions_file)
    log_in(client)
    return client.cookies[COOKIE]


def with_cookie(client, token):
    client.cookies.set(COOKIE, token)
    return client


def test_a_login_survives_a_restart(sessions_file):
    token = logged_in_token(sessions_file)
    assert with_cookie(saving_to(sessions_file), token).get("/api/data").status_code == 200


def test_the_file_holds_no_token_that_could_be_used_to_log_in(sessions_file):
    token = logged_in_token(sessions_file)
    saved = sessions_file.read_text(encoding="utf-8")
    assert token not in saved
    for key in json.loads(saved):
        assert with_cookie(saving_to(sessions_file), key).get("/api/data").status_code == 401


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no Unix file modes")
def test_only_the_apps_own_user_can_read_the_file(sessions_file):
    logged_in_token(sessions_file)
    assert stat.S_IMODE(sessions_file.stat().st_mode) == 0o600


def test_logging_out_is_saved_too(sessions_file):
    token = logged_in_token(sessions_file)
    with_cookie(saving_to(sessions_file), token).post("/logout")
    assert json.loads(sessions_file.read_text(encoding="utf-8")) == {}
    assert with_cookie(saving_to(sessions_file), token).get("/api/data").status_code == 401


def test_changing_the_password_logs_everyone_out(sessions_file):
    token = logged_in_token(sessions_file)
    assert with_cookie(saving_to(sessions_file, "new password"), token).get("/api/data").status_code == 401


def test_sessions_that_expired_while_the_app_was_stopped_are_dropped(sessions_file, monkeypatch):
    token = logged_in_token(sessions_file)
    now = auth.time.time()
    monkeypatch.setattr(auth.time, "time", lambda: now + auth.SESSION_SECONDS + 1)
    client = saving_to(sessions_file)
    assert with_cookie(client, token).get("/api/data").status_code == 401
    log_in(client)
    assert len(json.loads(sessions_file.read_text(encoding="utf-8"))) == 1


@pytest.mark.parametrize("damage", ["", "not json", "[]", '{"key": "not a time"}', '{"key": null}'])
def test_a_damaged_file_means_logging_in_again_not_a_crash(sessions_file, damage):
    sessions_file.write_text(damage, encoding="utf-8")
    client = saving_to(sessions_file)
    assert with_cookie(client, "key").get("/api/data").status_code == 401
    client.cookies.clear()
    log_in(client)
    assert client.get("/api/data").status_code == 200
    assert len(json.loads(sessions_file.read_text(encoding="utf-8"))) == 1


def test_a_file_that_cant_be_saved_still_lets_you_log_in(tmp_path, capsys):
    client = saving_to(tmp_path / "no-such-folder" / "sessions.json")
    log_in(client)
    assert client.get("/api/data").status_code == 200
    assert "restart will log everyone out" in capsys.readouterr().err


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
