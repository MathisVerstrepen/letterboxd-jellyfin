from unittest.mock import Mock, call

import pytest
import requests

import src.radarr as radarr
from src.exceptions import RadarrException


def client_without_connection():
    client = radarr.RadarrClient.__new__(radarr.RadarrClient)
    client.base_url = "http://radarr.invalid/api/v3"
    client.headers = {"X-Api-Key": "fake-key"}
    client.timeout = 12
    client.logger = Mock()
    return client


def test_constructor_normalizes_url_and_connects(response_factory, monkeypatch):
    get = Mock(return_value=response_factory(status_code=200))
    monkeypatch.setattr(radarr.requests, "get", get)
    client = radarr.RadarrClient("http://radarr.invalid/", "fake-key", timeout=9)
    assert client.base_url == "http://radarr.invalid/api/v3"
    get.assert_called_once_with(
        "http://radarr.invalid/api/v3/system/status",
        headers={"X-Api-Key": "fake-key"},
        timeout=9,
    )


@pytest.mark.parametrize(
    "outcome",
    [
        lambda response: response(status_code=503),
        lambda response: requests.exceptions.ConnectionError("unreachable"),
    ],
)
def test_constructor_connection_failures(outcome, response_factory, monkeypatch):
    monkeypatch.setattr(radarr.requests, "get", Mock(return_value=outcome(response_factory)))
    if isinstance(outcome(response_factory), Exception):
        radarr.requests.get.side_effect = outcome(response_factory)
    with pytest.raises(RadarrException):
        radarr.RadarrClient("http://radarr.invalid", "fake-key")


def lookup_response(response_factory, data, **kwargs):
    return response_factory(
        headers={"Content-Type": "application/json"}, json_data=data, **kwargs
    )


def test_lookup_maps_complete_movie_and_animation(response_factory, monkeypatch):
    response = lookup_response(
        response_factory,
        [
            {
                "title": "Movie",
                "tmdbId": 123,
                "year": 2020,
                "movieFile": {"id": 1},
                "monitored": True,
                "genres": ["Animation", "Family"],
            }
        ],
    )
    get = Mock(return_value=response)
    monkeypatch.setattr(radarr.requests, "get", get)
    result = client_without_connection().check_radarr_state("123")
    assert result.failed_items == 0
    assert result.state == {
        "hasFile": True,
        "monitored": True,
        "name": "Movie",
        "tmdbId": 123,
        "productionYear": 2020,
        "is_animation": True,
    }
    assert get.call_args.kwargs["params"] == {"term": "tmdb:123"}


@pytest.mark.parametrize(
    "response_builder",
    [
        lambda response: response(headers={"Content-Type": "text/html"}),
        lambda response: lookup_response(response, []),
        lambda response: lookup_response(response, {"unexpected": True}),
        lambda response: lookup_response(response, [{"title": "Incomplete"}]),
        lambda response: response(
            headers={"Content-Type": "application/json"},
            json_error=requests.exceptions.JSONDecodeError("bad", "doc", 0),
        ),
        lambda response: response(
            headers={"Content-Type": "application/json"},
            raise_error=requests.exceptions.HTTPError("bad status"),
        ),
    ],
)
def test_lookup_failure_shapes(response_builder, response_factory, monkeypatch):
    monkeypatch.setattr(
        radarr.requests, "get", Mock(return_value=response_builder(response_factory))
    )
    result = client_without_connection().check_radarr_state("123")
    assert result.state is None
    assert result.failed_items == 1


def test_lookup_request_failure(response_factory, monkeypatch):
    monkeypatch.setattr(
        radarr.requests,
        "get",
        Mock(side_effect=requests.exceptions.ConnectionError("failed")),
    )
    result = client_without_connection().check_radarr_state("123")
    assert result.state is None
    assert result.failed_items == 1


def movie_state():
    return {
        "tmdbId": 123,
        "name": "Movie",
        "productionYear": 2020,
        "hasFile": False,
        "monitored": False,
        "is_animation": False,
    }


@pytest.mark.parametrize(
    ("status", "text"), [(201, ""), (400, "Movie has already been added")]
)
def test_queue_success_and_payload(status, text, response_factory, monkeypatch):
    post = Mock(return_value=response_factory(status_code=status, text=text))
    monkeypatch.setattr(radarr.requests, "post", post)
    result = client_without_connection().add_to_radarr_download_queue(
        [movie_state()], "/movies", 7
    )
    assert result == radarr.MutationResult(attempted=1, succeeded=1)
    assert post.call_args.kwargs["json"] == {
        "tmdbId": 123,
        "title": "Movie",
        "year": 2020,
        "qualityProfileId": 7,
        "monitored": True,
        "rootFolderPath": "/movies",
        "addOptions": {"searchForMovie": True},
    }


def test_queue_retries_request_failures_then_succeeds(response_factory, monkeypatch):
    error = requests.exceptions.ConnectionError("failed")
    post = Mock(side_effect=[error, error, response_factory(status_code=201)])
    sleep = Mock()
    monkeypatch.setattr(radarr.requests, "post", post)
    monkeypatch.setattr(radarr.time, "sleep", sleep)
    result = client_without_connection().add_to_radarr_download_queue(
        [movie_state()], "/movies", 7
    )
    assert result.succeeded == 1
    assert sleep.call_args_list == [call(1), call(2)]


def test_queue_terminal_request_failure(response_factory, monkeypatch):
    post = Mock(side_effect=requests.exceptions.ConnectionError("failed"))
    monkeypatch.setattr(radarr.requests, "post", post)
    monkeypatch.setattr(radarr.time, "sleep", Mock())
    result = client_without_connection().add_to_radarr_download_queue(
        [movie_state()], "/movies", 7
    )
    assert result == radarr.MutationResult(attempted=1, failed_items=1)
    assert post.call_count == 3


def test_queue_rejected_response_is_not_retried(response_factory, monkeypatch):
    post = Mock(return_value=response_factory(status_code=500))
    monkeypatch.setattr(radarr.requests, "post", post)
    result = client_without_connection().add_to_radarr_download_queue(
        [movie_state()], "/movies", 7
    )
    assert result.failed_items == 1
    assert post.call_count == 1
