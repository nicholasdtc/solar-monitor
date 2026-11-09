"""Logging in, sessions, and what each account may reach."""

import re
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from logins import PASSWORD, Browser, add_account, real_app

from solar_monitor import auth, db
from solar_monitor.auth import COOKIE, LoginRequired, Throttle


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    yield conn
    conn.close()


@pytest.fixture
def owner(conn):
    return add_account(conn, auth.OWNER, admin=True)


@pytest.fixture
def browser(conn, owner):
    add_account(conn, "someone", permissions=["statistics"])
    return Browser(real_app(conn))


def history(conn, kind=None):
    return [(row["username"], row["summary"]) for row in db.list_history(conn, kind=kind)]


# Logging in


def test_a_page_without_a_login_goes_to_the_login_page(browser):
    response = browser.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_the_api_without_a_login_is_refused_with_no_content(browser):
    response = browser.get("/api/latest")
    assert response.status_code == 401
    assert response.json() == {"detail": "Login required"}


def test_the_browsers_own_login_dialog_is_never_triggered(browser):
    for response in (browser.get("/"), browser.get("/api/latest"), browser.post("/login", data={})):
        assert "www-authenticate" not in response.headers


def test_the_login_page_has_a_form_and_no_error(browser):
    response = browser.get("/login")
    assert response.status_code == 200
    assert '<form method="post" action="/login">' in response.text
    assert "<!--error-->" not in response.text
    assert auth.WRONG not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_the_right_login_gets_in(browser):
    response = browser.post("/login", data={"username": "someone", "password": PASSWORD})
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert browser.get("/").status_code == 200
    assert browser.get("/api/latest").status_code == 200
    assert browser.get("/api/session").json()["user"] == {
        "name": "someone",
        "admin": False,
        "owner": False,
        "local": False,
        "permissions": ["statistics"],
    }


@pytest.mark.parametrize("typed", ["SomeOne", "  someone ", "SOMEONE"])
def test_a_username_is_the_same_whatever_its_case(browser, typed):
    browser.log_in(typed)
    assert browser.get("/api/session").json()["user"]["name"] == "someone"


def test_the_session_cookie_is_out_of_reach_of_scripts_and_other_sites(browser):
    response = browser.post("/login", data={"username": "someone", "password": PASSWORD})
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie


def test_logged_in_the_login_page_goes_to_the_dashboard(browser):
    browser.log_in("someone")
    response = browser.get("/login")
    assert response.status_code == 303
    assert response.headers["location"] == "/"


@pytest.mark.parametrize(
    "form",
    [
        {"username": "someone", "password": "wrong"},
        {"username": "nobody", "password": PASSWORD},
        {"username": "someone", "password": PASSWORD + "x"},
        {"username": "someone", "password": PASSWORD.upper()},
        {"username": "someone", "password": ""},
        {"username": "someone"},
        {"password": PASSWORD},
        {},
        {"username": "some one", "password": PASSWORD},
        {"username": "someone", "password": "x" * (auth.MAX_FORM_BYTES + 1)},
    ],
)
def test_anything_else_shows_the_error_and_gives_no_session(browser, form):
    response = browser.post("/login", data=form)
    assert response.status_code == 401
    assert auth.WRONG in response.text
    assert browser.token is None
    assert browser.get("/api/latest").status_code == 401


def test_a_made_up_cookie_is_refused(browser):
    assert browser.with_token("made-up").get("/api/latest").status_code == 401


def test_a_session_ends_when_it_expires(browser, monkeypatch):
    browser.log_in("someone")
    now = auth.time.time()
    monkeypatch.setattr(auth.time, "time", lambda: now + auth.SESSION_SECONDS - 1)
    assert browser.get("/api/latest").status_code == 200
    monkeypatch.setattr(auth.time, "time", lambda: now + auth.SESSION_SECONDS + 1)
    assert browser.get("/api/latest").status_code == 401


def test_the_database_holds_no_token_that_could_be_used_to_log_in(browser, conn):
    browser.log_in("someone")
    kept = [row[0] for row in conn.execute("SELECT token_hash FROM sessions")]
    assert kept == [auth.token_hash(browser.token)]
    assert browser.token not in kept
    assert browser.with_token(kept[0]).get("/api/latest").status_code == 401


