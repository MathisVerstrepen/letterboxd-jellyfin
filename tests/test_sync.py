from unittest.mock import Mock, call

import pytest

import src.sync as sync
from src.results import (
    MutationResult,
    PlayedMoviesResult,
    RadarrLookupResult,
    WatchlistResult,
)


def movie_state(
    tmdb_id: int = 1,
    *,
    has_file: bool = False,
    animation: bool = False,
):
    return {
        "hasFile": has_file,
        "monitored": False,
        "name": f"Movie {tmdb_id}",
        "tmdbId": tmdb_id,
        "productionYear": 2020,
        "is_animation": animation,
    }


def build_manager(
    *,
    jellyfin_username="viewer",
    collection_id="collection",
    radarr_config=None,
):
    jellyfin = Mock()
    jellyfin.get_movie_id.side_effect = lambda name, year: f"jf-{name.split()[-1]}"
    jellyfin.add_to_collection.return_value = MutationResult()
    jellyfin.get_user_id.return_value = "user-id"
    jellyfin.get_played_movies_from_collection.return_value = PlayedMoviesResult([])
    jellyfin.remove_from_collection.return_value = MutationResult()

    radarr_client = Mock()
    radarr_client.add_to_radarr_download_queue.return_value = MutationResult(
        attempted=1, succeeded=1
    )

    config = {
        "root_folder_path": "/movies",
        "quality_profile_id": 7,
        "animated_movies": {"enabled": True, "root_folder_path": "/animated"},
    }
    if radarr_config:
        config.update(radarr_config)
    manager = sync.SyncManager(
        {
            "letterboxd_username": "alice",
            "jellyfin_username": jellyfin_username,
            "jellyfin_collection_id": collection_id,
        },
        jellyfin,
        radarr_client,
        "old-id",
        {"max_concurrent_requests": 1, "validate_proxies_on_startup": False},
        config,
    )
    return manager, jellyfin, radarr_client


def set_watchlist(monkeypatch, ids, failed=0, skipped=0):
    scrape = Mock(return_value=WatchlistResult(ids, failed, skipped))
    monkeypatch.setattr(sync, "get_new_watchlist_tmdb_ids", scrape)
    return scrape


def test_missing_jellyfin_username_stops_before_scrape(monkeypatch):
    manager, jellyfin, radarr_client = build_manager(jellyfin_username=None)
    scrape = set_watchlist(monkeypatch, ["1"])
    result = manager.run()
    assert not result.completed
    assert result.failures_by_stage["configuration"] == 1
    scrape.assert_not_called()
    radarr_client.check_radarr_state.assert_not_called()
    jellyfin.get_user_id.assert_not_called()


def test_no_new_entries_still_removes_watched_movies(monkeypatch):
    manager, jellyfin, radarr_client = build_manager()
    scrape = set_watchlist(monkeypatch, [])
    jellyfin.get_played_movies_from_collection.return_value = PlayedMoviesResult(["played"])
    jellyfin.remove_from_collection.return_value = MutationResult(attempted=1, succeeded=1)
    result = manager.run()
    assert result.completed
    assert result.state_advance_id is None
    assert result.queue_counts["jellyfin_remove"] == 1
    scrape.assert_called_once_with("alice", manager.proxy_manager, 1, "old-id")
    radarr_client.check_radarr_state.assert_not_called()
    jellyfin.remove_from_collection.assert_called_once_with(["played"], "collection")


def test_lookup_failure_is_counted_and_skips_queue(monkeypatch):
    manager, jellyfin, radarr_client = build_manager()
    set_watchlist(monkeypatch, ["10"])
    radarr_client.check_radarr_state.return_value = RadarrLookupResult(None, failed_items=1)
    result = manager.run()
    assert result.completed
    assert result.state_advance_id == "10"
    assert result.failures_by_stage["radarr"] == 1
    assert result.queue_counts["radarr_add"] == 0
    radarr_client.add_to_radarr_download_queue.assert_not_called()


def test_default_and_animated_roots_and_queue_results(monkeypatch):
    manager, jellyfin, radarr_client = build_manager()
    set_watchlist(monkeypatch, ["1", "2"])
    states = [movie_state(1), movie_state(2, animation=True)]
    radarr_client.check_radarr_state.side_effect = [
        RadarrLookupResult(states[0]),
        RadarrLookupResult(states[1]),
    ]
    radarr_client.add_to_radarr_download_queue.side_effect = [
        MutationResult(attempted=1, succeeded=1),
        MutationResult(attempted=1, failed_items=1),
    ]
    result = manager.run()
    assert result.completed
    assert result.state_advance_id == "1"
    assert result.queue_counts["radarr_add"] == 2
    assert result.failures_by_stage["radarr"] == 1
    assert radarr_client.add_to_radarr_download_queue.call_args_list == [
        call([states[0]], "/movies", 7),
        call([states[1]], "/animated", 7),
    ]


