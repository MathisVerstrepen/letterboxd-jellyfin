import json
import os
import tempfile
from typing import Any

from src.logger import get_logger
from src.results import StateLoadResult, StateSaveResult

STATE_FILE_PATH = os.getenv("SYNC_STATE_PATH", "sync_state.json")
logger = get_logger("state")

STATE_VERSION = 2
STATUSES = {
    "retry_letterboxd",
    "pending_radarr",
    "retry_radarr",
    "pending_jellyfin",
    "retry_jellyfin",
    "completed",
}
COMPLETION_REASONS = {
    "jellyfin_added",
    "jellyfin_not_found",
    "radarr_no_file",
    "collection_disabled",
}


def empty_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "users": {}}


def empty_user_state() -> dict[str, Any]:
    return {"cursor": None, "movies": {}}


def _validate_cursor(cursor: Any) -> None:
    if cursor is None:
        return
    if not isinstance(cursor, dict) or set(cursor) != {"kind", "value"}:
        raise ValueError("invalid state cursor")
    if cursor["kind"] not in {"legacy_tmdb", "letterboxd"}:
        raise ValueError("invalid state cursor kind")
    if not isinstance(cursor["value"], str) or not cursor["value"]:
        raise ValueError("invalid state cursor value")


def _validate_movie(endpoint: Any, movie: Any) -> None:
    fields = {"tmdb_id", "status", "title", "year", "completion_reason"}
    if not isinstance(endpoint, str) or not endpoint or endpoint.startswith("/"):
        raise ValueError("invalid movie endpoint")
    if not isinstance(movie, dict) or set(movie) != fields:
        raise ValueError("invalid movie record")
    status = movie["status"]
    if status not in STATUSES:
        raise ValueError("invalid movie status")
    tmdb_id = movie["tmdb_id"]
    title = movie["title"]
    year = movie["year"]
    reason = movie["completion_reason"]
    if status == "retry_letterboxd":
        if any(value is not None for value in (tmdb_id, title, year, reason)):
            raise ValueError("invalid Letterboxd retry record")
        return
    if not isinstance(tmdb_id, str) or not tmdb_id:
        raise ValueError("invalid movie TMDB ID")
    if status in {"pending_jellyfin", "retry_jellyfin"}:
        if (
            not isinstance(title, str)
            or not title
            or not isinstance(year, int)
            or isinstance(year, bool)
        ):
            raise ValueError("invalid Jellyfin movie record")
    elif title is not None or year is not None:
        if status != "completed" or reason not in {
            "jellyfin_added",
            "jellyfin_not_found",
        }:
            raise ValueError("unexpected movie metadata")
    if status == "completed":
        if reason not in COMPLETION_REASONS:
            raise ValueError("invalid completion reason")
        if reason in {"jellyfin_added", "jellyfin_not_found"} and (
            not isinstance(title, str)
            or not title
            or not isinstance(year, int)
            or isinstance(year, bool)
        ):
            raise ValueError("missing completed Jellyfin metadata")
    elif reason is not None:
        raise ValueError("unexpected completion reason")


def _validate_v2(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) != {"version", "users"}:
        raise ValueError("invalid state root")
    if data["version"] != STATE_VERSION or isinstance(data["version"], bool):
        raise ValueError("unsupported state version")
    users = data["users"]
    if not isinstance(users, dict):
        raise ValueError("invalid state users")
    for username, user_state in users.items():
        if not isinstance(username, str) or not username:
            raise ValueError("invalid state username")
        if not isinstance(user_state, dict) or set(user_state) != {"cursor", "movies"}:
            raise ValueError("invalid user state")
        _validate_cursor(user_state["cursor"])
        movies = user_state["movies"]
        if not isinstance(movies, dict):
            raise ValueError("invalid movies state")
        for endpoint, movie in movies.items():
            _validate_movie(endpoint, movie)
    return data


def _migrate_v1(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("state root is not an object")
    users = {}
    for username, tmdb_id in data.items():
        if not isinstance(username, str) or not username:
            raise ValueError("invalid legacy username")
        if not isinstance(tmdb_id, str) or not tmdb_id:
            raise ValueError("invalid legacy TMDB ID")
        users[username] = {
            "cursor": {"kind": "legacy_tmdb", "value": tmdb_id},
            "movies": {},
        }
    return {"version": STATE_VERSION, "users": users}


def load_state() -> StateLoadResult:
    """
    Load and validate v2 state, normalizing the supported legacy format.
    """
    if not os.path.exists(STATE_FILE_PATH):
        return StateLoadResult(data=empty_state())
    try:
        with open(STATE_FILE_PATH, encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and "version" in data:
                return StateLoadResult(data=_validate_v2(data))
            return StateLoadResult(data=_migrate_v1(data), migrated=True)
    except (json.JSONDecodeError, OSError, ValueError):
        logger.warning(
            "State could not be loaded",
            extra={"event": "state_load_failed", "stage": "state"},
        )
        return StateLoadResult(data=empty_state(), failed_items=1)


def save_state(data: dict[str, Any]) -> StateSaveResult:
    """Validate and atomically replace the state file."""
    temporary_path: str | None = None
    file_descriptor: int | None = None
    try:
        _validate_v2(data)
        parent = os.path.dirname(os.path.abspath(STATE_FILE_PATH))
        file_descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{os.path.basename(STATE_FILE_PATH)}.", suffix=".tmp", dir=parent
        )
        state_file = os.fdopen(file_descriptor, "w", encoding="utf-8")
        file_descriptor = None
        with state_file:
            json.dump(data, state_file, indent=2)
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(temporary_path, STATE_FILE_PATH)
        temporary_path = None
        return StateSaveResult()
    except (OSError, TypeError, ValueError):
        logger.error(
            "State could not be saved",
            extra={"event": "state_save_failed", "stage": "state"},
        )
        return StateSaveResult(failed_items=1)
    finally:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
            except OSError:
                logger.warning(
                    "Temporary state file could not be removed",
                    extra={"event": "state_temp_cleanup_failed", "stage": "state"},
                )