def test_a_login_survives_a_restart(browser, conn):
    browser.log_in("someone")
    restarted = Browser(real_app(conn), token=browser.token)
    assert restarted.get("/api/latest").status_code == 200


def test_a_login_is_recorded_with_the_address(browser, conn):
    browser.log_in("someone")
    assert history(conn, "login") == [("someone", "Logged in from 192.0.2.1")]
    assert db.get_user(conn, db.find_user(conn, "someone")["id"])["last_login"] is not None


def test_a_wrong_password_is_recorded_under_the_account(browser, conn):
    browser.post("/login", data={"username": "someone", "password": "wrong password"})
    assert history(conn, "login") == [("someone", "Wrong password, from 192.0.2.1")]


def test_an_unknown_name_is_recorded_without_it(browser, conn):
    # It may be a password, typed in the wrong box.
    browser.post("/login", data={"username": "my-secret-pw", "password": "wrong password"})
    assert history(conn, "login") == [("", "Log-in with an unknown name, from 192.0.2.1")]
    assert "my-secret-pw" not in repr(conn.execute("SELECT * FROM actions").fetchall())


def test_nothing_typed_as_a_password_is_kept(browser, conn):
    browser.post("/login", data={"username": "someone", "password": "my typo'd password"})
    browser.log_in("someone")
    for table in ("actions", "sessions", "users"):
        assert "typo" not in repr(conn.execute(f"SELECT * FROM {table}").fetchall())
        assert PASSWORD not in repr(conn.execute(f"SELECT * FROM {table}").fetchall())


# Throttling


def test_wrong_passwords_make_an_address_wait_even_for_the_right_one(browser):
    for _ in range(Throttle.FREE_TRIES):
        assert browser.post("/login", data={"username": "someone", "password": "wrong"}).status_code == 401
    response = browser.post("/login", data={"username": "someone", "password": PASSWORD})
    assert response.status_code == 429
    assert response.headers["retry-after"] == "1"
    assert "Wait 1 second, then try again." in response.text
    assert browser.token is None


def test_waiting_at_one_address_doesnt_hold_up_another(browser):
    for _ in range(Throttle.FREE_TRIES):
        browser.post("/login", data={"username": "someone", "password": "wrong"})
    Browser(browser.app, address="192.0.2.2").log_in("someone")


def test_after_the_wait_the_right_password_gets_in_and_clears_the_count(browser, monkeypatch):
    for _ in range(Throttle.FREE_TRIES):
        browser.post("/login", data={"username": "someone", "password": "wrong"})
    now = auth.time.time()
    monkeypatch.setattr(auth.time, "time", lambda: now + 2)
    browser.log_in("someone")
    browser.post("/logout")
    assert browser.post("/login", data={"username": "someone", "password": "wrong"}).status_code == 401


def test_each_wrong_try_after_the_free_ones_doubles_the_wait():
    throttle = Throttle()
    waits = []
    for count in range(Throttle.FREE_TRIES + 4):
        waits.append(throttle.wait("a", now=1000.0))
        throttle.failed("a", now=1000.0)
    assert waits == [0, 0, 0, 0, 0, 1, 2, 4, 8]


def test_the_wait_has_a_limit():
    throttle = Throttle()
    for _ in range(Throttle.FREE_TRIES + 30):
        throttle.failed("a", now=1000.0)
    assert throttle.wait("a", now=1000.0) == Throttle.LONGEST_WAIT


def test_wrong_tries_are_forgotten_after_a_day():
    throttle = Throttle()
    for _ in range(Throttle.FREE_TRIES + 3):
        throttle.failed("a", now=1000.0)
    throttle.failed("a", now=1000.0 + Throttle.FORGET_AFTER)
    assert throttle.wait("a", now=1000.0 + Throttle.FORGET_AFTER) == 0


def test_the_throttle_keeps_only_so_many_addresses():
    throttle = Throttle()
    throttle.MOST_ADDRESSES = 3
    for number in range(10):
        throttle.failed(f"address {number}", now=1000.0 + number)
    assert len(throttle.failures) <= 3
    assert "address 9" in throttle.failures


# Logging out


