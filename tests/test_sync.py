from copy import deepcopy
from unittest.mock import Mock, call

import pytest

import src.sync as sync
from src.results import (
    LetterboxdDetailResult,
    MutationResult,
    PlayedMoviesResult,
    RadarrLookupResult,
    StateSaveResult,
    WatchlistEntry,
    WatchlistResult,
)


def record(status, tmdb_id=None, title=None, year=None, reason=None):
    return {
        "tmdb_id": tmdb_id,
        "status": status,
        "title": title,
        "year": year,
        "completion_reason": reason,
    }


def movie_state(tmdb_id=1, *, has_file=True, animation=False):
    return {
        "hasFile": has_file,
        "monitored": False,
        "name": f"Movie {tmdb_id}",
        "tmdbId": tmdb_id,
        "productionYear": 2020,
        "is_animation": animation,
    }


def watchlist(entries=(), *, outcome="success", complete=True, cursor="film/a/", failed=0):
    return WatchlistResult(
        entries=list(entries),
        outcome=outcome,
        scan_complete=complete,
        cursor_uri=cursor,
        failed_items=failed,
    )


def build_manager(*, state=None, collection_id="collection", checkpoint=None):
    jellyfin = Mock()
    jellyfin.get_movie_id.return_value = "jf-id"
    jellyfin.add_to_collection.return_value = MutationResult(attempted=1, succeeded=1)
    jellyfin.get_user_id.return_value = "user-id"
    jellyfin.get_played_movies_from_collection.return_value = PlayedMoviesResult([])
    jellyfin.remove_from_collection.return_value = MutationResult()
    radarr = Mock()
    radarr.add_to_radarr_download_queue.return_value = MutationResult(
        attempted=1, succeeded=1
    )
    user_state = state or {"cursor": None, "movies": {}}
    checkpoint = checkpoint or Mock(return_value=StateSaveResult())
    manager = sync.SyncManager(
        {
            "letterboxd_username": "alice",
            "jellyfin_username": "viewer",
            "jellyfin_collection_id": collection_id,
        },
        jellyfin,
        radarr,
        user_state,
        checkpoint,
        {"max_concurrent_requests": 1, "validate_proxies_on_startup": False},
        {
            "root_folder_path": "/movies",
            "quality_profile_id": 7,
            "animated_movies": {"enabled": True, "root_folder_path": "/animated"},
        },
    )
    return manager, user_state, jellyfin, radarr, checkpoint


def set_watchlist(monkeypatch, result):
    scrape = Mock(return_value=result)
    monkeypatch.setattr(sync, "get_new_watchlist_entries", scrape)
    return scrape


def test_failed_scrape_is_counted_but_existing_retry_runs(monkeypatch):
    state = {
        "cursor": {"kind": "letterboxd", "value": "film/old/"},
        "movies": {"film/a/": record("retry_radarr", "1")},
    }
    manager, state, _, radarr, _ = build_manager(state=state)
    set_watchlist(
        monkeypatch,
        watchlist(outcome="failed", complete=False, cursor=None, failed=1),
    )
    radarr.check_radarr_state.return_value = RadarrLookupResult(movie_state())
    result = manager.run()
    assert result.completed
    assert result.failures_by_stage["letterboxd"] == 1
    assert state["movies"]["film/a/"]["completion_reason"] == "jellyfin_added"


def test_discovery_checkpoints_before_radarr_and_completes(monkeypatch):
    manager, state, _, radarr, checkpoint = build_manager()
    entry = WatchlistEntry("film/a/", LetterboxdDetailResult("movie", "1"))
    set_watchlist(monkeypatch, watchlist([entry]))
    snapshots = []
    checkpoint.side_effect = lambda: snapshots.append(deepcopy(state)) or StateSaveResult()
    radarr.check_radarr_state.return_value = RadarrLookupResult(movie_state())
    result = manager.run()
    assert result.completed
    assert snapshots[0]["cursor"] == {"kind": "letterboxd", "value": "film/a/"}
    assert snapshots[0]["movies"]["film/a/"]["status"] == "pending_radarr"
    assert state["movies"]["film/a/"]["completion_reason"] == "jellyfin_added"
    assert result.queue_counts == {
        "radarr_add": 1,
        "jellyfin_add": 1,
        "jellyfin_remove": 0,
    }


def test_detail_failure_is_persisted_without_immediate_retry(monkeypatch):
    manager, state, _, radarr, checkpoint = build_manager()
    entry = WatchlistEntry("film/a/", LetterboxdDetailResult("retry"))
    set_watchlist(monkeypatch, watchlist([entry], outcome="partial", failed=1))
    detail = Mock()
    monkeypatch.setattr(sync, "extract_tmdb_id_from_endpoint", detail)
    result = manager.run()
    assert result.completed
    assert state["movies"]["film/a/"] == record("retry_letterboxd")
    assert checkpoint.call_count == 1
    detail.assert_not_called()
    radarr.check_radarr_state.assert_not_called()


def test_persisted_letterboxd_retry_advances_on_later_run(monkeypatch):
    state = {
        "cursor": {"kind": "letterboxd", "value": "film/a/"},
        "movies": {"film/a/": record("retry_letterboxd")},
    }
    manager, state, _, radarr, checkpoint = build_manager(state=state)
    set_watchlist(monkeypatch, watchlist([], cursor="film/a/"))
    monkeypatch.setattr(
        sync,
        "extract_tmdb_id_from_endpoint",
        Mock(return_value=LetterboxdDetailResult("movie", "1")),
    )
    radarr.check_radarr_state.return_value = RadarrLookupResult(movie_state(has_file=False))
    manager.run()
    assert state["movies"]["film/a/"]["completion_reason"] == "radarr_no_file"
    assert checkpoint.call_count == 2


