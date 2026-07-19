from unittest.mock import Mock, call

import pytest
import requests

import src.proxies as proxies
from src.exceptions import RequestException


def manager_config(**overrides):
    config = {"validate_proxies_on_startup": False}
    config.update(overrides)
    return config


def test_proxy_file_parsing_and_file_priority(tmp_path):
    proxy_file = tmp_path / "proxies.txt"
    proxy_file.write_text(
        "# comment\n\nmalformed\n1.2.3.4:80:user:pass\n", encoding="utf-8"
    )
    manager = proxies.ProxyManager(
        manager_config(
            proxy_file=str(proxy_file),
            proxy_type="socks5h",
            proxies=["http://ignored:1"],
        )
    )
    expected = "socks5h://user:pass@1.2.3.4:80"
    assert manager.proxies == [{"http": expected, "https": expected}]


def test_inline_proxy_mapping_and_round_robin():
    manager = proxies.ProxyManager(
        manager_config(proxies=["http://one:1", "https://two:2"])
    )
    assert manager.get_proxy() == {"http": "http://one:1", "https": "http://one:1"}
    assert manager.get_proxy() == {"http": "https://two:2", "https": "https://two:2"}
    assert manager.get_proxy() == {"http": "http://one:1", "https": "http://one:1"}


def test_missing_proxy_file_uses_direct(tmp_path):
    manager = proxies.ProxyManager(manager_config(proxy_file=str(tmp_path / "missing")))
    assert manager.proxies == []
    assert manager.get_proxy() is None


def test_proxy_validation_keeps_only_reachable(monkeypatch):
    class FakeSocket:
        def __init__(self, result):
            self.result = result
            self.timeout = None
            self.closed = False

        def settimeout(self, timeout):
            self.timeout = timeout

        def connect_ex(self, address):
            return self.result

        def close(self):
            self.closed = True

    sockets = [FakeSocket(0), FakeSocket(1)]
    socket_factory = Mock(side_effect=sockets)
    monkeypatch.setattr(proxies.socket, "socket", socket_factory)
    manager = proxies.ProxyManager(
        {
            "proxies": ["http://reachable:10", "http://blocked:20"],
            "validate_proxies_on_startup": True,
        }
    )
    assert manager.proxies == [
        {"http": "http://reachable:10", "https": "http://reachable:10"}
    ]
    assert [sock.timeout for sock in sockets] == [5, 5]
    assert all(sock.closed for sock in sockets)


def prepare_request(monkeypatch, side_effect):
    session = Mock()
    session.get.side_effect = side_effect
    monkeypatch.setattr(proxies, "get_session", Mock(return_value=session))
    monkeypatch.setattr(proxies, "get_browser_headers", Mock(return_value={"X": "header"}))
    sleep = Mock()
    monkeypatch.setattr(proxies.time, "sleep", sleep)
    monkeypatch.setattr(proxies.random, "uniform", Mock(return_value=0.75))
    return session, sleep


def test_make_request_proxy_success(response_factory, monkeypatch):
    response = response_factory()
    session, sleep = prepare_request(monkeypatch, [response])
    proxy = {"https": "http://proxy"}
    assert proxies.make_request("https://example.invalid", proxy) is response
    sleep.assert_called_once_with(0.75)
    session.get.assert_called_once_with(
        "https://example.invalid",
        timeout=20,
        proxies=proxy,
        headers={"X": "header"},
        allow_redirects=True,
    )


def test_make_request_proxy_failure_falls_back_direct(response_factory, monkeypatch):
    error = requests.exceptions.RequestException("failed")
    response = response_factory()
    session, _ = prepare_request(monkeypatch, [error, response])
    proxy = {"https": "http://proxy"}
    assert proxies.make_request("https://example.invalid", proxy) is response
    assert session.get.call_args_list[1].kwargs["proxies"] is None


def test_make_request_proxy_failure_without_fallback(monkeypatch):
    session, _ = prepare_request(
        monkeypatch, [requests.exceptions.RequestException("failed")]
    )
    with pytest.raises(RequestException):
        proxies.make_request("https://example.invalid", {"https": "proxy"}, allow_fallback=False)
    assert session.get.call_count == 1


def test_make_request_both_proxy_and_direct_fail(monkeypatch):
    error = requests.exceptions.RequestException("failed")
    session, _ = prepare_request(monkeypatch, [error, error])
    with pytest.raises(RequestException):
        proxies.make_request("https://example.invalid", {"https": "proxy"})
    assert session.get.call_count == 2


@pytest.mark.parametrize("fails", [False, True])
def test_make_request_direct_success_or_failure(fails, response_factory, monkeypatch):
    outcome = requests.exceptions.RequestException("failed") if fails else response_factory()
    session, _ = prepare_request(monkeypatch, [outcome])
    if fails:
        with pytest.raises(RequestException):
            proxies.make_request("https://example.invalid")
    else:
        assert proxies.make_request("https://example.invalid") is outcome
    assert session.get.call_args.kwargs["proxies"] is None