def test_logging_out_ends_the_session_even_if_the_cookie_is_kept(browser):
    browser.log_in("someone")
    token = browser.token
    response = browser.post("/logout")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert browser.token is None
    assert browser.with_token(token).get("/api/latest").status_code == 401


def two_sessions(browser):
    """Log in twice, as from two devices."""
    other = browser.elsewhere().log_in("someone")
    browser.log_in("someone")
    return other


def test_logging_out_leaves_other_devices_logged_in(browser):
    other = two_sessions(browser)
    browser.post("/logout")
    assert other.get("/api/latest").status_code == 200


def test_logging_out_everywhere_ends_every_session(browser, conn):
    other = two_sessions(browser)
    token = browser.token
    response = browser.post("/logout", data={"everywhere": "1"})
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert browser.with_token(token).get("/api/latest").status_code == 401
    assert other.get("/api/latest").status_code == 401
    assert history(conn, "login")[0] == ("someone", "Logged out everywhere")


def test_logging_out_everywhere_leaves_other_accounts_logged_in(browser):
    owner = browser.elsewhere().log_in(auth.OWNER)
    browser.log_in("someone")
    browser.post("/logout", data={"everywhere": "1"})
    assert owner.get("/api/latest").status_code == 200


def test_only_a_logged_in_session_can_log_everyone_out(browser):
    browser.log_in("someone")
    browser.elsewhere().post("/logout", data={"everywhere": "1"})
    browser.with_token("made-up").post("/logout", data={"everywhere": "1"})
    assert browser.get("/api/latest").status_code == 200


def test_an_oversized_logout_form_only_logs_this_session_out(browser):
    other = two_sessions(browser)
    browser.post("/logout", data={"everywhere": "1", "padding": "x" * auth.MAX_FORM_BYTES})
    assert browser.token is None
    assert other.get("/api/latest").status_code == 200


def test_following_a_link_to_logout_does_not_log_out(browser):
    browser.log_in("someone")
    browser.get("/logout")
    assert browser.get("/api/latest").status_code == 200


# What each account may reach


def test_a_change_to_an_account_applies_to_its_next_request(browser, conn):
    browser.log_in("someone")
    assert browser.get("/api/actions").status_code == 403
    db.update_user(conn, db.find_user(conn, "someone")["id"], False, ["history"])
    assert browser.get("/api/actions").status_code == 200
    db.update_user(conn, db.find_user(conn, "someone")["id"], False, [])
    assert browser.get("/api/actions").status_code == 403


def test_removing_an_account_ends_its_sessions(browser, conn):
    browser.log_in("someone")
    db.delete_user(conn, db.find_user(conn, "someone")["id"])
    assert browser.get("/api/latest").status_code == 401


def test_the_owner_is_an_admin_whatever_the_database_says(browser, conn, owner):
    db.update_user(conn, owner, False, [])
    browser.log_in(auth.OWNER)
    assert browser.get("/api/users").status_code == 200
    assert browser.get("/api/session").json()["user"]["admin"] is True


TAB_ROUTES = [
    ("GET", "/api/meter", "grid-meter"),
    ("GET", "/api/appliances", "appliances"),
    ("GET", "/api/tou/writes", "schedule"),
    ("GET", "/api/inverter-settings/writes", "schedule"),
    ("GET", "/api/export", "app-data"),
    ("GET", "/api/actions", "history"),
]


@pytest.mark.parametrize(("method", "path", "permission"), TAB_ROUTES)
def test_a_tab_needs_its_permission(conn, owner, method, path, permission):
    add_account(conn, "without", permissions=[p for p in auth.PERMISSIONS if p != permission and p != "inverter"])
    add_account(conn, "with", permissions=[permission])
    app = real_app(conn)
    refused = Browser(app).log_in("without").request(method, path)
    assert refused.status_code == 403
    assert "An admin can change that under Users" in refused.json()["detail"]
    assert refused.headers["x-not-allowed"] == "1"  # The page offers a reload.
    assert Browser(app).log_in("with").request(method, path).status_code == 200



@pytest.mark.parametrize("path", ["/api/weather", "/api/weather/providers", "/api/weather/history?start=0&end=3600"])
def test_the_weather_needs_no_permission(conn, owner, path):
    add_account(conn, "nobody")
    assert Browser(real_app(conn)).log_in("nobody").get(path).status_code == 200


