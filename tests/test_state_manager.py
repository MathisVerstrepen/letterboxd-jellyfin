import json
import os
import sqlite3

import pytest

from src.state_manager import (
    APPLICATION_ID,
    SCHEMA_VERSION,
    MovieStateChange,
    SQLiteStateStore,
    StateCheckpoint,
    _derive_db_path,
)


def record(status, tmdb_id=None, title=None, year=None, reason=None):
    return {
        "tmdb_id": tmdb_id,
        "status": status,
        "title": title,
        "year": year,
        "completion_reason": reason,
    }


def v2_state():
    return {
        "version": 2,
        "users": {
            "alice": {
                "cursor": {"kind": "letterboxd", "value": "film/example/"},
                "movies": {
                    "film/first/": record("retry_letterboxd"),
                    "film/example/": record(
                        "completed", "123", "Example", 2024, "jellyfin_added"
                    ),
                },
            }
        },
    }


def store_for(tmp_path, *, json_name="state.json", db_name="state.db"):
    return SQLiteStateStore(str(tmp_path / db_name), str(tmp_path / json_name))


def test_database_path_derivation():
    assert _derive_db_path("sync_state.json") == "sync_state.db"
    assert _derive_db_path("state") == "state.db"
    assert _derive_db_path("state.JSON") == "state.JSON.db"


def test_empty_database_initializes_with_exact_metadata_and_permissions(tmp_path):
    store = store_for(tmp_path)
    result = store.initialize()
    assert (result.failed_items, result.migrated) == (0, False)
    assert store.load_or_create_user("alice").data == {"cursor": None, "movies": {}}
    assert store.close().failed_items == 0

    path = tmp_path / "state.db"
    assert path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA application_id").fetchone() == (APPLICATION_ID,)
        assert connection.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
        assert [
            row[1] for row in connection.execute("PRAGMA index_list(movies)")
        ].count("movies_username_movie_id_idx") == 1


@pytest.mark.parametrize(
    ("source", "expected_cursor"),
    [
        ({"alice": "123"}, {"kind": "legacy_tmdb", "value": "123"}),
        ({}, None),
        (v2_state(), {"kind": "letterboxd", "value": "film/example/"}),
    ],
)
def test_json_import_is_one_time_and_preserves_source(tmp_path, source, expected_cursor):
    source_path = tmp_path / "state.json"
    original = json.dumps(source).encode()
    source_path.write_bytes(original)
    store = store_for(tmp_path)

    result = store.initialize()
    assert (result.failed_items, result.migrated) == (0, True)
    if "alice" in source.get("users", source):
        loaded = store.load_or_create_user("alice").data
        assert loaded["cursor"] == expected_cursor
        if source.get("version") == 2:
            assert list(loaded["movies"]) == ["film/first/", "film/example/"]
    assert store.close().failed_items == 0
    assert source_path.read_bytes() == original


def test_existing_database_is_authoritative_and_json_is_not_opened(tmp_path):
    store = store_for(tmp_path)
    assert store.initialize().failed_items == 0
    assert store.load_or_create_user("alice").failed_items == 0
    assert store.close().failed_items == 0
    (tmp_path / "state.json").write_text("not json", encoding="utf-8")

    reopened = store_for(tmp_path)
    assert reopened.initialize().failed_items == 0
    assert reopened.load_or_create_user("alice").failed_items == 0
    reopened.close()


@pytest.mark.parametrize("contents", [b"not sqlite", b""])
def test_invalid_existing_database_fails_closed_without_json_fallback(tmp_path, contents):
    database = tmp_path / "state.db"
    database.write_bytes(contents)
    source = tmp_path / "state.json"
    source.write_text('{"alice": "123"}', encoding="utf-8")
    original = database.read_bytes()

    result = store_for(tmp_path).initialize()

    assert result.failed_items == 1
    assert database.read_bytes() == original
    assert source.read_text(encoding="utf-8") == '{"alice": "123"}'


def test_unsupported_database_version_fails_without_mutating_file(tmp_path):
    store = store_for(tmp_path)
    assert store.initialize().failed_items == 0
    store.close()
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 99")
    original = path.read_bytes()

    assert store_for(tmp_path).initialize().failed_items == 1
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "source",
    [
        {"version": 3, "users": {}},
        {"alice": ""},
        {"alice": 123},
        {"version": 2, "users": {"alice": {"cursor": None, "movies": {"/bad": {}}}}},
    ],
)
def test_invalid_json_fails_without_database_or_temporary_artifacts(tmp_path, source):
    path = tmp_path / "state.json"
    original = json.dumps(source)
    path.write_text(original, encoding="utf-8")

    assert store_for(tmp_path).initialize().failed_items == 1
    assert not (tmp_path / "state.db").exists()
    assert path.read_text(encoding="utf-8") == original
    assert sorted(item.name for item in tmp_path.iterdir()) == ["state.json"]


