import json
import os
import sqlite3
import tempfile
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from src.logger import get_logger
from src.results import (
    CompletedEndpointsResult,
    StateInitializationResult,
    StateLoadResult,
    StateSaveResult,
)

LEGACY_STATE_FILE_PATH = os.getenv("SYNC_STATE_PATH", "sync_state.json")


def _derive_db_path(legacy_path: str) -> str:
    if legacy_path.endswith(".json"):
        return f"{legacy_path[:-5]}.db"
    return f"{legacy_path}.db"


_configured_db_path = os.getenv("SYNC_STATE_DB_PATH")
STATE_DB_PATH = (
    _derive_db_path(LEGACY_STATE_FILE_PATH)
    if _configured_db_path is None
    else _configured_db_path
)
logger = get_logger("state")

JSON_STATE_VERSION = 2
SCHEMA_VERSION = 3
APPLICATION_ID = 1279412806
PENDING_STATUSES = (
    "retry_letterboxd",
    "pending_radarr",
    "retry_radarr",
    "pending_jellyfin",
    "retry_jellyfin",
)
STATUSES = frozenset((*PENDING_STATUSES, "completed"))
COMPLETION_REASONS = {
    "jellyfin_added",
    "jellyfin_not_found",
    "radarr_no_file",
    "collection_disabled",
}
MOVIE_FIELDS = {"tmdb_id", "status", "title", "year", "completion_reason"}
SERIES_PENDING_STATUSES = ("retry_letterboxd", "pending_sonarr", "retry_sonarr")
SERIES_STATUSES = frozenset((*SERIES_PENDING_STATUSES, "completed"))
SERIES_FIELDS = {"tmdb_id", "status", "completion_reason"}

SCHEMA_V1_SQL = """
CREATE TABLE users (
    username TEXT PRIMARY KEY NOT NULL CHECK (length(username) > 0),
    cursor_kind TEXT,
    cursor_value TEXT,
    CHECK (
        (cursor_kind IS NULL AND cursor_value IS NULL)
        OR (
            cursor_kind IS NOT NULL
            AND cursor_kind IN ('legacy_tmdb', 'letterboxd')
            AND cursor_value IS NOT NULL
            AND length(cursor_value) > 0
        )
    )
);

CREATE TABLE movies (
    movie_id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    endpoint TEXT NOT NULL CHECK (
        length(endpoint) > 0 AND substr(endpoint, 1, 1) <> '/'
    ),
    tmdb_id TEXT,
    status TEXT NOT NULL CHECK (
        status IN (
            'retry_letterboxd',
            'pending_radarr',
            'retry_radarr',
            'pending_jellyfin',
            'retry_jellyfin',
            'completed'
        )
    ),
    title TEXT,
    year INTEGER,
    completion_reason TEXT,
    UNIQUE (username, endpoint),
    FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE,
    CHECK (
        (
            status = 'retry_letterboxd'
            AND tmdb_id IS NULL
            AND title IS NULL
            AND year IS NULL
            AND completion_reason IS NULL
        )
        OR (
            status IN ('pending_radarr', 'retry_radarr')
            AND tmdb_id IS NOT NULL
            AND length(tmdb_id) > 0
            AND title IS NULL
            AND year IS NULL
            AND completion_reason IS NULL
        )
        OR (
            status IN ('pending_jellyfin', 'retry_jellyfin')
            AND tmdb_id IS NOT NULL
            AND length(tmdb_id) > 0
            AND title IS NOT NULL
            AND length(title) > 0
            AND typeof(year) = 'integer'
            AND completion_reason IS NULL
        )
        OR (
            status = 'completed'
            AND tmdb_id IS NOT NULL
            AND length(tmdb_id) > 0
            AND completion_reason IS NOT NULL
            AND (
                (
                    completion_reason IN ('jellyfin_added', 'jellyfin_not_found')
                    AND title IS NOT NULL
                    AND length(title) > 0
                    AND typeof(year) = 'integer'
                )
                OR (
                    completion_reason IN ('radarr_no_file', 'collection_disabled')
                    AND title IS NULL
                    AND year IS NULL
                )
            )
        )
    )
);

CREATE INDEX movies_username_movie_id_idx
ON movies (username, movie_id);
"""

