"""The Users screen, changing your own password, the Action history tab, and making and resetting
accounts from the command line."""

import json
import re
from html.parser import HTMLParser

import pytest
from logins import PASSWORD, Browser, add_account, real_app

from solar_monitor import auth, config, db, main
from solar_monitor.api import create_app
from solar_monitor.collector import Collector

NEW = "a brand new password"


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    yield conn
    conn.close()


@pytest.fixture
def ids(conn):
    """The owner, another admin, and someone who can see Statistics."""
    return {
        "admin": add_account(conn, auth.OWNER, admin=True),
        "carol": add_account(conn, "carol", admin=True),
        "bob": add_account(conn, "bob", permissions=["statistics"]),
    }


@pytest.fixture
def app(conn, ids):
    return real_app(conn)


def as_(app, name):
    return Browser(app).log_in(name)


def actions(conn, kind="users"):
    return [(row["username"], row["summary"]) for row in db.list_history(conn, kind=kind)]


def names(response):
    return [user["username"] for user in response.json()["users"]]


# The list


def test_admins_see_everyone_owner_first(app, ids):
    response = as_(app, "carol").get("/api/users")
    assert response.status_code == 200
    body = response.json()
    assert names(response) == ["admin", "bob", "carol"]
    assert body["you"] == ids["carol"]
    assert [p["id"] for p in body["permissions"]] == list(auth.PERMISSIONS)
    owner, bob, carol = body["users"]
    assert (owner["owner"], owner["admin"]) == (True, True)
    assert (bob["owner"], bob["admin"], bob["permissions"]) == (False, False, ["statistics"])
    assert carol["sessions"] == 1
    assert bob["sessions"] == 0


def test_the_list_holds_no_password_hash(app, conn):
    text = as_(app, "admin").get("/api/users").text
    for (stored,) in conn.execute("SELECT password_hash FROM users"):
        assert stored not in text
        assert stored.split("$")[-1] not in text
    assert "password" not in text


def test_only_admins_reach_the_accounts(app, ids):
    bob = as_(app, "bob")
    assert bob.get("/api/users").status_code == 403
    assert bob.post("/api/users", json={"username": "eve", "password": NEW}).status_code == 403
    assert bob.put(f"/api/users/{ids['bob']}", json={"admin": True, "permissions": []}).status_code == 403
    assert bob.delete(f"/api/users/{ids['carol']}").status_code == 403


def test_the_routes_check_for_an_admin_themselves(app, ids, monkeypatch):
    # In case a mistake in auth.ROUTES ever let someone through.
    monkeypatch.setattr(auth, "needs", lambda *_: ())
    bob = as_(app, "bob")
    assert bob.get("/api/users").status_code == 403
    assert bob.put(f"/api/users/{ids['bob']}", json={"admin": True, "permissions": []}).status_code == 403


def test_without_accounts_the_accounts_point_to_the_command_line(conn):
    browser = Browser(create_app(Collector("test", 0, conn)), address="127.0.0.1", base_url="http://127.0.0.1:8000")
    for response in (browser.get("/api/users"), browser.put("/api/password", json={"current": PASSWORD, "new": NEW})):
        assert response.status_code == 409
        assert "reset-password admin" in response.json()["detail"]


# Adding


def test_adding_an_account(app, conn):
    response = as_(app, "carol").post(
        "/api/users", json={"username": "  Dave ", "password": NEW, "permissions": ["history", "statistics"]}
    )
    assert response.status_code == 201
    assert "dave" in names(response)
    dave = Browser(app).log_in("dave", NEW)
    assert dave.get("/api/session").json()["user"]["permissions"] == ["statistics", "history"]
    assert actions(conn)[0] == ("carol", "Added dave, with Statistics, Action history")


def test_adding_an_admin(app, conn):
    as_(app, "carol").post("/api/users", json={"username": "erin", "password": NEW, "admin": True})
    assert Browser(app).log_in("erin", NEW).get("/api/users").status_code == 200
    assert actions(conn)[0] == ("carol", "Added erin, as an admin")