def test_a_routes_own_refusal_isnt_marked_as_a_missing_permission(conn, owner):
    add_account(conn, "someone")
    browser = Browser(real_app(conn)).log_in("someone")
    refused = browser.put("/api/password", json={"current": "not the password", "new": "a new long password"})
    assert refused.status_code == 403
    assert "x-not-allowed" not in refused.headers


def test_write_to_inverter_includes_the_battery_schedule(conn, owner):
    add_account(conn, "writer", permissions=["inverter"])
    browser = Browser(real_app(conn)).log_in("writer")
    assert browser.get("/api/tou/writes").status_code == 200
    assert "schedule" in browser.get("/api/session").json()["user"]["permissions"]


EDITS = [
    ("POST", "/api/appliances", {"name": "Kettle", "typical_w": 2000}),
    ("PUT", "/api/appliances/1", {"name": "Kettle", "typical_w": 2000}),
    ("DELETE", "/api/appliances/1", None),
    ("PUT", "/api/settings", {"limit_amps": 20}),
]


@pytest.mark.parametrize(("method", "path", "body"), EDITS)
def test_changing_appliances_and_tariffs_needs_edit_data(conn, owner, method, path, body):
    add_account(conn, "reader", permissions=["appliances"])
    add_account(conn, "editor", permissions=["appliances", "edit"])
    app = real_app(conn)
    kwargs = {} if body is None else {"json": body}
    assert Browser(app).log_in("reader").request(method, path, **kwargs).status_code == 403
    assert Browser(app).log_in("editor").request(method, path, **kwargs).status_code not in (401, 403)


def test_edit_data_alone_doesnt_open_the_appliances_tab(conn, owner):
    add_account(conn, "editor", permissions=["edit"])
    browser = Browser(real_app(conn)).log_in("editor")
    assert browser.get("/api/appliances").status_code == 403
    assert browser.post("/api/appliances", json={"name": "Kettle", "typical_w": 2000}).status_code == 403


def test_everyone_logged_in_has_the_dashboard(conn, owner):
    add_account(conn, "nothing")
    browser = Browser(real_app(conn)).log_in("nothing")
    for path in ("/", "/api/latest", "/api/session", "/api/prefs", "/api/about", "/api/daily"):
        assert browser.get(path).status_code == 200, path
    assert browser.get("/api/history?start=0&end=1").status_code == 200


ADMIN_ROUTES = {
    ("GET", "/api/users"),
    ("POST", "/api/users"),
    ("PUT", "/api/users/{user_id}"),
    ("DELETE", "/api/users/{user_id}"),
    ("PUT", "/api/users/{user_id}/password"),
    ("DELETE", "/api/users/{user_id}/sessions"),
}


def test_every_route_is_given_to_a_permission_or_kept_for_admins(conn):
    # A route added later lands here, as admins only, until it's listed in auth.ROUTES.
    app = real_app(conn)
    admin_only = set()
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in route.methods:
                path = re.sub(r"{[^}]+}", "1", route.path)
                if auth.needs(method, path) == auth.ADMIN_ONLY:
                    admin_only.add((method, route.path))
    assert admin_only == ADMIN_ROUTES


def test_a_route_that_isnt_listed_is_for_admins_only(conn, owner):
    app = FastAPI()

    @app.get("/api/added-later")
    def added_later():
        return "for admins"

    app.add_middleware(LoginRequired, conn=conn, open_without_accounts=False)
    add_account(conn, "everything", permissions=list(auth.PERMISSIONS))
    assert Browser(app).log_in("everything").get("/api/added-later").status_code == 403
    assert Browser(app).log_in(auth.OWNER).get("/api/added-later").status_code == 200


@pytest.mark.parametrize(
    ("query", "applies"),
    [
        (b"", False),
        (b"apply=false", False),
        (b"apply=0", False),
        (b"apply=no", False),
        (b"apply=Off", False),
        (b"apply=true", True),
        (b"apply=1", True),
        (b"apply=", True),
        (b"apply=maybe", True),
        (b"apply=false&apply=true", True),
    ],
)
def test_applying_an_import_needs_edit_data_unless_plainly_not_applied(query, applies):
    assert ("edit" in auth.needs("POST", "/api/import", query)) == applies