def test_paths_must_be_nonempty_distinct_and_have_existing_parent(tmp_path):
    assert SQLiteStateStore("", str(tmp_path / "state.json")).initialize().failed_items == 1
    same = str(tmp_path / "state.json")
    assert SQLiteStateStore(same, same).initialize().failed_items == 1
    missing = tmp_path / "missing" / "state.db"
    assert SQLiteStateStore(str(missing), same).initialize().failed_items == 1
    assert not missing.parent.exists()


@pytest.mark.parametrize(
    "movie",
    [
        record("retry_letterboxd"),
        record("pending_radarr", "1"),
        record("retry_radarr", "1"),
        record("pending_jellyfin", "1", "Movie", 2020),
        record("retry_jellyfin", "1", "Movie", 2020),
        record("completed", "1", "Movie", 2020, "jellyfin_added"),
        record("completed", "1", "Movie", 2020, "jellyfin_not_found"),
        record("completed", "1", reason="radarr_no_file"),
        record("completed", "1", reason="collection_disabled"),
    ],
)
def test_every_valid_movie_status_round_trips(tmp_path, movie):
    store = store_for(tmp_path)
    store.initialize()
    store.load_or_create_user("alice")
    checkpoint = StateCheckpoint(
        movie_changes=(MovieStateChange("upsert", "film/a/", movie),)
    )
    assert store.checkpoint_user("alice", checkpoint).failed_items == 0
    assert store.load_or_create_user("alice").data["movies"]["film/a/"] == movie
    store.close()


def test_ordered_upsert_delete_and_cursor_clear_survive_reopen(tmp_path):
    store = store_for(tmp_path)
    store.initialize()
    store.load_or_create_user("alice")
    first = StateCheckpoint(
        cursor_changed=True,
        cursor={"kind": "legacy_tmdb", "value": "10"},
        movie_changes=(
            MovieStateChange("upsert", "film/a/", record("pending_radarr", "1")),
            MovieStateChange("upsert", "film/b/", record("pending_radarr", "2")),
        ),
    )
    assert store.checkpoint_user("alice", first).failed_items == 0
    reordered = StateCheckpoint(
        cursor_changed=True,
        cursor=None,
        movie_changes=(
            MovieStateChange("delete", "film/a/"),
            MovieStateChange("upsert", "film/a/", record("retry_radarr", "1")),
        ),
    )
    assert store.checkpoint_user("alice", reordered).failed_items == 0
    store.close()

    store = store_for(tmp_path)
    assert store.initialize().failed_items == 0
    loaded = store.load_or_create_user("alice").data
    assert loaded["cursor"] is None
    assert list(loaded["movies"]) == ["film/b/", "film/a/"]
    store.close()


def test_checkpoint_snapshots_do_not_retain_mutable_state():
    cursor = {"kind": "letterboxd", "value": "film/a/"}
    movie = record("pending_radarr", "1")
    checkpoint = StateCheckpoint(
        cursor_changed=True,
        cursor=cursor,
        movie_changes=(MovieStateChange("upsert", "film/a/", movie),),
    )
    cursor["value"] = "changed"
    movie["status"] = "retry_radarr"
    assert checkpoint.cursor["value"] == "film/a/"
    assert checkpoint.movie_changes[0].movie["status"] == "pending_radarr"


def test_checkpoint_rolls_back_cursor_and_prior_movie_on_mid_transaction_failure(tmp_path):
    store = store_for(tmp_path)
    store.initialize()
    store.load_or_create_user("alice")
    connection = store._connection
    connection.execute(
        """
        CREATE TRIGGER fail_second_movie BEFORE INSERT ON movies
        WHEN NEW.endpoint = 'film/b/'
        BEGIN SELECT RAISE(ABORT, 'injected failure'); END
        """
    )
    checkpoint = StateCheckpoint(
        cursor_changed=True,
        cursor={"kind": "letterboxd", "value": "film/new/"},
        movie_changes=(
            MovieStateChange("upsert", "film/a/", record("pending_radarr", "1")),
            MovieStateChange("upsert", "film/b/", record("pending_radarr", "2")),
        ),
    )

    assert store.checkpoint_user("alice", checkpoint).failed_items == 1
    assert store.load_or_create_user("alice").data == {"cursor": None, "movies": {}}
    store.close()


def test_empty_and_invalid_checkpoints_issue_no_changes(tmp_path):
    store = store_for(tmp_path)
    store.initialize()
    store.load_or_create_user("alice")
    assert store.checkpoint_user("alice", StateCheckpoint()).failed_items == 1
    with pytest.raises(ValueError):
        MovieStateChange("upsert", "film/a/", record("pending_jellyfin", "1", "M", True))
    assert store.load_or_create_user("alice").data == {"cursor": None, "movies": {}}
    store.close()


def test_source_mode_is_not_changed(tmp_path):
    source = tmp_path / "state.json"
    source.write_text('{"alice": "123"}', encoding="utf-8")
    os.chmod(source, 0o640)
    store = store_for(tmp_path)
    assert store.initialize().failed_items == 0
    store.close()
    assert source.stat().st_mode & 0o777 == 0o640