SCHEMA_V2_SQL = (
    SCHEMA_V1_SQL
    + """

CREATE INDEX movies_username_status_movie_id_idx
ON movies (username, status, movie_id);
"""
)

SCHEMA_SQL = (
    SCHEMA_V2_SQL
    + """

CREATE TABLE series_sync (
    username TEXT PRIMARY KEY NOT NULL,
    backfill_complete INTEGER NOT NULL DEFAULT 0 CHECK (backfill_complete IN (0, 1)),
    FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
);

CREATE TABLE series (
    series_id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    endpoint TEXT NOT NULL CHECK (
        length(endpoint) > 0 AND substr(endpoint, 1, 1) <> '/'
    ),
    tmdb_id TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('retry_letterboxd', 'pending_sonarr', 'retry_sonarr', 'completed')
    ),
    completion_reason TEXT,
    UNIQUE (username, endpoint),
    FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE,
    CHECK (
        (
            status = 'retry_letterboxd'
            AND tmdb_id IS NULL
            AND completion_reason IS NULL
        )
        OR (
            status IN ('pending_sonarr', 'retry_sonarr')
            AND tmdb_id IS NOT NULL
            AND length(tmdb_id) > 0
            AND completion_reason IS NULL
        )
        OR (
            status = 'completed'
            AND tmdb_id IS NOT NULL
            AND length(tmdb_id) > 0
            AND completion_reason = 'sonarr_processed'
        )
    )
);

CREATE INDEX series_username_series_id_idx
ON series (username, series_id);

CREATE INDEX series_username_status_series_id_idx
ON series (username, status, series_id);
"""
)


def empty_user_state() -> dict[str, Any]:
    return {
        "cursor": None,
        "movies": {},
        "series": {},
        "series_backfill_complete": False,
    }


def _validate_cursor(cursor: Any) -> None:
    if cursor is None:
        return
    if not isinstance(cursor, Mapping) or set(cursor) != {"kind", "value"}:
        raise ValueError("invalid state cursor")
    if cursor["kind"] not in {"legacy_tmdb", "letterboxd"}:
        raise ValueError("invalid state cursor kind")
    if not isinstance(cursor["value"], str) or not cursor["value"]:
        raise ValueError("invalid state cursor value")


def _validate_endpoint(endpoint: Any) -> None:
    if not isinstance(endpoint, str) or not endpoint or endpoint.startswith("/"):
        raise ValueError("invalid movie endpoint")


def _validate_movie(endpoint: Any, movie: Any) -> None:
    _validate_endpoint(endpoint)
    if not isinstance(movie, Mapping) or set(movie) != MOVIE_FIELDS:
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
    if status in {"pending_radarr", "retry_radarr"}:
        if any(value is not None for value in (title, year, reason)):
            raise ValueError("invalid Radarr movie record")
        return
    if status in {"pending_jellyfin", "retry_jellyfin"}:
        if (
            not isinstance(title, str)
            or not title
            or not isinstance(year, int)
            or isinstance(year, bool)
            or reason is not None
        ):
            raise ValueError("invalid Jellyfin movie record")
        return
    if reason not in COMPLETION_REASONS:
        raise ValueError("invalid completion reason")
    if reason in {"jellyfin_added", "jellyfin_not_found"}:
        if (
            not isinstance(title, str)
            or not title
            or not isinstance(year, int)
            or isinstance(year, bool)
        ):
            raise ValueError("missing completed Jellyfin metadata")
    elif title is not None or year is not None:
        raise ValueError("unexpected completed movie metadata")