def test_a_new_account_starts_with_the_owners_display_choices_on_the_dashboard(app, conn, ids):
    db.save_display_prefs(conn, ids["admin"], {"theme": "dark", "section": "statistics", "units": "kw"})
    as_(app, "carol").post("/api/users", json={"username": "dave", "password": NEW})
    assert db.get_display_prefs(conn, db.find_user(conn, "dave")["id"]) == {"theme": "dark", "units": "kw"}


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"username": "has space", "password": NEW}, 400),
        ({"username": "", "password": NEW}, 400),
        ({"username": "x" * 33, "password": NEW}, 400),
        ({"username": "local", "password": NEW}, 400),
        ({"username": "dave", "password": "short"}, 400),
        ({"username": "dave", "password": NEW, "permissions": ["root"]}, 400),
        ({"username": "BOB", "password": NEW}, 409),
        ({"username": "Admin", "password": NEW}, 409),
        ({"username": "dave", "password": NEW, "admin": "yes"}, 422),
        ({"username": "dave", "password": NEW, "admin": 1}, 422),
        ({"username": "dave"}, 422),
    ],
)
def test_an_account_that_cant_be_added(app, conn, body, status):
    response = as_(app, "carol").post("/api/users", json=body)
    assert response.status_code == status
    assert db.find_user(conn, "dave") is None
    assert actions(conn) == []


# Changing


def test_changing_what_an_account_may_do_applies_at_once(app, conn, ids):
    bob = as_(app, "bob")
    assert bob.get("/api/actions").status_code == 403
    response = as_(app, "carol").put(f"/api/users/{ids['bob']}", json={"admin": False, "permissions": ["history"]})
    assert response.status_code == 200
    assert bob.get("/api/actions").status_code == 200
    assert actions(conn)[0] == ("carol", "Changed bob: given Action history; lost Statistics")


def test_making_an_admin(app, conn, ids):
    as_(app, "carol").put(f"/api/users/{ids['bob']}", json={"admin": True, "permissions": ["statistics"]})
    assert as_(app, "bob").get("/api/users").status_code == 200
    assert actions(conn)[0] == ("carol", "Changed bob: made an admin")


def test_no_change_isnt_recorded(app, conn, ids):
    as_(app, "carol").put(f"/api/users/{ids['bob']}", json={"admin": False, "permissions": ["statistics"]})
    assert actions(conn) == []


def test_an_admin_cant_remove_their_own_admin(app, conn, ids):
    response = as_(app, "carol").put(f"/api/users/{ids['carol']}", json={"admin": False, "permissions": []})
    assert response.status_code == 400
    assert db.get_user(conn, ids["carol"])["admin"] is True


def test_another_admin_can(app, conn, ids):
    response = as_(app, "admin").put(f"/api/users/{ids['carol']}", json={"admin": False, "permissions": []})
    assert response.status_code == 200
    assert as_(app, "carol").get("/api/users").status_code == 403


def test_an_unknown_permission_changes_nothing(app, conn, ids):
    response = as_(app, "carol").put(f"/api/users/{ids['bob']}", json={"admin": False, "permissions": ["root"]})
    assert response.status_code == 400
    assert db.get_user(conn, ids["bob"])["permissions"] == ["statistics"]


# The owner


@pytest.mark.parametrize("acting", ["carol", "admin"])
def test_no_one_can_change_the_owners_account_from_the_page(app, conn, ids, acting):
    browser = as_(app, acting)
    owner = ids["admin"]
    before = db.get_user(conn, owner)
    for response in (
        browser.put(f"/api/users/{owner}", json={"admin": False, "permissions": []}),
        browser.put(f"/api/users/{owner}/password", json={"password": NEW}),
        browser.delete(f"/api/users/{owner}"),
        browser.delete(f"/api/users/{owner}/sessions"),
    ):
        assert response.status_code == 403
        assert "owner" in response.json()["detail"]
    assert db.get_user(conn, owner) == before
    assert Browser(app).log_in("admin").get("/api/users").status_code == 200


def test_the_owner_changes_their_own_password_with_the_current_one(app):
    owner = as_(app, "admin")
    assert owner.put("/api/password", json={"current": PASSWORD, "new": NEW}).status_code == 200
    Browser(app).log_in("admin", NEW)


# Passwords set by an admin


def test_an_admin_sets_a_new_password_and_logs_them_out(app, conn, ids):
    bob = as_(app, "bob")
    response = as_(app, "carol").put(f"/api/users/{ids['bob']}/password", json={"password": NEW})
    assert response.status_code == 200
    assert bob.get("/api/latest").status_code == 401
    Browser(app).log_in("bob", NEW)
    assert Browser(app).post("/login", data={"username": "bob", "password": PASSWORD}).status_code == 401
    assert actions(conn)[0] == ("carol", "Set a new password for bob, logging them out (1)")


def test_an_admin_sets_their_own_from_the_menu(app, ids):
    response = as_(app, "carol").put(f"/api/users/{ids['carol']}/password", json={"password": NEW})
    assert response.status_code == 400
    assert "Change password" in response.json()["detail"]


