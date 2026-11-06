import threading
from types import SimpleNamespace

from solar_monitor.main import close_handler

WINDOW_CLOSED = 2
LOGGING_OFF = 5
SHUTTING_DOWN = 6
CTRL_C = 0
CTRL_BREAK = 1


def test_closing_the_window_stops_the_server_and_waits_until_it_has_saved():
    server = SimpleNamespace(should_exit=False)
    finished = threading.Event()
    handle = close_handler(server, finished)
    result = []

    closing = threading.Thread(target=lambda: result.append(handle(WINDOW_CLOSED)))
    closing.start()
    closing.join(0.2)

    # Returning now would let Windows end the process before the readings are saved.
    assert closing.is_alive()
    assert server.should_exit

    finished.set()
    closing.join(2)
    assert result == [True]


def test_logging_off_and_shutting_down_are_handled_like_closing_the_window():
    for event in (LOGGING_OFF, SHUTTING_DOWN):
        server = SimpleNamespace(should_exit=False)
        finished = threading.Event()
        finished.set()

        assert close_handler(server, finished)(event) is True
        assert server.should_exit


def test_ctrl_c_is_left_to_uvicorn():
    for event in (CTRL_C, CTRL_BREAK):
        server = SimpleNamespace(should_exit=False)

        assert close_handler(server, threading.Event())(event) is False
        assert not server.should_exit