def _validate_series(endpoint: Any, series: Any) -> None:
    _validate_endpoint(endpoint)
    if not isinstance(series, Mapping) or set(series) != SERIES_FIELDS:
        raise ValueError("invalid series record")
    status = series["status"]
    tmdb_id = series["tmdb_id"]
    reason = series["completion_reason"]
    if status not in SERIES_STATUSES:
        raise ValueError("invalid series status")
    if status == "retry_letterboxd":
        if tmdb_id is not None or reason is not None:
            raise ValueError("invalid Letterboxd series retry record")
    elif not isinstance(tmdb_id, str) or not tmdb_id:
        raise ValueError("invalid series TMDB ID")
    elif status == "completed":
        if reason != "sonarr_processed":
            raise ValueError("invalid series completion reason")
    elif reason is not None:
        raise ValueError("invalid pending series completion reason")


def _validate_v2(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) != {"version", "users"}:
        raise ValueError("invalid state root")
    if data["version"] != JSON_STATE_VERSION or isinstance(data["version"], bool):
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
    return {"version": JSON_STATE_VERSION, "users": users}


@dataclass(frozen=True)
class MovieStateChange:
    action: str
    endpoint: str
    movie: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.action not in {"upsert", "delete"}:
            raise ValueError("invalid movie state action")
        _validate_endpoint(self.endpoint)
        if self.action == "delete":
            if self.movie is not None:
                raise ValueError("a delete cannot carry a movie record")
            return
        _validate_movie(self.endpoint, self.movie)
        object.__setattr__(self, "movie", MappingProxyType(dict(self.movie or {})))


@dataclass(frozen=True)
class SeriesStateChange:
    action: str
    endpoint: str
    series: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.action not in {"upsert", "delete"}:
            raise ValueError("invalid series state action")
        _validate_endpoint(self.endpoint)
        if self.action == "delete":
            if self.series is not None:
                raise ValueError("a delete cannot carry a series record")
            return
        _validate_series(self.endpoint, self.series)
        object.__setattr__(self, "series", MappingProxyType(dict(self.series or {})))


@dataclass(frozen=True)
class StateCheckpoint:
    cursor_changed: bool = False
    cursor: Mapping[str, Any] | None = None
    movie_changes: tuple[MovieStateChange, ...] = ()
    series_changes: tuple[SeriesStateChange, ...] = ()
    series_backfill_changed: bool = False
    series_backfill_complete: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.cursor_changed, bool):
            raise ValueError("invalid cursor change flag")
        changes = tuple(self.movie_changes)
        if any(not isinstance(change, MovieStateChange) for change in changes):
            raise ValueError("invalid movie state changes")
        object.__setattr__(self, "movie_changes", changes)
        series_changes = tuple(self.series_changes)
        if any(not isinstance(change, SeriesStateChange) for change in series_changes):
            raise ValueError("invalid series state changes")
        object.__setattr__(self, "series_changes", series_changes)
        if not isinstance(self.series_backfill_changed, bool) or not isinstance(
            self.series_backfill_complete, bool
        ):
            raise ValueError("invalid series backfill marker")
        if not self.series_backfill_changed and self.series_backfill_complete:
            raise ValueError("unchanged series backfill marker cannot carry a value")
        if self.cursor_changed:
            _validate_cursor(self.cursor)
            if self.cursor is not None:
                object.__setattr__(self, "cursor", MappingProxyType(dict(self.cursor)))
        elif self.cursor is not None:
            raise ValueError("unchanged cursor cannot carry a value")


