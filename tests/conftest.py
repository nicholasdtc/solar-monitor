import urllib.request

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """The app's weather fetch must never reach a real service from a test. Tests of the fetch
    itself replace this with their own canned answers."""

    def refuse(request, timeout=None):
        raise AssertionError(f"a test tried to fetch {getattr(request, 'full_url', request)}")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