def test_radarr_lookup_and_queue_failures_remain_retryable(monkeypatch):
    state = {
        "cursor": {"kind": "letterboxd", "value": "film/a/"},
        "movies": {
            "film/a/": record("pending_radarr", "1"),
            "film/b/": record("pending_radarr", "2"),
        },
    }
    manager, state, _, radarr, _ = build_manager(state=state)
    set_watchlist(monkeypatch, watchlist([], cursor="film/a/"))
    radarr.check_radarr_state.side_effect = [
        RadarrLookupResult(None, 1),
        RadarrLookupResult(movie_state(2)),
    ]
    radarr.add_to_radarr_download_queue.return_value = MutationResult(
        attempted=1, failed_items=1
    )
    result = manager.run()
    assert result.failures_by_stage["radarr"] == 2
    assert state["movies"]["film/a/"]["status"] == "retry_radarr"
    assert state["movies"]["film/b/"]["status"] == "retry_radarr"


@pytest.mark.parametrize(
    ("has_file", "collection_id", "reason"),
    [(False, "collection", "radarr_no_file"), (True, None, "collection_disabled")],
)
def test_radarr_terminal_outcomes(monkeypatch, has_file, collection_id, reason):
    state = {
        "cursor": None,
        "movies": {"film/a/": record("pending_radarr", "1")},
    }
    manager, state, jellyfin, radarr, _ = build_manager(
        state=state, collection_id=collection_id
    )
    set_watchlist(monkeypatch, watchlist([], cursor=None))
    radarr.check_radarr_state.return_value = RadarrLookupResult(
        movie_state(has_file=has_file)
    )
    manager.run()
    assert state["movies"]["film/a/"]["completion_reason"] == reason
    jellyfin.get_movie_id.assert_not_called()


def test_jellyfin_lookup_failure_retries_without_radarr(monkeypatch):
    state = {
        "cursor": None,
        "movies": {"film/a/": record("pending_jellyfin", "1", "Movie", 2020)},
    }
    manager, state, jellyfin, radarr, _ = build_manager(state=state)
    set_watchlist(monkeypatch, watchlist([], cursor=None))
    jellyfin.get_movie_id.side_effect = RuntimeError("down")
    result = manager.run()
    assert result.failures_by_stage["jellyfin"] == 1
    assert state["movies"]["film/a/"]["status"] == "retry_jellyfin"
    radarr.check_radarr_state.assert_not_called()


def test_jellyfin_not_found_is_terminal(monkeypatch):
    state = {
        "cursor": None,
        "movies": {"film/a/": record("retry_jellyfin", "1", "Movie", 2020)},
    }
    manager, state, jellyfin, _, _ = build_manager(state=state)
    set_watchlist(monkeypatch, watchlist([], cursor=None))
    jellyfin.get_movie_id.return_value = None
    manager.run()
    assert state["movies"]["film/a/"]["completion_reason"] == "jellyfin_not_found"


def test_rediscovery_does_not_downgrade_completed_record(monkeypatch):
    completed = record("completed", "1", "Movie", 2020, "jellyfin_added")
    state = {"cursor": None, "movies": {"film/a/": deepcopy(completed)}}
    manager, state, jellyfin, radarr, _ = build_manager(state=state)
    entry = WatchlistEntry("film/a/", LetterboxdDetailResult("movie", "1"))
    set_watchlist(monkeypatch, watchlist([entry]))
    manager.run()
    assert state["movies"]["film/a/"] == completed
    radarr.check_radarr_state.assert_not_called()
    jellyfin.add_to_collection.assert_not_called()


def test_checkpoint_failure_stops_before_remote_side_effect(monkeypatch):
    checkpoint = Mock(return_value=StateSaveResult(failed_items=1))
    manager, _, jellyfin, radarr, _ = build_manager(checkpoint=checkpoint)
    entry = WatchlistEntry("film/a/", LetterboxdDetailResult("movie", "1"))
    set_watchlist(monkeypatch, watchlist([entry]))
    result = manager.run()
    assert not result.completed
    assert result.failures_by_stage["state"] == 1
    radarr.check_radarr_state.assert_not_called()
    jellyfin.get_user_id.assert_not_called()


def test_watched_removal_still_runs_on_no_change(monkeypatch):
    manager, _, jellyfin, _, _ = build_manager()
    scrape = set_watchlist(monkeypatch, watchlist([], cursor=None))
    jellyfin.get_played_movies_from_collection.return_value = PlayedMoviesResult(["played"])
    jellyfin.remove_from_collection.return_value = MutationResult(attempted=1, succeeded=1)
    result = manager.run()
    assert result.completed
    assert result.queue_counts["jellyfin_remove"] == 1
    scrape.assert_called_once_with("alice", manager.proxy_manager, 1, None)
    jellyfin.remove_from_collection.assert_called_once_with(["played"], "collection")


def test_animated_movie_uses_animated_root(monkeypatch):
    state = {"cursor": None, "movies": {"film/a/": record("pending_radarr", "1")}}
    manager, _, _, radarr, _ = build_manager(state=state)
    set_watchlist(monkeypatch, watchlist([], cursor=None))
    movie = movie_state(animation=True)
    radarr.check_radarr_state.return_value = RadarrLookupResult(movie)
    manager.run()
    assert radarr.add_to_radarr_download_queue.call_args == call([movie], "/animated", 7)