class SQLiteStateStore:
    def __init__(
        self,
        db_path: str = STATE_DB_PATH,
        legacy_json_path: str = LEGACY_STATE_FILE_PATH,
    ) -> None:
        self.db_path = db_path
        self.legacy_json_path = legacy_json_path
        self._connection: sqlite3.Connection | None = None

    def initialize(self) -> StateInitializationResult:
        temporary_path: str | None = None
        installed = False
        migrated = False
        try:
            self._validate_paths()
            if os.path.exists(self.db_path):
                connection, migrated = self._open_existing()
                self._connection = connection
                return StateInitializationResult(migrated=migrated)

            state = {"version": JSON_STATE_VERSION, "users": {}}
            if os.path.exists(self.legacy_json_path):
                state = self._load_legacy_json()
                migrated = True

            parent = os.path.dirname(os.path.abspath(self.db_path))
            descriptor, temporary_path = tempfile.mkstemp(
                prefix=f".{os.path.basename(self.db_path)}.", suffix=".tmp", dir=parent
            )
            try:
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
            self._build_database(temporary_path, state)
            with open(temporary_path, "rb") as database_file:
                os.fsync(database_file.fileno())
            if os.path.exists(self.db_path):
                raise FileExistsError("state database appeared during initialization")
            os.replace(temporary_path, self.db_path)
            temporary_path = None
            installed = True
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            self._connection, _ = self._open_existing()
            return StateInitializationResult(migrated=migrated)
        except (json.JSONDecodeError, OSError, sqlite3.Error, TypeError, ValueError):
            logger.error(
                "State database could not be initialized",
                extra={"event": "state_initialization_failed", "stage": "state"},
            )
            if self._connection is not None:
                try:
                    self._connection.close()
                except sqlite3.Error:
                    pass
                self._connection = None
            if installed:
                self._remove_artifact(self.db_path)
                self._remove_artifact(f"{self.db_path}-journal")
            return StateInitializationResult(failed_items=1)
        finally:
            if temporary_path is not None:
                self._remove_artifact(temporary_path)
                self._remove_artifact(f"{temporary_path}-journal")

    def load_or_create_user(self, username: str) -> StateLoadResult:
        try:
            if not isinstance(username, str) or not username:
                raise ValueError("invalid state username")
            connection = self._require_connection()
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT OR IGNORE INTO users (username) VALUES (?)", (username,)
                )
                connection.execute(
                    "INSERT OR IGNORE INTO series_sync (username) VALUES (?)", (username,)
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            cursor_row = connection.execute(
                "SELECT cursor_kind, cursor_value FROM users WHERE username = ?",
                (username,),
            ).fetchone()
            if cursor_row is None:
                raise sqlite3.DatabaseError("user was not created")
            cursor = (
                {"kind": cursor_row[0], "value": cursor_row[1]}
                if cursor_row[0] is not None
                else None
            )
            movies = {}
            rows = connection.execute(
                """
                SELECT endpoint, tmdb_id, status, title, year, completion_reason
                FROM movies
                WHERE username = ?
                  AND status IN (?, ?, ?, ?, ?)
                ORDER BY movie_id
                """,
                (username, *PENDING_STATUSES),
            )
            for endpoint, tmdb_id, status, title, year, reason in rows:
                movie = {
                    "tmdb_id": tmdb_id,
                    "status": status,
                    "title": title,
                    "year": year,
                    "completion_reason": reason,
                }
                _validate_movie(endpoint, movie)
                movies[endpoint] = movie
            marker_row = connection.execute(
                "SELECT backfill_complete FROM series_sync WHERE username = ?",
                (username,),
            ).fetchone()
            if marker_row is None:
                raise sqlite3.DatabaseError("series state was not created")
            series_state = {}
            rows = connection.execute(
                """
                SELECT endpoint, tmdb_id, status, completion_reason
                FROM series
                WHERE username = ? AND status IN (?, ?, ?)
                ORDER BY series_id
                """,
                (username, *SERIES_PENDING_STATUSES),
            )
            for endpoint, tmdb_id, status, reason in rows:
                item = {
                    "tmdb_id": tmdb_id,
                    "status": status,
                    "completion_reason": reason,
                }
                _validate_series(endpoint, item)
                series_state[endpoint] = item
            _validate_cursor(cursor)
            return StateLoadResult(
                data={
                    "cursor": cursor,
                    "movies": movies,
                    "series": series_state,
                    "series_backfill_complete": bool(marker_row[0]),
                }
            )
        except (sqlite3.Error, TypeError, ValueError):
            logger.error(
                "User state could not be loaded",
                extra={"event": "state_load_failed", "stage": "state"},
            )
            return StateLoadResult(data=empty_user_state(), failed_items=1)

    def get_completed_endpoints(
        self, username: str, endpoints: Collection[str]
    ) -> CompletedEndpointsResult:
        try:
            if not isinstance(username, str) or not username:
                raise ValueError("invalid state username")
            if isinstance(endpoints, (str, bytes)) or not isinstance(
                endpoints, Collection
            ):
                raise TypeError("invalid completed endpoint collection")
            candidates = tuple(dict.fromkeys(endpoints))
            for endpoint in candidates:
                _validate_endpoint(endpoint)
            if not candidates:
                return CompletedEndpointsResult()

            connection = self._require_connection()
            completed = set()
            for offset in range(0, len(candidates), 500):
                batch = candidates[offset : offset + 500]
                values = ", ".join("(?)" for _ in batch)
                rows = connection.execute(
                    f"""
                    WITH candidates(endpoint) AS (VALUES {values})
                    SELECT endpoint FROM movies
                    WHERE username = ? AND status = 'completed'
                      AND endpoint IN (SELECT endpoint FROM candidates)
                    UNION
                    SELECT endpoint FROM series
                    WHERE username = ? AND status = 'completed'
                      AND endpoint IN (SELECT endpoint FROM candidates)
                    """,
                    (*batch, username, username),
                )
                completed.update(row[0] for row in rows)
            return CompletedEndpointsResult(endpoints=frozenset(completed))
        except (sqlite3.Error, TypeError, ValueError):
            logger.error(
                "Completed movie endpoints could not be loaded",
                extra={"event": "state_completed_lookup_failed", "stage": "state"},
            )
            return CompletedEndpointsResult(failed_items=1)

    def checkpoint_user(
        self, username: str, checkpoint: StateCheckpoint
    ) -> StateSaveResult:
        try:
            if not isinstance(username, str) or not username:
                raise ValueError("invalid state username")
            if not isinstance(checkpoint, StateCheckpoint):
                raise TypeError("invalid state checkpoint")
            if not any(
                (
                    checkpoint.cursor_changed,
                    checkpoint.movie_changes,
                    checkpoint.series_changes,
                    checkpoint.series_backfill_changed,
                )
            ):
                raise ValueError("empty state checkpoint")
            connection = self._require_connection()
            connection.execute("BEGIN IMMEDIATE")
            try:
                exists = connection.execute(
                    "SELECT 1 FROM users WHERE username = ?", (username,)
                ).fetchone()
                if exists is None:
                    raise ValueError("unknown state user")
                if checkpoint.cursor_changed:
                    cursor = checkpoint.cursor
                    connection.execute(
                        "UPDATE users SET cursor_kind = ?, cursor_value = ? WHERE username = ?",
                        (
                            cursor["kind"] if cursor else None,
                            cursor["value"] if cursor else None,
                            username,
                        ),
                    )
                if checkpoint.series_backfill_changed:
                    connection.execute(
                        "UPDATE series_sync SET backfill_complete = ? WHERE username = ?",
                        (int(checkpoint.series_backfill_complete), username),
                    )
                for change in checkpoint.movie_changes:
                    if change.action == "delete":
                        connection.execute(
                            "DELETE FROM movies WHERE username = ? AND endpoint = ?",
                            (username, change.endpoint),
                        )
                    else:
                        movie = change.movie
                        if movie is None:
                            raise ValueError("upsert is missing a movie record")
                        connection.execute(
                            """
                            INSERT INTO movies (
                                username, endpoint, tmdb_id, status, title, year,
                                completion_reason
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(username, endpoint) DO UPDATE SET
                                tmdb_id = excluded.tmdb_id,
                                status = excluded.status,
                                title = excluded.title,
                                year = excluded.year,
                                completion_reason = excluded.completion_reason
                            """,
                            (
                                username,
                                change.endpoint,
                                movie["tmdb_id"],
                                movie["status"],
                                movie["title"],
                                movie["year"],
                                movie["completion_reason"],
                            ),
                        )
                for change in checkpoint.series_changes:
                    if change.action == "delete":
                        connection.execute(
                            "DELETE FROM series WHERE username = ? AND endpoint = ?",
                            (username, change.endpoint),
                        )
                    else:
                        item = change.series
                        if item is None:
                            raise ValueError("upsert is missing a series record")
                        connection.execute(
                            """
                            INSERT INTO series (
                                username, endpoint, tmdb_id, status, completion_reason
                            ) VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(username, endpoint) DO UPDATE SET
                                tmdb_id = excluded.tmdb_id,
                                status = excluded.status,
                                completion_reason = excluded.completion_reason
                            """,
                            (
                                username,
                                change.endpoint,
                                item["tmdb_id"],
                                item["status"],
                                item["completion_reason"],
                            ),
                        )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            return StateSaveResult()
        except (sqlite3.Error, TypeError, ValueError):
            logger.error(
                "User state could not be checkpointed",
                extra={"event": "state_save_failed", "stage": "state"},
            )
            return StateSaveResult(failed_items=1)

    def close(self) -> StateSaveResult:
        if self._connection is None:
            return StateSaveResult()
        connection = self._connection
        self._connection = None
        try:
            connection.close()
            return StateSaveResult()
        except sqlite3.Error:
            logger.error(
                "State database could not be closed",
                extra={"event": "state_close_failed", "stage": "state"},
            )
            return StateSaveResult(failed_items=1)

    def _validate_paths(self) -> None:
        if not isinstance(self.db_path, str) or not self.db_path:
            raise ValueError("empty state database path")
        if not isinstance(self.legacy_json_path, str) or not self.legacy_json_path:
            raise ValueError("empty legacy state path")
        if os.path.realpath(self.db_path) == os.path.realpath(self.legacy_json_path):
            raise ValueError("state database and legacy paths must differ")
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if not os.path.isdir(parent) or not os.access(parent, os.W_OK):
            raise OSError("state database parent is not writable")

    def _load_legacy_json(self) -> dict[str, Any]:
        with open(self.legacy_json_path, encoding="utf-8") as state_file:
            data = json.load(state_file)
        if isinstance(data, dict) and "version" in data:
            return _validate_v2(data)
        return _migrate_v1(data)

    def _build_database(self, path: str, state: dict[str, Any]) -> None:
        connection = sqlite3.connect(path, timeout=5.0, isolation_level=None)
        try:
            self._configure_connection(connection)
            connection.executescript(f"BEGIN IMMEDIATE;\n{SCHEMA_SQL}")
            try:
                connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                for username, user_state in state["users"].items():
                    cursor = user_state["cursor"]
                    connection.execute(
                        """
                        INSERT INTO users (username, cursor_kind, cursor_value)
                        VALUES (?, ?, ?)
                        """,
                        (
                            username,
                            cursor["kind"] if cursor else None,
                            cursor["value"] if cursor else None,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO series_sync (username) VALUES (?)", (username,)
                    )
                    for endpoint, movie in user_state["movies"].items():
                        connection.execute(
                            """
                            INSERT INTO movies (
                                username, endpoint, tmdb_id, status, title, year,
                                completion_reason
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                username,
                                endpoint,
                                movie["tmdb_id"],
                                movie["status"],
                                movie["title"],
                                movie["year"],
                                movie["completion_reason"],
                            ),
                        )
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity != ("ok",):
                raise sqlite3.DatabaseError("state database integrity check failed")
        finally:
            connection.close()

    def _open_existing(self) -> tuple[sqlite3.Connection, bool]:
        uri = f"{Path(self.db_path).resolve().as_uri()}?mode=rw"
        connection = sqlite3.connect(
            uri, uri=True, timeout=5.0, isolation_level=None
        )
        try:
            version = connection.execute("PRAGMA user_version").fetchone()
            if version in {(1,), (2,)}:
                source_version = version[0]
                self._verify_schema(connection, version=source_version)
                self._configure_connection(connection)
                self._migrate_database(connection, source_version=source_version)
                self._verify_schema(connection, version=SCHEMA_VERSION)
                return connection, True
            self._verify_schema(connection, version=SCHEMA_VERSION)
            self._configure_connection(connection)
            return connection, False
        except Exception:
            connection.close()
            raise

    @staticmethod
    def _configure_connection(connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA busy_timeout = 5000")

    @staticmethod
    def _migrate_database(
        connection: sqlite3.Connection, *, source_version: int
    ) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            if source_version == 1:
                connection.execute(
                    """
                    CREATE INDEX movies_username_status_movie_id_idx
                    ON movies (username, status, movie_id)
                    """
                )
            statements = SCHEMA_SQL.removeprefix(SCHEMA_V2_SQL).strip().split(";\n\n")
            for statement in statements:
                connection.execute(statement.removesuffix(";"))
            connection.execute(
                "INSERT INTO series_sync (username) SELECT username FROM users"
            )
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            SQLiteStateStore._verify_schema(connection, version=SCHEMA_VERSION)
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _verify_schema(connection: sqlite3.Connection, *, version: int) -> None:
        if connection.execute("PRAGMA application_id").fetchone() != (APPLICATION_ID,):
            raise sqlite3.DatabaseError("foreign state database")
        if connection.execute("PRAGMA user_version").fetchone() != (version,):
            raise sqlite3.DatabaseError("unsupported state database schema")

        schema_sql = {1: SCHEMA_V1_SQL, 2: SCHEMA_V2_SQL, 3: SCHEMA_SQL}.get(version)
        if schema_sql is None:
            raise sqlite3.DatabaseError("unsupported state database schema")
        schema_statements = schema_sql.strip().split(";\n\n")
        expected_sql = {
            "users": schema_statements[0],
            "movies": schema_statements[1],
            "movies_username_movie_id_idx": schema_statements[2].removesuffix(";"),
        }
        if version >= 2:
            expected_sql["movies_username_status_movie_id_idx"] = schema_statements[
                3
            ].removesuffix(";")
        if version == 3:
            expected_sql.update(
                {
                    "series_sync": schema_statements[4],
                    "series": schema_statements[5],
                    "series_username_series_id_idx": schema_statements[6].removesuffix(
                        ";"
                    ),
                    "series_username_status_series_id_idx": schema_statements[
                        7
                    ].removesuffix(";"),
                }
            )
        stored_sql = {
            name: sql
            for name, sql in connection.execute(
                """
                SELECT name, sql FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL
                """
            )
        }
        def normalize(sql: str) -> str:
            return " ".join(sql.split())

        if set(stored_sql) != set(expected_sql) or any(
            normalize(stored_sql[name]) != normalize(sql)
            for name, sql in expected_sql.items()
        ):
            raise sqlite3.DatabaseError("invalid state database schema")

        expected_users = [
            ("username", "TEXT", 1, 1),
            ("cursor_kind", "TEXT", 0, 0),
            ("cursor_value", "TEXT", 0, 0),
        ]
        expected_movies = [
            ("movie_id", "INTEGER", 0, 1),
            ("username", "TEXT", 1, 0),
            ("endpoint", "TEXT", 1, 0),
            ("tmdb_id", "TEXT", 0, 0),
            ("status", "TEXT", 1, 0),
            ("title", "TEXT", 0, 0),
            ("year", "INTEGER", 0, 0),
            ("completion_reason", "TEXT", 0, 0),
        ]

        def columns(table: str) -> list[tuple[Any, ...]]:
            return [
                (row[1], row[2].upper(), row[3], row[5])
                for row in connection.execute(f"PRAGMA table_info({table})")
            ]

        if columns("users") != expected_users or columns("movies") != expected_movies:
            raise sqlite3.DatabaseError("invalid state database columns")
        if version == 3:
            if columns("series_sync") != [
                ("username", "TEXT", 1, 1),
                ("backfill_complete", "INTEGER", 1, 0),
            ] or columns("series") != [
                ("series_id", "INTEGER", 0, 1),
                ("username", "TEXT", 1, 0),
                ("endpoint", "TEXT", 1, 0),
                ("tmdb_id", "TEXT", 0, 0),
                ("status", "TEXT", 1, 0),
                ("completion_reason", "TEXT", 0, 0),
            ]:
                raise sqlite3.DatabaseError("invalid series state database columns")

        foreign_keys = list(connection.execute("PRAGMA foreign_key_list(movies)"))
        if len(foreign_keys) != 1 or (
            foreign_keys[0][2],
            foreign_keys[0][3],
            foreign_keys[0][4],
            foreign_keys[0][6],
        ) != ("users", "username", "username", "CASCADE"):
            raise sqlite3.DatabaseError("invalid state database foreign key")

        indexes = list(connection.execute("PRAGMA index_list(movies)"))
        explicit = next(
            (row for row in indexes if row[1] == "movies_username_movie_id_idx"),
            None,
        )
        if explicit is None or explicit[2] != 0:
            raise sqlite3.DatabaseError("missing state ordering index")
        explicit_columns = [
            row[2]
            for row in connection.execute(
                'PRAGMA index_info("movies_username_movie_id_idx")'
            )
        ]
        if explicit_columns != ["username", "movie_id"]:
            raise sqlite3.DatabaseError("invalid state ordering index")
        if version >= 2:
            status_index = next(
                (
                    row
                    for row in indexes
                    if row[1] == "movies_username_status_movie_id_idx"
                ),
                None,
            )
            if status_index is None or status_index[2] != 0:
                raise sqlite3.DatabaseError("missing state status index")
            status_columns = [
                row[2]
                for row in connection.execute(
                    'PRAGMA index_info("movies_username_status_movie_id_idx")'
                )
            ]
            if status_columns != ["username", "status", "movie_id"]:
                raise sqlite3.DatabaseError("invalid state status index")
        unique_endpoint = False
        for index in indexes:
            if index[2] != 1:
                continue
            index_columns = [
                row[2]
                for row in connection.execute(
                    f'PRAGMA index_info("{index[1]}")'
                )
            ]
            if index_columns == ["username", "endpoint"]:
                unique_endpoint = True
                break
        if not unique_endpoint:
            raise sqlite3.DatabaseError("missing unique movie endpoint index")
        if version == 3:
            for table in ("series_sync", "series"):
                keys = list(connection.execute(f"PRAGMA foreign_key_list({table})"))
                if len(keys) != 1 or (
                    keys[0][2], keys[0][3], keys[0][4], keys[0][6]
                ) != ("users", "username", "username", "CASCADE"):
                    raise sqlite3.DatabaseError("invalid series state database foreign key")
            series_indexes = list(connection.execute("PRAGMA index_list(series)"))
            expected = {
                "series_username_series_id_idx": ["username", "series_id"],
                "series_username_status_series_id_idx": [
                    "username",
                    "status",
                    "series_id",
                ],
            }
            for name, expected_columns in expected.items():
                index = next((row for row in series_indexes if row[1] == name), None)
                if index is None or index[2] != 0:
                    raise sqlite3.DatabaseError("missing series state index")
                actual = [
                    row[2]
                    for row in connection.execute(f'PRAGMA index_info("{name}")')
                ]
                if actual != expected_columns:
                    raise sqlite3.DatabaseError("invalid series state index")

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise sqlite3.ProgrammingError("state database is not initialized")
        return self._connection

    @staticmethod
    def _remove_artifact(path: str) -> None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning(
                "Temporary state database could not be removed",
                extra={"event": "state_temp_cleanup_failed", "stage": "state"},
            )