def test_only_available_movies_are_added_to_jellyfin(monkeypatch):
    manager, jellyfin, radarr_client = build_manager()
    set_watchlist(monkeypatch, ["1", "2"])
    radarr_client.check_radarr_state.side_effect = [
        RadarrLookupResult(movie_state(1, has_file=False)),
        RadarrLookupResult(movie_state(2, has_file=True)),
    ]
    jellyfin.add_to_collection.return_value = MutationResult(attempted=1, succeeded=1)
    result = manager.run()
    assert result.completed
    jellyfin.get_movie_id.assert_called_once_with("Movie 2", 2020)
    jellyfin.add_to_collection.assert_called_once_with(["jf-2"], "collection")
    assert result.queue_counts["jellyfin_add"] == 1


def test_absent_collection_skips_all_collection_work_and_advances(monkeypatch):
    manager, jellyfin, radarr_client = build_manager(collection_id=None)
    set_watchlist(monkeypatch, ["1"])
    radarr_client.check_radarr_state.return_value = RadarrLookupResult(
        movie_state(1, has_file=True)
    )
    result = manager.run()
    assert result.completed
    assert result.state_advance_id == "1"
    jellyfin.get_movie_id.assert_not_called()
    jellyfin.add_to_collection.assert_not_called()
    jellyfin.get_user_id.assert_not_called()


def test_fatal_collection_add_stops_without_state_advance(monkeypatch):
    manager, jellyfin, radarr_client = build_manager()
    set_watchlist(monkeypatch, ["1"])
    radarr_client.check_radarr_state.return_value = RadarrLookupResult(
        movie_state(1, has_file=True)
    )
    jellyfin.add_to_collection.return_value = MutationResult(
        attempted=1, failed_items=1, fatal=True
    )
    result = manager.run()
    assert not result.completed
    assert result.state_advance_id is None
    assert result.failures_by_stage["jellyfin"] == 1
    jellyfin.get_user_id.assert_not_called()


def test_missing_jellyfin_user_is_nonfatal_completion(monkeypatch):
    manager, jellyfin, radarr_client = build_manager()
    set_watchlist(monkeypatch, ["1"])
    radarr_client.check_radarr_state.return_value = RadarrLookupResult(movie_state(1))
    jellyfin.get_user_id.return_value = None
    result = manager.run()
    assert result.completed
    assert result.state_advance_id == "1"
    assert result.failures_by_stage["jellyfin"] == 1
    jellyfin.get_played_movies_from_collection.assert_not_called()


@pytest.mark.parametrize("operation", ["movie_lookup", "user_lookup"])
def test_lookup_exceptions_are_fatal(operation, monkeypatch):
    manager, jellyfin, radarr_client = build_manager()
    set_watchlist(monkeypatch, ["1"])
    radarr_client.check_radarr_state.return_value = RadarrLookupResult(
        movie_state(1, has_file=True)
    )
    if operation == "movie_lookup":
        jellyfin.get_movie_id.side_effect = RuntimeError("failed")
    else:
        jellyfin.get_user_id.side_effect = RuntimeError("failed")
    result = manager.run()
    assert not result.completed
    assert result.failures_by_stage["jellyfin"] == 1


@pytest.mark.parametrize("operation", ["played", "remove"])
def test_fatal_watched_operations_stop_completion(operation, monkeypatch):
    manager, jellyfin, radarr_client = build_manager()
    set_watchlist(monkeypatch, [])
    if operation == "played":
        jellyfin.get_played_movies_from_collection.return_value = PlayedMoviesResult(
            [], failed_items=1, fatal=True
        )
    else:
        jellyfin.get_played_movies_from_collection.return_value = PlayedMoviesResult(["one"])
        jellyfin.remove_from_collection.return_value = MutationResult(
            attempted=1, failed_items=1, fatal=True
        )
    result = manager.run()
    assert not result.completed
    assert result.failures_by_stage["jellyfin"] == 1


def test_nonfatal_played_failure_completes_partial(monkeypatch):
    manager, jellyfin, radarr_client = build_manager()
    set_watchlist(monkeypatch, [], failed=1)
    jellyfin.get_played_movies_from_collection.return_value = PlayedMoviesResult(
        [], failed_items=1, fatal=False
    )
    result = manager.run()
    assert result.completed
    assert result.failures_by_stage["letterboxd"] == 1
    assert result.failures_by_stage["jellyfin"] == 1
