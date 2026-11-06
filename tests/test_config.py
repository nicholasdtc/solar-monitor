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
