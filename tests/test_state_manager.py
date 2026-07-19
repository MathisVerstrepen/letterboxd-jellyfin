import builtins
import json
from unittest.mock import Mock

import pytest

import src.state_manager as state_manager


def test_missing_state_is_empty_success(tmp_path, monkeypatch):
    monkeypatch.setattr(state_manager, "STATE_FILE_PATH", str(tmp_path / "missing.json"))
    result = state_manager.load_state()
    assert result.data == {}
    assert result.failed_items == 0


def test_valid_state_loads(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text('{"alice": "123"}', encoding="utf-8")
    monkeypatch.setattr(state_manager, "STATE_FILE_PATH", str(path))
    assert state_manager.load_state().data == {"alice": "123"}


def test_state_save_load_round_trip(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    monkeypatch.setattr(state_manager, "STATE_FILE_PATH", str(path))
    data = {"alice": "123", "bob": "456"}
    assert state_manager.save_state(data).failed_items == 0
    assert state_manager.load_state().data == data
    assert json.loads(path.read_text(encoding="utf-8")) == data


@pytest.mark.parametrize("content", ["{broken", '["not", "an", "object"]'])
def test_invalid_state_returns_failure(tmp_path, monkeypatch, content):
    path = tmp_path / "state.json"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(state_manager, "STATE_FILE_PATH", str(path))
    result = state_manager.load_state()
    assert result.data == {}
    assert result.failed_items == 1


def test_state_read_oserror_returns_failure(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(state_manager, "STATE_FILE_PATH", str(path))
    monkeypatch.setattr(builtins, "open", Mock(side_effect=OSError("denied")))
    result = state_manager.load_state()
    assert result.data == {}
    assert result.failed_items == 1


def test_state_write_oserror_returns_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(state_manager, "STATE_FILE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(builtins, "open", Mock(side_effect=OSError("denied")))
    assert state_manager.save_state({"alice": "1"}).failed_items == 1
