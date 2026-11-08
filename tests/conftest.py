"""Shared test helpers. Tests never touch the network: a fake session
serves canned responses and fails loudly on anything unexpected."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import requests
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def cfg() -> dict:
    """The repo's real config.yaml, pointed at a fixture site."""
    data = yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
    data = copy.deepcopy(data)
    data["site"].update(name="Example", base_url="https://example.test",
                        sitemap_url="https://example.test/sitemap.xml")
    return data


def make_response(url: str, status: int = 200, body: str | bytes = "", headers: dict | None = None) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r.url = url
    r._content = body.encode("utf-8") if isinstance(body, str) else body
    r._content_consumed = True  # like a fully read response (close() is then safe)
    r.headers.update(headers or {})
    r.encoding = "utf-8"
    return r


class FakeSession:
    """Minimal stand-in for requests.Session.

    routes: url -> Response, Exception, or a list of those (served in order).
    """

    def __init__(self, routes: dict):
        self.routes = {k: (list(v) if isinstance(v, list) else [v]) for k, v in routes.items()}
        self.headers: dict = {}
        self.calls: list[tuple[str, str]] = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        if url not in self.routes:
            raise AssertionError(f"unexpected request: {method} {url}")
        queue = self.routes[url]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s
