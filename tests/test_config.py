from solar_monitor import config


class FakeSocket:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.sent = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def setsockopt(self, *_):
        pass

    def settimeout(self, _):
        pass

    def sendto(self, *_):
        self.sent += 1

    def recvfrom(self, _):
        reply = next(self.replies)
        if reply is None:
            raise TimeoutError
        return b"reply", (reply, 48899)


def use_socket(monkeypatch, fake):
    monkeypatch.setattr(config.socket, "socket", lambda *_: fake)


def without_env_file(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda *_: None)


def test_dashboard_stays_on_this_machine_unless_told_otherwise(monkeypatch):
    without_env_file(monkeypatch)
    monkeypatch.delenv("BIND_HOST", raising=False)
    assert config.bind_host() == "127.0.0.1"
    monkeypatch.setenv("BIND_HOST", "  ")
    assert config.bind_host() == "127.0.0.1"


def test_bind_host_can_open_the_dashboard_to_the_network(monkeypatch):
    without_env_file(monkeypatch)
    monkeypatch.setenv("BIND_HOST", "0.0.0.0")
    assert config.bind_host() == "0.0.0.0"


def test_discovery_survives_a_missed_reply(monkeypatch):
    fake = FakeSocket([None, "10.0.0.5"])
    use_socket(monkeypatch, fake)
    assert config.discover_host() == "10.0.0.5"
    assert fake.sent == 2


def test_discovery_gives_up_after_its_attempts(monkeypatch):
    fake = FakeSocket([None] * config.DISCOVERY_ATTEMPTS)
    use_socket(monkeypatch, fake)
    assert config.discover_host() is None
    assert fake.sent == config.DISCOVERY_ATTEMPTS
