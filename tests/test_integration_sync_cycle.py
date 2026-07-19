import json
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
    StateSaveResult,
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


def use_state_paths(tmp_path, monkeypatch):
    database = tmp_path / "state.db"
    source = tmp_path / "state.json"
    real_store = state_manager.SQLiteStateStore
    monkeypatch.setattr(
        main,
        "SQLiteStateStore",
        lambda: real_store(str(database), str(source)),
    )
    return database, source


def load_user(database, source, username="alice"):
    store = state_manager.SQLiteStateStore(str(database), str(source))
    assert store.initialize().failed_items == 0
    result = store.load_or_create_user(username)
    assert result.failed_items == 0
    assert store.close().failed_items == 0
    return result.data


@pytest.mark.integration
@pytest.mark.parametrize("legacy", [{"alice": "old"}, {"version": 2, "users": {}}])
def test_json_migration_and_completed_state_persist(tmp_path, monkeypatch, legacy):
    database, source = use_state_paths(tmp_path, monkeypatch)
    original = json.dumps(legacy)
    source.write_text(original, encoding="utf-8")
    jellyfin, radarr, _, _ = external_fakes(monkeypatch)
    entry = WatchlistEntry("film/new/", LetterboxdDetailResult("movie", "101"))
    monkeypatch.setattr(
        sync,
        "get_new_watchlist_entries",
        Mock(return_value=scrape([entry], boundary="film/old/")),
    )

    outcome = main.run_sync_cycle(
        config_for(user()), ObservabilityService("127.0.0.1", 0), 1
    )

    persisted = load_user(database, source)
    assert outcome == "success"
    assert persisted["cursor"] == {"kind": "letterboxd", "value": "film/new/"}
    assert persisted["movies"]["film/new/"]["completion_reason"] == "jellyfin_added"
    assert source.read_text(encoding="utf-8") == original
    assert radarr.check_radarr_state.call_count == 1
    assert jellyfin.add_to_collection.call_count == 1


@pytest.mark.integration
def test_radarr_failure_retries_after_connection_reopen(tmp_path, monkeypatch):
    database, source = use_state_paths(tmp_path, monkeypatch)
    _, radarr, _, _ = external_fakes(monkeypatch)
    entry = WatchlistEntry("film/new/", LetterboxdDetailResult("movie", "101"))
    monkeypatch.setattr(
        sync,
        "get_new_watchlist_entries",
        Mock(side_effect=[scrape([entry]), scrape([], cursor="film/new/")]),
    )
    success = radarr.check_radarr_state.return_value
    radarr.check_radarr_state.side_effect = [RadarrLookupResult(None, 1), success]
    observability = ObservabilityService("127.0.0.1", 0)

    assert main.run_sync_cycle(config_for(user()), observability, 1) == "partial"
    assert load_user(database, source)["movies"]["film/new/"]["status"] == "retry_radarr"
    assert main.run_sync_cycle(config_for(user()), observability, 2) == "success"
    assert (
        load_user(database, source)["movies"]["film/new/"]["completion_reason"]
        == "jellyfin_added"
    )
    assert radarr.check_radarr_state.call_count == 2


@pytest.mark.integration
def test_jellyfin_failure_retries_without_repeating_radarr(tmp_path, monkeypatch):
    database, source = use_state_paths(tmp_path, monkeypatch)
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
    persisted = load_user(database, source)
    assert persisted["movies"]["film/new/"]["completion_reason"] == "jellyfin_added"
    assert radarr.check_radarr_state.call_count == 1
    assert jellyfin.add_to_collection.call_count == 2


@pytest.mark.integration
def test_partial_pagination_processes_entry_without_cursor_advance(tmp_path, monkeypatch):
    database, source = use_state_paths(tmp_path, monkeypatch)
    source.write_text(
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
    persisted = load_user(database, source)
    assert outcome == "partial"
    assert persisted["cursor"]["value"] == "film/old/"
    assert "film/new/" in persisted["movies"]


@pytest.mark.integration
def test_import_install_failure_preserves_json_and_stops_before_clients(tmp_path, monkeypatch):
    database, source = use_state_paths(tmp_path, monkeypatch)
    source.write_text('{"alice": "old"}', encoding="utf-8")
    _, _, jellyfin_constructor, radarr_constructor = external_fakes(monkeypatch)
    monkeypatch.setattr(state_manager.os, "replace", Mock(side_effect=OSError("denied")))

    outcome = main.run_sync_cycle(
        config_for(user()), ObservabilityService("127.0.0.1", 0), 1
    )

    assert outcome == "failed"
    assert source.read_text(encoding="utf-8") == '{"alice": "old"}'
    assert not database.exists()
    jellyfin_constructor.assert_not_called()
    radarr_constructor.assert_not_called()


@pytest.mark.integration
def test_invalid_existing_database_stops_before_clients(tmp_path, monkeypatch):
    database, source = use_state_paths(tmp_path, monkeypatch)
    database.write_text("not sqlite", encoding="utf-8")
    source.write_text('{"alice": "old"}', encoding="utf-8")
    _, _, jellyfin_constructor, radarr_constructor = external_fakes(monkeypatch)

    outcome = main.run_sync_cycle(
        config_for(user()), ObservabilityService("127.0.0.1", 0), 1
    )

    assert outcome == "failed"
    assert database.read_text(encoding="utf-8") == "not sqlite"
    jellyfin_constructor.assert_not_called()
    radarr_constructor.assert_not_called()


@pytest.mark.integration
def test_checkpoint_failure_stops_later_users_and_preserves_last_commit(
    tmp_path, monkeypatch
):
    database, source = use_state_paths(tmp_path, monkeypatch)
    _, radarr, _, _ = external_fakes(monkeypatch)
    entry = WatchlistEntry("film/new/", LetterboxdDetailResult("movie", "101"))
    scrape_mock = Mock(return_value=scrape([entry]))
    monkeypatch.setattr(sync, "get_new_watchlist_entries", scrape_mock)
    monkeypatch.setattr(
        state_manager.SQLiteStateStore,
        "checkpoint_user",
        Mock(return_value=StateSaveResult(failed_items=1)),
    )

    outcome = main.run_sync_cycle(
        config_for(user("alice"), user("bob")),
        ObservabilityService("127.0.0.1", 0),
        1,
    )

    assert outcome == "failed"
    assert load_user(database, source, "alice") == {"cursor": None, "movies": {}}
    assert scrape_mock.call_count == 1
    radarr.check_radarr_state.assert_not_called()


@pytest.mark.integration
def test_close_failure_marks_cycle_failed(tmp_path, monkeypatch):
    use_state_paths(tmp_path, monkeypatch)
    external_fakes(monkeypatch)
    monkeypatch.setattr(sync, "get_new_watchlist_entries", Mock(return_value=scrape([])))
    real_close = state_manager.SQLiteStateStore.close

    def close_with_failure(store):
        real_close(store)
        return StateSaveResult(failed_items=1)

    monkeypatch.setattr(state_manager.SQLiteStateStore, "close", close_with_failure)
    outcome = main.run_sync_cycle(
        config_for(user()), ObservabilityService("127.0.0.1", 0), 1
    )
    assert outcome == "failed"
