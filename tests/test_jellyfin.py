from unittest.mock import Mock

import pytest
import requests

import src.jellyfin as jellyfin
from src.exceptions import JellyfinException
from src.results import MutationResult, PlayedMoviesResult


def client_without_connection():
    client = jellyfin.Jellyfin.__new__(jellyfin.Jellyfin)
    client.base_url = "http://jellyfin.invalid"
    client.headers = {"Authorization": 'MediaBrowser Token="fake-key"'}
    client._movie_cache = None
    client.logger = Mock()
    return client


def test_constructor_normalizes_url_and_connects(response_factory, monkeypatch):
    get = Mock(return_value=response_factory(status_code=200))
    monkeypatch.setattr(jellyfin.requests, "get", get)
    client = jellyfin.Jellyfin("http://jellyfin.invalid/", "fake-key")
    assert client.base_url == "http://jellyfin.invalid"
    get.assert_called_once_with(
        "http://jellyfin.invalid/System/Info",
        headers={"Authorization": 'MediaBrowser Token="fake-key"'},
        timeout=10,
    )


@pytest.mark.parametrize("request_error", [False, True])
def test_constructor_connection_failure(request_error, response_factory, monkeypatch):
    get = Mock(return_value=response_factory(status_code=500))
    if request_error:
        get.side_effect = requests.exceptions.ConnectionError("failed")
    monkeypatch.setattr(jellyfin.requests, "get", get)
    with pytest.raises(JellyfinException):
        jellyfin.Jellyfin("http://jellyfin.invalid", "fake-key")


def test_movies_load_transform_and_cache(response_factory, monkeypatch):
    data = {
        "Items": [
            {
                "Id": "jf-1",
                "Name": "Movie",
                "ProductionYear": 2020,
                "People": [
                    {"Name": "Director", "Type": "Director"},
                    {"Name": "Actor", "Type": "Actor"},
                ],
            }
        ]
    }
    get = Mock(return_value=response_factory(status_code=200, json_data=data))
    monkeypatch.setattr(jellyfin.requests, "get", get)
    client = client_without_connection()
    assert client.get_movie_id("Movie", 2020) == "jf-1"
    assert client.get_movie_id("Missing", 2020) is None
    assert get.call_count == 1
    assert data["Items"][0]["Directors"] == [{"Name": "Director", "Type": "Director"}]


def test_movie_loading_http_failure(response_factory, monkeypatch):
    monkeypatch.setattr(
        jellyfin.requests, "get", Mock(return_value=response_factory(status_code=503))
    )
    with pytest.raises(JellyfinException):
        client_without_connection().get_movies()


def test_add_collection_empty_does_not_request(monkeypatch):
    post = Mock()
    monkeypatch.setattr(jellyfin.requests, "post", post)
    assert client_without_connection().add_to_collection([], "collection") == MutationResult()
    post.assert_not_called()


def test_add_collection_batches_at_fifty(response_factory, monkeypatch):
    post = Mock(return_value=response_factory(status_code=204))
    monkeypatch.setattr(jellyfin.requests, "post", post)
    movie_ids = [str(index) for index in range(51)]
    result = client_without_connection().add_to_collection(movie_ids, "collection")
    assert result == MutationResult(attempted=51, succeeded=51)
    assert post.call_count == 2
    assert len(post.call_args_list[0].kwargs["params"]["ids"].split(",")) == 50
    assert post.call_args_list[1].kwargs["params"] == {"ids": "50"}


@pytest.mark.parametrize("request_error", [False, True])
def test_add_collection_failure_is_fatal(request_error, response_factory, monkeypatch):
    post = Mock(return_value=response_factory(status_code=500))
    if request_error:
        post.side_effect = requests.exceptions.ConnectionError("failed")
    monkeypatch.setattr(jellyfin.requests, "post", post)
    result = client_without_connection().add_to_collection(["one"], "collection")
    assert result == MutationResult(attempted=1, failed_items=1, fatal=True)


@pytest.mark.parametrize(
    ("users", "username", "expected"),
    [([{"Name": "alice", "Id": "user-1"}], "alice", "user-1"), ([], "alice", None)],
)
def test_get_user_id_hit_or_miss(users, username, expected, response_factory, monkeypatch):
    monkeypatch.setattr(
        jellyfin.requests,
        "get",
        Mock(return_value=response_factory(status_code=200, json_data=users)),
    )
    assert client_without_connection().get_user_id(username) == expected


def test_get_user_id_http_failure(response_factory, monkeypatch):
    monkeypatch.setattr(
        jellyfin.requests, "get", Mock(return_value=response_factory(status_code=500))
    )
    with pytest.raises(JellyfinException):
        client_without_connection().get_user_id("alice")


def test_played_movies_success(response_factory, monkeypatch):
    response = response_factory(
        status_code=200,
        json_data={"Items": [{"Id": "one"}, {"Id": "two"}, {}]},
    )
    monkeypatch.setattr(jellyfin.requests, "get", Mock(return_value=response))
    result = client_without_connection().get_played_movies_from_collection(
        "collection", "user"
    )
    assert result == PlayedMoviesResult(movie_ids=["one", "two"])


@pytest.mark.parametrize(
    ("response_builder", "fatal"),
    [
        (lambda response: response(status_code=500), False),
        (
            lambda response: response(
                status_code=200,
                json_error=requests.exceptions.JSONDecodeError("bad", "doc", 0),
            ),
            True,
        ),
        (lambda response: response(status_code=200, json_data=[]), True),
        (lambda response: response(status_code=200, json_data={"Items": {}}), True),
    ],
)
def test_played_movies_response_failures(
    response_builder, fatal, response_factory, monkeypatch
):
    monkeypatch.setattr(
        jellyfin.requests, "get", Mock(return_value=response_builder(response_factory))
    )
    result = client_without_connection().get_played_movies_from_collection(
        "collection", "user"
    )
    assert result == PlayedMoviesResult(movie_ids=[], failed_items=1, fatal=fatal)


def test_played_movies_request_failure_is_fatal(monkeypatch):
    monkeypatch.setattr(
        jellyfin.requests,
        "get",
        Mock(side_effect=requests.exceptions.ConnectionError("failed")),
    )
    result = client_without_connection().get_played_movies_from_collection(
        "collection", "user"
    )
    assert result == PlayedMoviesResult(movie_ids=[], failed_items=1, fatal=True)


def test_remove_collection_empty_does_not_request(monkeypatch):
    delete = Mock()
    monkeypatch.setattr(jellyfin.requests, "delete", delete)
    assert client_without_connection().remove_from_collection([], "collection") == MutationResult()
    delete.assert_not_called()


def test_remove_collection_success(response_factory, monkeypatch):
    delete = Mock(return_value=response_factory(status_code=204))
    monkeypatch.setattr(jellyfin.requests, "delete", delete)
    result = client_without_connection().remove_from_collection(["one", "two"], "collection")
    assert result == MutationResult(attempted=2, succeeded=2)
    assert delete.call_args.kwargs["params"] == {"ids": "one,two"}


@pytest.mark.parametrize("request_error", [False, True])
def test_remove_collection_failure_is_fatal(request_error, response_factory, monkeypatch):
    delete = Mock(return_value=response_factory(status_code=500))
    if request_error:
        delete.side_effect = requests.exceptions.ConnectionError("failed")
    monkeypatch.setattr(jellyfin.requests, "delete", delete)
    result = client_without_connection().remove_from_collection(["one"], "collection")
    assert result == MutationResult(attempted=1, failed_items=1, fatal=True)