def test_a_short_password_isnt_set(app, ids):
    assert as_(app, "carol").put(f"/api/users/{ids['bob']}/password", json={"password": "short"}).status_code == 400
    Browser(app).log_in("bob")


# Removing


def test_removing_an_account_logs_it_out_and_keeps_what_it_did(app, conn, ids):
    bob = as_(app, "bob")
    response = as_(app, "carol").delete(f"/api/users/{ids['bob']}")
    assert response.status_code == 200
    assert "bob" not in names(response)
    assert bob.get("/api/latest").status_code == 401
    assert ("bob", "Logged in from 192.0.2.1") in actions(conn, "login")
    assert actions(conn)[0] == ("carol", "Removed bob")
    assert "bob" in db.history_names(conn)


def test_an_admin_cant_remove_themselves(app, conn, ids):
    assert as_(app, "carol").delete(f"/api/users/{ids['carol']}").status_code == 400
    assert db.get_user(conn, ids["carol"]) is not None


def test_removing_an_account_thats_gone(app):
    assert as_(app, "carol").delete("/api/users/999").status_code == 404


# Logging someone out


def test_an_admin_logs_someone_out_everywhere(app, conn, ids):
    bob = as_(app, "bob")
    other = as_(app, "bob")
    response = as_(app, "carol").delete(f"/api/users/{ids['bob']}/sessions")
    assert response.status_code == 200
    assert bob.get("/api/latest").status_code == 401
    assert other.get("/api/latest").status_code == 401
    assert actions(conn)[0] == ("carol", "Logged bob out everywhere (2)")


def test_logging_yourself_out_is_from_the_menu(app, ids):
    carol = as_(app, "carol")
    assert carol.delete(f"/api/users/{ids['carol']}/sessions").status_code == 400
    assert carol.get("/api/latest").status_code == 200


# Your own password


def test_changing_your_password_logs_you_out_everywhere_else(app, conn):
    elsewhere = as_(app, "bob")
    bob = as_(app, "bob")
    response = bob.put("/api/password", json={"current": PASSWORD, "new": NEW})
    assert response.status_code == 200
    assert response.json() == {"ended": 1}
    assert bob.get("/api/latest").status_code == 200
    assert elsewhere.get("/api/latest").status_code == 401
    Browser(app).log_in("bob", NEW)
    assert actions(conn)[-1] == ("bob", "Changed their password, logging out elsewhere (1)")


def test_the_wrong_current_password_changes_nothing(app):
    bob = as_(app, "bob")
    response = bob.put("/api/password", json={"current": "not the password", "new": NEW})
    assert response.status_code == 403
    assert "current password" in response.json()["detail"]
    Browser(app).log_in("bob")


def test_a_short_new_password_changes_nothing(app):
    assert as_(app, "bob").put("/api/password", json={"current": PASSWORD, "new": "short"}).status_code == 400
    Browser(app).log_in("bob")


def test_guessing_the_current_password_has_to_wait(app):
    bob = as_(app, "bob")
    for _ in range(auth.Throttle.FREE_TRIES):
        assert bob.put("/api/password", json={"current": "a wrong guess", "new": NEW}).status_code == 403
    assert bob.put("/api/password", json={"current": PASSWORD, "new": NEW}).status_code == 429
    Browser(app).log_in("bob")


# The Action history tab


@pytest.fixture
def full_history(conn, ids, monkeypatch):
    """One of each kind, a second apart, oldest first."""
    clock = iter(range(1000, 1100))
    monkeypatch.setattr(db.time, "time", lambda: next(clock))
    db.add_action(conn, (ids["bob"], "bob"), "login", "Logged in from 192.0.2.1")
    db.add_setting_write(conn, "backup", 0, 1, (ids["carol"], "carol"))
    db.add_action(conn, (ids["carol"], "carol"), "data", "Day rate 0.3 → 0.35")
    db.add_setting_write(conn, "min_soc", 20, 10, None)  # From before accounts.
    db.add_action(conn, (ids["admin"], "admin"), "users", "Added bob, with Statistics")
    monkeypatch.undo()


def history_of(app, query=""):
    response = as_(app, "admin").get(f"/api/actions{query}")
    assert response.status_code == 200
    return response.json()


