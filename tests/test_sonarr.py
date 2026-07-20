from unittest.mock import Mock, call

import pytest
import requests

import src.sonarr as sonarr


def resource(tmdb_id=10, genres=None):
    value = {
        "id": 99,
        "title": "Series",
        "year": 2024,
        "tvdbId": 20,
        "tmdbId": tmdb_id,
        "titleSlug": "series",
        "seriesType": "standard",
        "seasons": [{"seasonNumber": 1, "monitored": False}],
    }
    if genres is not None:
        value["genres"] = genres
    return value


def client(monkeypatch, response_factory):
    get = Mock(return_value=response_factory(status_code=200))
    monkeypatch.setattr(sonarr.requests, "get", get)
    instance = sonarr.SonarrClient("http://sonarr.invalid/", "secret", timeout=12)
    return instance, get


def test_constructor_uses_v3_key_and_timeout(monkeypatch, response_factory):
    instance, get = client(monkeypatch, response_factory)
    assert instance.base_url == "http://sonarr.invalid/api/v3"
    assert get.call_args == call(
        "http://sonarr.invalid/api/v3/system/status",
        headers={"X-Api-Key": "secret"},
        timeout=12,
    )


def test_exact_lookup_is_cached_and_mismatch_fails(monkeypatch, response_factory):
    instance, get = client(monkeypatch, response_factory)
    get.side_effect = [
        response_factory(headers={"Content-Type": "application/json"}, json_data=[]),
        response_factory(
            headers={"Content-Type": "application/json"},
            json_data=[resource(11), resource(10)],
        ),
    ]
    first = instance.check_sonarr_state("10")
    second = instance.check_sonarr_state("10")
    assert first.resource == {**resource(10), "is_animation": False}
    assert second == first
    assert get.call_count == 3


def test_add_payload_monitors_all_and_updates_inventory(
    monkeypatch, response_factory
):
    instance, _ = client(monkeypatch, response_factory)
    post = Mock(return_value=response_factory(status_code=201))
    monkeypatch.setattr(sonarr.requests, "post", post)
    lookup_resource = {**resource(genres=["Animation"]), "is_animation": True}
    result = instance.add_to_sonarr_download_queue(lookup_resource, "/series", 4)
    body = post.call_args.kwargs["json"]
    assert result.succeeded == 1
    assert "id" not in body
    assert "is_animation" not in body
    assert body["rootFolderPath"] == "/series"
    assert body["qualityProfileId"] == 4
    assert body["monitorNewItems"] == "all"
    assert body["seasons"][0]["monitored"] is True
    assert body["addOptions"] == {
        "monitor": "all",
        "searchForMissingEpisodes": True,
    }
    assert instance.check_sonarr_state("10").installed


@pytest.mark.parametrize(
    ("genres", "expected"),
    [
        (["Animation"], True),
        (["Drama", "Animation"], True),
        (None, False),
        ([], False),
        (["animation"], False),
        (["Animation & Anime"], False),
        ("Animation", False),
        (("Animation",), False),
    ],
)
def test_lookup_animation_classification_uses_exact_list_element(genres, expected):
    mapped = sonarr.SonarrClient._valid_resource(resource(genres=genres), "10")
    assert mapped is not None
    assert mapped["is_animation"] is expected


def test_non_2xx_is_verified_by_forced_inventory(monkeypatch, response_factory):
    instance, get = client(monkeypatch, response_factory)
    monkeypatch.setattr(
        sonarr.requests, "post", Mock(return_value=response_factory(status_code=400))
    )
    get.return_value = response_factory(
        headers={"Content-Type": "application/json"}, json_data=[resource()]
    )
    result = instance.add_to_sonarr_download_queue(resource(), "/series", 4)
    assert (result.attempted, result.succeeded, result.failed_items) == (1, 1, 0)


def test_request_retries_count_each_post(monkeypatch, response_factory):
    instance, get = client(monkeypatch, response_factory)
    post = Mock(
        side_effect=[
            requests.RequestException(),
            requests.RequestException(),
            requests.RequestException(),
        ]
    )
    monkeypatch.setattr(sonarr.requests, "post", post)
    monkeypatch.setattr(sonarr.time, "sleep", Mock())
    get.return_value = response_factory(
        headers={"Content-Type": "application/json"}, json_data=[]
    )
    result = instance.add_to_sonarr_download_queue(resource(), "/series", 4)
    assert (result.attempted, result.failed_items) == (3, 1)
