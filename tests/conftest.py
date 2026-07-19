from __future__ import annotations

import urllib.request
from typing import Any

import pytest
import requests

import src.proxies as proxies


class MockResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        text: str = "",
        content: bytes = b"",
        json_data: Any = None,
        json_error: Exception | None = None,
        raise_error: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self.content = content
        self._json_data = json_data
        self._json_error = json_error
        self._raise_error = raise_error

    def json(self) -> Any:
        if self._json_error is not None:
            raise self._json_error
        return self._json_data

    def raise_for_status(self) -> None:
        if self._raise_error is not None:
            raise self._raise_error


@pytest.fixture
def response_factory():
    return MockResponse


@pytest.fixture(autouse=True)
def block_external_io(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("test attempted unmocked external I/O")

    proxies._session = None
    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
    monkeypatch.setattr(urllib.request, "urlopen", blocked)
    monkeypatch.setattr(proxies.socket, "socket", blocked)
    yield
    proxies._session = None