def test_the_history_holds_everything_latest_first(app, full_history):
    rows = [row for row in history_of(app)["rows"] if row["ts"] < 1100]  # Not the log-in just made to look.
    assert [(row["username"], row["kind"], row["summary"]) for row in rows] == [
        ("admin", "users", "Added bob, with Statistics"),
        (None, "inverter", "On-grid floor 20% → 10%"),
        ("carol", "data", "Day rate 0.3 → 0.35"),
        ("carol", "inverter", "Backup mode off → on"),
        ("bob", "login", "Logged in from 192.0.2.1"),
    ]


def test_the_history_by_person_and_by_kind(app, full_history):
    assert [row["summary"] for row in history_of(app, "?user=carol")["rows"]] == [
        "Day rate 0.3 → 0.35",
        "Backup mode off → on",
    ]
    assert [row["username"] for row in history_of(app, "?kind=inverter")["rows"]] == [None, "carol"]


def test_the_history_comes_a_page_at_a_time(app, full_history):
    first = history_of(app, "?kind=inverter&limit=1")
    assert first["more"] is True
    assert [row["ts"] for row in first["rows"]] == [1003]
    older = history_of(app, f"?kind=inverter&limit=2&before={first['rows'][0]['ts']}")
    # The second it ended on comes again, for the page to drop what it has.
    assert [row["ts"] for row in older["rows"]] == [1003, 1001]
    assert older["more"] is True
    assert history_of(app, "?kind=inverter&limit=5&before=1001")["more"] is False


def test_the_history_names_everyone_including_the_removed(app, conn, ids, full_history):
    db.delete_user(conn, ids["bob"])
    assert history_of(app)["names"] == ["admin", "bob", "carol"]


@pytest.mark.parametrize("query", ["?kind=everything", "?limit=0", "?limit=501", "?user=" + "x" * 65])
def test_the_history_refuses_what_it_cant_answer(app, query):
    assert as_(app, "admin").get(f"/api/actions{query}").status_code == 422


# The page


def session_user(page):
    """What the page is told about who's logged in, as its script reads it."""
    found = []

    class Meta(HTMLParser):
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "meta" and attrs.get("name") == "session-user":
                found.append(attrs["content"])

    Meta().feed(page)
    assert len(found) == 1
    return json.loads(found[0])


def test_the_page_knows_who_is_logged_in(app):
    page = as_(app, "bob").get("/").text
    assert session_user(page) == {
        "name": "bob", "admin": False, "owner": False, "local": False, "permissions": ["statistics"],
    }


def test_the_page_knows_what_write_to_inverter_opens(app, conn):
    add_account(conn, "dave", permissions=["inverter"])
    assert session_user(as_(app, "dave").get("/").text)["permissions"] == ["inverter", "schedule"]


def test_without_accounts_the_page_knows_its_this_machine(conn):
    page = Browser(real_app(conn, open_without_accounts=True), address="127.0.0.1").get("/").text
    user = session_user(page)
    assert (user["local"], user["admin"]) == (True, True)


def test_every_tab_but_the_dashboard_has_a_permission(app):
    page = as_(app, "admin").get("/").text
    links = re.search(r'<ul id="tab-links">(.*?)</ul>', page, re.S).group(1)
    tabs = re.findall(r'href="#([\w-]+)"', links)
    assert tabs[0] == "dashboard"
    assert set(tabs[1:]) == set(auth.TABS)
    for tab in tabs:
        assert f'id="view-{tab}"' in page


def test_everything_the_script_looks_up_is_on_the_page(app):
    page = as_(app, "admin").get("/").text
    ids = set(re.findall(r'\bid="([^"]+)"', page))
    looked_up = set(re.findall(r'\$\("([^"]+)"\)', page))
    assert {"open-users", "open-password", "users-rows", "user-password", "actions-rows", "access-note"} <= looked_up
    assert looked_up - ids == set()


# The command line


def answers(*typed):
    given = iter(typed)
    return lambda prompt: next(given)


def test_resetting_with_no_accounts_makes_the_owners(tmp_path, capsys):
    path = tmp_path / "test.db"
    conn = db.connect(path)
    db.save_display_prefs(conn, 0, {"theme": "dark", "section": "statistics"})
    conn.close()
    assert main.reset_password("ADMIN", path, answers(NEW, NEW)) == 0
    assert "Log in as admin" in capsys.readouterr().out
    conn = db.connect(path)
    try:
        owner = db.find_user(conn, "admin")
        assert owner["admin"] is True
        assert auth.check_password(NEW, owner["password_hash"])
        # Theirs to start with, as the choices made before there were accounts.
        assert db.get_display_prefs(conn, owner["id"]) == {"theme": "dark", "section": "statistics"}
        assert actions(conn) == [("command line", "Made the owner's account, admin, from the command line")]
    finally:
        conn.close()


