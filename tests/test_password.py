import pytest
from dotenv import dotenv_values

from solar_monitor import password
from solar_monitor.password import hash_password, is_hashed, parse, verify

PASSWORD = "correct horse+é&= #$'\""
CHEAP = {"n": 16, "r": 1, "p": 1}  # Tests don't need the real cost.


def test_the_right_password_matches_its_hash():
    stored = hash_password(PASSWORD, **CHEAP)
    assert is_hashed(stored)
    assert verify(PASSWORD, stored)


@pytest.mark.parametrize("attempt", ["", "correct horse", PASSWORD + " ", PASSWORD.upper()])
def test_anything_else_does_not(attempt):
    assert not verify(attempt, hash_password(PASSWORD, **CHEAP))


def test_the_hash_does_not_contain_the_password_and_is_salted():
    first, second = hash_password(PASSWORD, **CHEAP), hash_password(PASSWORD, **CHEAP)
    assert first != second
    assert "horse" not in first


def test_the_real_cost_is_the_one_recommended():
    n, r, p, salt, key = parse(hash_password("x"))
    assert (n, r, p, len(salt), len(key)) == (2**17, 8, 1, 16, 32)


def test_env_reads_the_hash_back_exactly(tmp_path):
    stored = hash_password(PASSWORD, **CHEAP)
    env = tmp_path / ".env"
    env.write_text(f"DASHBOARD_PASSWORD={stored}\n", encoding="utf-8")
    assert dotenv_values(env)["DASHBOARD_PASSWORD"] == stored


@pytest.mark.parametrize(
    "stored",
    [
        "scrypt:",
        "scrypt:16:1:1:00:00",
        "scrypt:15:1:1:" + "00" * 16 + ":" + "00" * 32,  # n isn't a power of two
        "scrypt:16:1:1:" + "zz" * 16 + ":" + "00" * 32,
        "scrypt:16:0:1:" + "00" * 16 + ":" + "00" * 32,
        f"scrypt:{2**30}:8:1:" + "00" * 16 + ":" + "00" * 32,  # far more memory than the Pi has
        "scrypt:16:1:1:" + "00" * 16,
    ],
)
def test_a_damaged_hash_is_rejected(stored):
    assert is_hashed(stored)
    with pytest.raises(ValueError):
        parse(stored)


def test_the_command_prints_a_line_for_env(monkeypatch, capsys):
    monkeypatch.setattr(password.getpass, "getpass", lambda _: PASSWORD)
    assert password.main() == 0
    line = capsys.readouterr().out.splitlines()[-1]
    name, stored = line.split("=", 1)
    assert name == "DASHBOARD_PASSWORD"
    assert verify(PASSWORD, stored)


@pytest.mark.parametrize("typed", [["  ", "  "], ["one", "two"]])
def test_the_command_refuses_a_blank_or_mistyped_password(monkeypatch, capsys, typed):
    answers = iter(typed)
    monkeypatch.setattr(password.getpass, "getpass", lambda _: next(answers))
    assert password.main() == 1
    assert "DASHBOARD_PASSWORD" not in capsys.readouterr().out
