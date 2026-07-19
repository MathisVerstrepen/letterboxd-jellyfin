import json
import os
from unittest.mock import Mock

import pytest

import src.state_manager as state_manager


def v2_state():
    return {
        "version": 2,
        "users": {
            "alice": {
                "cursor": {"kind": "letterboxd", "value": "film/example/"},
                "movies": {
                    "film/example/": {
                        "tmdb_id": "123",
                        "status": "completed",
                        "title": "Example",
                        "year": 2024,
                        "completion_reason": "jellyfin_added",
                    }
                },
            }
        },
    }


def test_missing_state_is_empty_v2_success(tmp_path, monkeypatch):
    monkeypatch.setattr(state_manager, "STATE_FILE_PATH", str(tmp_path / "missing.json"))
    result = state_manager.load_state()
    assert result.data == {"version": 2, "users": {}}
    assert (result.failed_items, result.migrated) == (0, False)


def test_valid_v2_state_loads(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(v2_state()), encoding="utf-8")
    monkeypatch.setattr(state_manager, "STATE_FILE_PATH", str(path))
    result = state_manager.load_state()
    assert result.data == v2_state()
    assert not result.migrated


@pytest.mark.parametrize(
    ("legacy", "users"),
    [
        (
            {"alice": "123"},
            {
                "alice": {
                    "cursor": {"kind": "legacy_tmdb", "value": "123"},
                    "movies": {},
                }
            },
        ),
        ({}, {}),
    ],
)
def test_legacy_state_is_normalized(tmp_path, monkeypatch, legacy, users):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    monkeypatch.setattr(state_manager, "STATE_FILE_PATH", str(path))
    result = state_manager.load_state()
    assert result.data == {"version": 2, "users": users}
    assert result.migrated


@pytest.mark.parametrize(
    "data",
    [
        {"version": 3, "users": {}},
        {"alice": ""},
        {"alice": 123},
        {"version": 2, "users": {"alice": {"cursor": None, "movies": {"/bad": {}}}}},
        {
            "version": 2,
            "users": {
                "alice": {
                    "cursor": None,
                    "movies": {
                        "film/a/": {
                            "tmdb_id": None,
                            "status": "pending_radarr",
                            "title": None,
                            "year": None,
                            "completion_reason": None,
                        }
                    },
                }
            },
        },
    ],
)
def test_invalid_schemas_are_rejected(tmp_path, monkeypatch, data):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(state_manager, "STATE_FILE_PATH", str(path))
    result = state_manager.load_state()
    assert result.failed_items == 1
    assert result.data == {"version": 2, "users": {}}


def test_state_save_load_round_trip_and_replaces_atomically(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text("old", encoding="utf-8")
    monkeypatch.setattr(state_manager, "STATE_FILE_PATH", str(path))
    replace = Mock(wraps=os.replace)
    fsync = Mock(wraps=os.fsync)
    monkeypatch.setattr(state_manager.os, "replace", replace)
    monkeypatch.setattr(state_manager.os, "fsync", fsync)

    assert state_manager.save_state(v2_state()).failed_items == 0

    source, target = replace.call_args.args
    assert os.path.dirname(source) == str(tmp_path)
    assert target == str(path)
    assert fsync.call_count == 1
    assert state_manager.load_state().data == v2_state()
    assert not list(tmp_path.glob("*.tmp"))


def test_replace_failure_preserves_target_and_cleans_temp(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text('{"old": "state"}', encoding="utf-8")
    monkeypatch.setattr(state_manager, "STATE_FILE_PATH", str(path))
    monkeypatch.setattr(state_manager.os, "replace", Mock(side_effect=OSError("denied")))

    assert state_manager.save_state(v2_state()).failed_items == 1
    assert path.read_text(encoding="utf-8") == '{"old": "state"}'
    assert not list(tmp_path.glob("*.tmp"))


def test_invalid_save_does_not_touch_existing_target(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text("preserve", encoding="utf-8")
    monkeypatch.setattr(state_manager, "STATE_FILE_PATH", str(path))
    assert state_manager.save_state({"version": 99, "users": {}}).failed_items == 1
    assert path.read_text(encoding="utf-8") == "preserve"