def test_resetting_a_password_logs_them_out(tmp_path):
    path = tmp_path / "test.db"
    conn = db.connect(path)
    bob = add_account(conn, "bob")
    db.add_session(conn, "a hash", bob, 2**40)
    conn.close()
    assert main.reset_password("bob", path, answers(NEW, NEW)) == 0
    conn = db.connect(path)
    try:
        assert auth.check_password(NEW, db.find_user(conn, "bob")["password_hash"])
        assert db.count_sessions(conn) == {}
        assert actions(conn) == [("command line", "Set a new password for bob, logging them out (1)")]
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("name", "typed", "error"),
    [
        ("nobody", (NEW, NEW), "no account named nobody"),
        ("admin", ("short", "short"), "at least 8"),
        ("admin", (NEW, NEW + "x"), "didn't match"),
    ],
)
def test_resetting_that_changes_nothing(tmp_path, capsys, name, typed, error):
    path = tmp_path / "test.db"
    db.connect(path).close()
    assert main.reset_password(name, path, answers(*typed)) == 1
    assert error in capsys.readouterr().err
    conn = db.connect(path)
    try:
        assert not db.has_users(conn)
    finally:
        conn.close()


def test_resetting_without_the_database_doesnt_make_one(tmp_path, capsys):
    path = tmp_path / "wrong" / "test.db"
    assert main.reset_password("admin", path, answers(NEW, NEW)) == 1
    assert "DB_PATH" in capsys.readouterr().err
    assert not path.exists()
    assert not path.parent.exists()


@pytest.fixture
def env(monkeypatch):
    """Nothing from the real .env."""
    monkeypatch.setattr(config, "load_dotenv", lambda *_: None)
    for name in ("BIND_HOST", "DASHBOARD_USER", "DASHBOARD_PASSWORD", "DB_PATH"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_the_old_password_makes_the_owners_account(env, conn, tmp_path, capsys):
    env.setenv("DASHBOARD_USER", "nick")
    env.setenv("DASHBOARD_PASSWORD", "the old password")
    db.save_display_prefs(conn, 0, {"theme": "dark"})
    assert main.prepare_accounts(conn, tmp_path / "test.db", "0.0.0.0") is True
    owner = db.find_user(conn, "admin")
    assert auth.check_password("the old password", owner["password_hash"])
    assert db.get_display_prefs(conn, owner["id"]) == {"theme": "dark"}
    assert db.find_user(conn, "nick") is None
    assert "Log in as admin" in capsys.readouterr().out


def test_a_weak_old_password_is_used_with_a_warning(env, conn, tmp_path, capsys):
    env.setenv("DASHBOARD_PASSWORD", "pw")
    assert main.prepare_accounts(conn, tmp_path / "test.db", "0.0.0.0") is True
    assert auth.check_password("pw", db.find_user(conn, "admin")["password_hash"])
    assert "weak" in capsys.readouterr().err


@pytest.mark.parametrize("left", ["DASHBOARD_USER", "DASHBOARD_PASSWORD"])
def test_with_accounts_the_old_login_only_gets_a_reminder(env, conn, tmp_path, capsys, left):
    owner = add_account(conn, "admin", admin=True)
    env.setenv(left, "something old")
    assert main.prepare_accounts(conn, tmp_path / "test.db", "0.0.0.0") is True
    assert auth.check_password(PASSWORD, db.get_user(conn, owner)["password_hash"])
    assert "aren't used any more" in capsys.readouterr().err


def test_the_old_sessions_file_goes(env, conn, tmp_path):
    old = tmp_path / "sessions.json"
    old.write_text("{}", encoding="utf-8")
    main.prepare_accounts(conn, tmp_path / "test.db", "127.0.0.1")
    assert not old.exists()


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_without_accounts_this_machine_alone_starts_open(env, conn, tmp_path, host):
    assert main.prepare_accounts(conn, tmp_path / "test.db", host) is True
    assert not db.has_users(conn)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.10", "pi.example"])
def test_without_accounts_the_app_wont_open_to_the_network(env, tmp_path, capsys, monkeypatch, host):
    env.setenv("BIND_HOST", host)
    env.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(main.sys, "argv", ["solar-monitor"])

    def stop(*_):
        raise AssertionError("got past the accounts check")

    monkeypatch.setattr(main, "resolve_target", stop)
    assert main.main() == 1
    assert "reset-password admin" in capsys.readouterr().err