def test_every_route_in_the_real_app_is_protected(conn, owner):
    app = real_app(conn)
    routes = [route for route in app.routes if isinstance(route, APIRoute)]
    assert routes
    browser = Browser(app)
    for route in routes:
        path = re.sub(r"{[^}]+}", "1", route.path)
        for method in route.methods:
            assert browser.request(method, path).status_code in (303, 401), f"{method} {path}"
    assert browser.get("/no-such-page").status_code == 303
    assert browser.get("/api/no-such-route").status_code == 401


def test_the_collector_still_starts_and_stops_behind_the_login(conn):
    events = []

    @asynccontextmanager
    async def lifespan(_):
        events.append("started")
        yield
        events.append("stopped")

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(LoginRequired, conn=conn, open_without_accounts=False)
    with TestClient(app):
        pass
    assert events == ["started", "stopped"]


# Without accounts


def test_without_accounts_this_machine_uses_the_app_without_logging_in(conn):
    browser = Browser(real_app(conn, open_without_accounts=True), address="127.0.0.1", base_url="http://127.0.0.1:8000")
    assert browser.get("/").status_code == 200
    assert browser.get("/api/latest").status_code == 200
    assert browser.get("/api/session").json()["user"] == {
        "name": "local",
        "admin": True,
        "owner": False,
        "local": True,
        "permissions": list(auth.PERMISSIONS),
    }


@pytest.mark.parametrize("path", ["/login", "/logout"])
def test_without_accounts_logging_in_and_out_go_to_the_dashboard(conn, path):
    browser = Browser(real_app(conn, open_without_accounts=True))
    for response in (browser.get(path), browser.post(path)):
        assert response.status_code == 303
        assert response.headers["location"] == "/"


def test_without_accounts_on_the_network_nothing_opens(conn):
    browser = Browser(real_app(conn, open_without_accounts=False))
    for response in (browser.get("/"), browser.get("/api/latest"), browser.get("/login")):
        assert response.status_code == 503
        assert "reset-password admin" in response.text


def test_once_the_first_account_is_made_the_login_is_needed(conn):
    browser = Browser(real_app(conn, open_without_accounts=True))
    assert browser.get("/api/latest").status_code == 200
    add_account(conn, auth.OWNER, admin=True)
    assert browser.get("/api/latest").status_code == 401
    browser.log_in(auth.OWNER)
    assert browser.get("/api/latest").status_code == 200


# Passwords


def test_a_hash_is_salted_and_says_how_it_was_made():
    first, second = auth.hash_password(PASSWORD), auth.hash_password(PASSWORD)
    assert first != second
    assert first.startswith(f"scrypt${auth.SCRYPT_N}${auth.SCRYPT_R}${auth.SCRYPT_P}$")
    assert PASSWORD not in first
    assert auth.check_password(PASSWORD, first)
    assert auth.check_password(PASSWORD, second)
    assert not auth.check_password(PASSWORD + " ", first)


@pytest.mark.parametrize("stored", [None, "", "not a hash", "bcrypt$1$2$3$00$00", "scrypt$x$8$1$00$00"])
def test_a_missing_or_damaged_hash_never_matches(stored):
    assert not auth.check_password(PASSWORD, stored)
    assert not auth.check_password("", stored)


@pytest.mark.parametrize(
    ("password", "fine"),
    [("1234567", False), ("12345678", True), ("x" * auth.MAX_PASSWORD, True), ("x" * (auth.MAX_PASSWORD + 1), False)],
)
def test_a_password_needs_eight_characters(password, fine):
    assert (auth.password_problem(password) is None) == fine


@pytest.mark.parametrize(
    ("name", "fine"),
    [
        ("bob", True),
        ("b", True),
        ("mary.o-neill_2", True),
        ("0wen", True),
        ("x" * 32, True),
        ("x" * 33, False),
        ("", False),
        ("Bob", False),  # Made lower case before it's checked.
        ("has space", False),
        ("-dash", False),
        (".dot", False),
        ("émile", False),
        ("local", False),
    ],
)
def test_usernames(name, fine):
    assert (auth.username_problem(name) is None) == fine
