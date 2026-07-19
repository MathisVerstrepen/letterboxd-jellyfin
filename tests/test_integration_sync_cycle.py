import json
import os
from unittest.mock import Mock

import pytest

import main
import src.state_manager as state_manager
import src.sync as sync
from src.observability import ObservabilityService
from src.results import (
    LetterboxdDetailResult,
    MutationResult,
    PlayedMoviesResult,
    RadarrLookupResult,
    WatchlistEntry,
    WatchlistResult,
)


def config_for(*users):
    return {
        "jellyfin": {"url": "http://jellyfin.invalid", "api_key": "fake-jellyfin"},
        "radarr": {
            "url": "http://radarr.invalid",
            "api_key": "fake-radarr",
            "root_folder_path": "/movies",
            "quality_profile_id": 7,
        },
        "letterboxd": {
            "max_concurrent_requests": 1,
            "validate_proxies_on_startup": False,
        },
        "users": list(users),
    }


def user(username="alice"):
    return {
        "letterboxd_username": username,
        "jellyfin_username": f"{username}-viewer",
        "jellyfin_collection_id": "collection",
    }


def scrape(
    entries=(),
    *,
    cursor="film/new/",
    complete=True,
    outcome="success",
    failed=0,
    boundary=None,
):
    return WatchlistResult(
        list(entries),
        outcome,
        complete,
        cursor_uri=cursor,
        boundary_uri=boundary,
        failed_items=failed,
    )


def external_fakes(monkeypatch):
    jellyfin = Mock()
    jellyfin.get_movie_id.return_value = "jf-id"
    jellyfin.add_to_collection.return_value = MutationResult(attempted=1, succeeded=1)
    jellyfin.get_user_id.return_value = "user-id"
    jellyfin.get_played_movies_from_collection.return_value = PlayedMoviesResult([])
    jellyfin.remove_from_collection.return_value = MutationResult()
    radarr = Mock()
    radarr.check_radarr_state.return_value = RadarrLookupResult(
        {
            "hasFile": True,
            "monitored": True,
            "name": "Movie",
            "tmdbId": 101,
            "productionYear": 2020,
            "is_animation": False,
        }
    )
    radarr.add_to_radarr_download_queue.return_value = MutationResult(
        attempted=1, succeeded=1
    )
    jellyfin_constructor = Mock(return_value=jellyfin)
    radarr_constructor = Mock(return_value=radarr)
    monkeypatch.setattr(main, "Jellyfin", jellyfin_constructor)
    monkeypatch.setattr(main, "RadarrClient", radarr_constructor)
    return jellyfin, radarr, jellyfin_constructor, radarr_constructor


