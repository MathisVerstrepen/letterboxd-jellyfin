import json
import os
from typing import Any

STATE_FILE_PATH = "sync_state.json"

def load_state() -> dict[str, Any]:
    """
    Loads the state file (sync_state.json).
    Returns an empty dictionary if the file doesn't exist.
    """
    if not os.path.exists(STATE_FILE_PATH):
        return {}
    try:
        with open(STATE_FILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"WARNING: Could not read state file at '{STATE_FILE_PATH}': {e}")
        return {}

def save_state(data: dict[str, Any]):
    """Saves the state dictionary back to the JSON file."""
    try:
        with open(STATE_FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except IOError as e:
        print(f"ERROR: Could not write to state file: {e}")

