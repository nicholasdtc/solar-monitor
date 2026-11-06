import os
import sys
import threading

import pytest

from solar_monitor import stop


@pytest.mark.parametrize(
    "name, command_line",
    [
        ("python.exe", r".venv\Scripts\python.exe  -m solar_monitor.main"),
        ("python.exe", r"E:\app\.venv\Scripts\python.exe -m solar_monitor.main --host 1.2.3.4"),
        ("pythonw.exe", "pythonw -m solar_monitor.main"),
        ("python.exe", r'"E:\app\.venv\Scripts\python.exe" "E:\app\.venv\Scripts\solar-monitor.exe"'),
        ("solar-monitor.exe", r"E:\app\.venv\Scripts\solar-monitor.exe"),
    ],
)
def test_finds_each_way_of_running_the_server(name, command_line):
    assert stop.is_server(name, command_line)


@pytest.mark.parametrize(
    "name, command_line",
    [
        ("python.exe", "python -m pytest"),
        ("python.exe", "python -m solar_monitor.stop"),
        ("python.exe", "python -m solar_monitor.main_backup"),
        ("python.exe", r"python E:\Dev\solar-monitor\scratch.py"),
        ("python.exe", 'python -c "print(\'-m solar_monitor.mainly\')"'),
        ("python.exe", None),
        ("claude.exe", "claude --prompt -m solar_monitor.main"),
        ("cmd.exe", r"cmd /c E:\app\.venv\Scripts\solar-monitor.exe"),
    ],
)
def test_leaves_every_other_process_alone(name, command_line):
    assert not stop.is_server(name, command_line)


def use_fake_processes(monkeypatch, polls, listening=()):
    """polls is what each look at the running processes finds, in order."""
    remaining = list(polls)
    forced = []
    monkeypatch.setattr(stop, "running_servers", lambda: remaining.pop(0) if len(remaining) > 1 else remaining[0])
    monkeypatch.setattr(stop, "ask_to_stop", lambda pid: pid in listening)
    monkeypatch.setattr(stop, "force_stop", forced.append)
    monkeypatch.setattr(stop.time, "sleep", lambda seconds: None)
    return forced


def test_nothing_running_is_fine(monkeypatch, capsys):
    forced = use_fake_processes(monkeypatch, [[]])

    assert stop.stop_all() == 0
    assert forced == []
    assert "No dashboard is running" in capsys.readouterr().out


def test_a_copy_that_stops_when_asked_is_not_forced(monkeypatch, capsys):
    forced = use_fake_processes(monkeypatch, [[10, 11], [10, 11], []], listening={11})

    assert stop.stop_all() == 0
    assert forced == []
    assert "All readings saved" in capsys.readouterr().out


def test_a_copy_that_ignores_the_request_is_forced_after_the_wait(monkeypatch, capsys):
    forced = use_fake_processes(monkeypatch, [[10, 11], []], listening={11})

    assert stop.stop_all(wait_seconds=0) == 0
    assert forced == [10, 11]
    assert "are lost" in capsys.readouterr().out


def test_a_copy_that_cannot_listen_is_forced_without_a_wait(monkeypatch, capsys):
    forced = use_fake_processes(monkeypatch, [[10], []])

    assert stop.stop_all() == 0
    assert forced == [10]
    assert "are lost" in capsys.readouterr().out


def test_a_copy_that_survives_being_forced_is_reported(monkeypatch, capsys):
    forced = use_fake_processes(monkeypatch, [[10]])

    assert stop.stop_all() == 1
    assert forced == [10]
    assert "Could not stop process 10" in capsys.readouterr().out


@pytest.mark.skipif(sys.platform != "win32", reason="named events are Windows only")
def test_a_stop_request_reaches_the_listening_process():
    requested = threading.Event()
    assert not stop.ask_to_stop(os.getpid())

    stop.on_stop_request(os.getpid(), requested.set)

    assert not requested.wait(0.1)
    assert stop.ask_to_stop(os.getpid())
    assert requested.wait(2)