@pytest.mark.integration
def test_legacy_migration_and_completed_state_persist(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text('{"alice": "old"}', encoding="utf-8")
    monkeypatch.setattr(state_manager, "STATE_FILE_PATH", str(path))
    jellyfin, radarr, _, _ = external_fakes(monkeypatch)
    entry = WatchlistEntry("film/new/", LetterboxdDetailResult("movie", "101"))
    monkeypatch.setattr(
        sync,
        "get_new_watchlist_entries",
        Mock(return_value=scrape([entry], boundary="film/old/")),
    )
    observability = ObservabilityService("127.0.0.1", 0)

    outcome = main.run_sync_cycle(config_for(user()), observability, 1)

    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert outcome == "success"
    assert persisted["version"] == 2
    assert persisted["users"]["alice"]["cursor"] == {
        "kind": "letterboxd",
        "value": "film/new/",
    }
    assert persisted["users"]["alice"]["movies"]["film/new/"][
        "completion_reason"
    ] == "jellyfin_added"
    assert radarr.check_radarr_state.call_count == 1
    assert jellyfin.add_to_collection.call_count == 1


@pytest.mark.integration
def test_radarr_failure_retries_after_state_reload(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    monkeypatch.setattr(state_manager, "STATE_FILE_PATH", str(path))
    _, radarr, _, _ = external_fakes(monkeypatch)
    entry = WatchlistEntry("film/new/", LetterboxdDetailResult("movie", "101"))
    scrape_mock = Mock(side_effect=[scrape([entry]), scrape([], cursor="film/new/")])
    monkeypatch.setattr(sync, "get_new_watchlist_entries", scrape_mock)
    success = radarr.check_radarr_state.return_value
    radarr.check_radarr_state.side_effect = [RadarrLookupResult(None, 1), success]
    observability = ObservabilityService("127.0.0.1", 0)

    assert main.run_sync_cycle(config_for(user()), observability, 1) == "partial"
    first = json.loads(path.read_text(encoding="utf-8"))
    assert first["users"]["alice"]["movies"]["film/new/"]["status"] == "retry_radarr"
    assert main.run_sync_cycle(config_for(user()), observability, 2) == "success"
    second = json.loads(path.read_text(encoding="utf-8"))
    assert second["users"]["alice"]["movies"]["film/new/"][
        "completion_reason"
    ] == "jellyfin_added"
    assert radarr.check_radarr_state.call_count == 2


@pytest.mark.integration
def test_jellyfin_failure_retries_without_repeating_radarr(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    monkeypatch.setattr(state_manager, "STATE_FILE_PATH", str(path))
    jellyfin, radarr, _, _ = external_fakes(monkeypatch)
    entry = WatchlistEntry("film/new/", LetterboxdDetailResult("movie", "101"))
    monkeypatch.setattr(
        sync,
        "get_new_watchlist_entries",
        Mock(side_effect=[scrape([entry]), scrape([], cursor="film/new/")]),
    )
    jellyfin.add_to_collection.side_effect = [
        MutationResult(attempted=1, failed_items=1, fatal=True),
        MutationResult(attempted=1, succeeded=1),
    ]
    observability = ObservabilityService("127.0.0.1", 0)

    assert main.run_sync_cycle(config_for(user()), observability, 1) == "partial"
    assert main.run_sync_cycle(config_for(user()), observability, 2) == "success"
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["users"]["alice"]["movies"]["film/new/"][
        "completion_reason"
    ] == "jellyfin_added"
    assert radarr.check_radarr_state.call_count == 1
    assert jellyfin.add_to_collection.call_count == 2


@pytest.mark.integration
def test_partial_pagination_processes_known_entry_without_cursor_advance(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "users": {
                    "alice": {
                        "cursor": {"kind": "letterboxd", "value": "film/old/"},
                        "movies": {},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(state_manager, "STATE_FILE_PATH", str(path))
    external_fakes(monkeypatch)
    entry = WatchlistEntry("film/new/", LetterboxdDetailResult("movie", "101"))
    monkeypatch.setattr(
        sync,
        "get_new_watchlist_entries",
        Mock(return_value=scrape([entry], complete=False, outcome="partial", failed=1)),
    )
    outcome = main.run_sync_cycle(
        config_for(user()), ObservabilityService("127.0.0.1", 0), 1
    )
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert outcome == "partial"
    assert persisted["users"]["alice"]["cursor"]["value"] == "film/old/"
    assert "film/new/" in persisted["users"]["alice"]["movies"]


@pytest.mark.integration
def test_migration_save_failure_preserves_v1_and_stops_before_clients(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text('{"alice": "old"}', encoding="utf-8")
    monkeypatch.setattr(state_manager, "STATE_FILE_PATH", str(path))
    _, _, jellyfin_constructor, radarr_constructor = external_fakes(monkeypatch)
    monkeypatch.setattr(state_manager.os, "replace", Mock(side_effect=OSError("denied")))

    outcome = main.run_sync_cycle(
        config_for(user()), ObservabilityService("127.0.0.1", 0), 1
    )

    assert outcome == "failed"
    assert path.read_text(encoding="utf-8") == '{"alice": "old"}'
    jellyfin_constructor.assert_not_called()
    radarr_constructor.assert_not_called()


@pytest.mark.integration
def test_checkpoint_failure_stops_later_users_and_preserves_last_snapshot(
    tmp_path, monkeypatch
):
    path = tmp_path / "state.json"
    monkeypatch.setattr(state_manager, "STATE_FILE_PATH", str(path))
    _, radarr, _, _ = external_fakes(monkeypatch)
    entry = WatchlistEntry("film/new/", LetterboxdDetailResult("movie", "101"))
    scrape_mock = Mock(return_value=scrape([entry]))
    monkeypatch.setattr(sync, "get_new_watchlist_entries", scrape_mock)
    original_replace = os.replace
    replace_calls = 0

    def fail_second_replace(source, target):
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise OSError("denied")
        return original_replace(source, target)

    monkeypatch.setattr(state_manager.os, "replace", fail_second_replace)
    outcome = main.run_sync_cycle(
        config_for(user("alice"), user("bob")),
        ObservabilityService("127.0.0.1", 0),
        1,
    )

    assert outcome == "failed"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": 2,
        "users": {"alice": {"cursor": None, "movies": {}}},
    }
    assert scrape_mock.call_count == 1
    radarr.check_radarr_state.assert_not_called()
