"""The real fetch: one GET with a short timeout, naming the app, and polite about caching.

It remembers each URL's Last-Modified, to ask only for something newer, and its Expires, to not ask
again before then. Either way, nothing new raises NotModified.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
from typing import Any

from solar_monitor.weather.base import FetchError, NotModified, RateLimited

# The app and its public repo, as services' terms ask. Never a person's email address.
USER_AGENT = "solar-monitor (github.com/nicholasdtc/solar-monitor)"
TIMEOUT_SECONDS = 20
MAX_BYTES = 5_000_000


def _http_date(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError):
        return None


class HttpFetch:
    def __init__(self, clock=time.time) -> None:
        self.clock = clock
        self.expires: dict[str, float] = {}
        self.modified: dict[str, str] = {}

    def __call__(self, url: str, params: dict[str, Any]) -> Any:
        full = f"{url}?{urllib.parse.urlencode(params)}" if params else url
        if self.clock() < self.expires.get(full, 0):
            raise NotModified()
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if full in self.modified:
            headers["If-Modified-Since"] = self.modified[full]
        request = urllib.request.Request(full, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                body = response.read(MAX_BYTES + 1)
                expires, modified = response.headers.get("Expires"), response.headers.get("Last-Modified")
        except urllib.error.HTTPError as error:
            if error.code == 304:
                raise NotModified() from None
            kind = RateLimited if error.code == 429 else FetchError
            raise kind(f"HTTP {error.code}", error.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise FetchError(f"no answer: {getattr(error, 'reason', error)}") from None
        if len(body) > MAX_BYTES:
            raise FetchError("answer too large")
        try:
            answer = json.loads(body)
        except ValueError:
            raise FetchError("answer isn't JSON") from None
        when = _http_date(expires)
        if when is not None:
            self.expires[full] = when
        if modified:
            self.modified[full] = modified
        return answer
