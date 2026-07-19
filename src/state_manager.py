import json
import os
from typing import Any

from src.logger import get_logger
from src.results import StateLoadResult, StateSaveResult

STATE_FILE_PATH = os.getenv("SYNC_STATE_PATH", "sync_state.json")
logger = get_logger("state")


def load_state() -> StateLoadResult:
    """
    Loads the state file (sync_state.json).
    Returns an empty dictionary if the file doesn't exist.
    """
    if not os.path.exists(STATE_FILE_PATH):
        return StateLoadResult(data={})
    try:
        with open(STATE_FILE_PATH, encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("state root is not an object")
            return StateLoadResult(data=data)
    except (json.JSONDecodeError, OSError, ValueError):
        logger.warning(
            "State could not be loaded; using an empty state",
            extra={"event": "state_load_failed", "stage": "state"},
        )
        return StateLoadResult(data={}, failed_items=1)


def save_state(data: dict[str, Any]) -> StateSaveResult:
    """Saves the state dictionary back to the JSON file."""
    try:
        with open(STATE_FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return StateSaveResult()
    except OSError:
        logger.error(
            "State could not be saved",
            extra={"event": "state_save_failed", "stage": "state"},
        )
        return StateSaveResult(failed_items=1)
