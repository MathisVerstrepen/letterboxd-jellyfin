import json
from unittest.mock import Mock

import pytest

import main
import src.state_manager as state_manager
import src.sync as sync
from src.observability import ObservabilityService
from src.results import (
    MutationResult,
    PlayedMoviesResult,
    RadarrLookupResult,
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


def user(letterboxd_username="alice"):
    return {
        "letterboxd_username": letterboxd_username,
        "jellyfin_username": f"{letterboxd_username}-viewer",
        "jellyfin_collection_id": "collection",
    }


def external_fakes(monkeypatch):
    jellyfin = Mock()
    jellyfin.get_movie_id.side_effect = lambda name, year: f"jf-{name}"
    jellyfin.add_to_collection.return_value = MutationResult(attempted=1, succeeded=1)
    jellyfin.get_user_id.side_effect = lambda username: f"id-{username}"
    jellyfin.get_played_movies_from_collection.return_value = PlayedMoviesResult([])
    jellyfin.remove_from_collection.return_value = MutationResult()

    radarr = Mock()
    radarr.check_radarr_state.side_effect = lambda tmdb_id: RadarrLookupResult(
        {
            "hasFile": True,
            "monitored": True,
            "name": f"Movie-{tmdb_id}",
            "tmdbId": int(tmdb_id),
            "productionYear": 2020,
            "is_animation": False,
        }
    )
    radarr.add_to_radarr_download_queue.return_value = MutationResult(
        attempted=1, succeeded=1
    )
    monkeypatch.setattr(main, "Jellyfin", Mock(return_value=jellyfin))
    monkeypatch.setattr(main, "RadarrClient", Mock(return_value=radarr))
    return jellyfin, radarr


@pytest.mark.integration
def test_successful_multi_user_cycle_persists_state_and_observability(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    monkeypatch.setattr(state_manager, "STATE_FILE_PATH", str(path))
    jellyfin, radarr = external_fakes(monkeypatch)
    scrape = Mock(
        side_effect=lambda username, *args: WatchlistResult(
            ["101"] if username == "alice" else ["202"]
        )
    )
    monkeypatch.setattr(sync, "get_new_watchlist_tmdb_ids", scrape)
    observability = ObservabilityService("127.0.0.1", 0)

    outcome = main.run_sync_cycle(
        config_for(user("alice"), user("bob")), observability, 1
    )

    assert outcome == "success"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "alice": "101",
        "bob": "202",
    }
    assert radarr.check_radarr_state.call_count == 2
    assert jellyfin.add_to_collection.call_count == 2
    snapshot = observability.snapshot()
    assert snapshot["ready"] is True
    assert snapshot["last_run"]["outcome"] == "success"
    assert snapshot["last_run"]["queue_counts"] == {
        "radarr_add": 2,
        "jellyfin_add": 2,
        "jellyfin_remove": 0,
    }


@pytest.mark.integration
def test_nonfatal_downstream_failure_is_partial_and_advances_state(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text('{"alice": "old"}', encoding="utf-8")
    monkeypatch.setattr(state_manager, "STATE_FILE_PATH", str(path))
    jellyfin, _ = external_fakes(monkeypatch)
    jellyfin.get_played_movies_from_collection.return_value = PlayedMoviesResult(
        [], failed_items=1, fatal=False
    )
    monkeypatch.setattr(
        sync,
        "get_new_watchlist_tmdb_ids",
        Mock(return_value=WatchlistResult(["101"])),
    )
    observability = ObservabilityService("127.0.0.1", 0)

    outcome = main.run_sync_cycle(config_for(user()), observability, 2)

    assert outcome == "partial"
    assert json.loads(path.read_text(encoding="utf-8")) == {"alice": "101"}
    snapshot = observability.snapshot()
    assert snapshot["ready"] is False
    assert snapshot["last_run"]["failures_by_stage"]["jellyfin"] == 1


@pytest.mark.integration
def test_fatal_user_mutation_fails_and_preserves_old_state(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text('{"alice": "old"}', encoding="utf-8")
    monkeypatch.setattr(state_manager, "STATE_FILE_PATH", str(path))
    jellyfin, _ = external_fakes(monkeypatch)
    jellyfin.add_to_collection.return_value = MutationResult(
        attempted=1, failed_items=1, fatal=True
    )
    monkeypatch.setattr(
        sync,
        "get_new_watchlist_tmdb_ids",
        Mock(return_value=WatchlistResult(["101"])),
    )
    observability = ObservabilityService("127.0.0.1", 0)

    outcome = main.run_sync_cycle(config_for(user()), observability, 3)

    assert outcome == "failed"
    assert json.loads(path.read_text(encoding="utf-8")) == {"alice": "old"}
    assert jellyfin.get_user_id.call_count == 0
    snapshot = observability.snapshot()
    assert snapshot["last_run"]["outcome"] == "failed"
    assert snapshot["last_run"]["failures_by_stage"]["jellyfin"] == 1
