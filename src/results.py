from dataclasses import dataclass, field
from typing import Any

FAILURE_STAGES = (
    "configuration",
    "letterboxd",
    "radarr",
    "jellyfin",
    "state",
    "runtime",
)
QUEUE_NAMES = ("radarr_add", "jellyfin_add", "jellyfin_remove")


def empty_failures() -> dict[str, int]:
    return {stage: 0 for stage in FAILURE_STAGES}


def empty_queue_counts() -> dict[str, int]:
    return {queue: 0 for queue in QUEUE_NAMES}


@dataclass(frozen=True)
class LetterboxdDetailResult:
    outcome: str
    tmdb_id: str | None = None


@dataclass(frozen=True)
class WatchlistEntry:
    endpoint: str
    detail: LetterboxdDetailResult


@dataclass(frozen=True)
class WatchlistResult:
    entries: list[WatchlistEntry]
    outcome: str
    scan_complete: bool
    cursor_uri: str | None = None
    boundary_uri: str | None = None
    failed_items: int = 0
    skipped_items: int = 0


@dataclass(frozen=True)
class RadarrLookupResult:
    state: dict[str, Any] | None
    failed_items: int = 0


@dataclass(frozen=True)
class MutationResult:
    attempted: int = 0
    succeeded: int = 0
    failed_items: int = 0
    fatal: bool = False


@dataclass(frozen=True)
class PlayedMoviesResult:
    movie_ids: list[str]
    failed_items: int = 0
    fatal: bool = False


@dataclass(frozen=True)
class StateLoadResult:
    data: dict[str, Any]
    failed_items: int = 0
    migrated: bool = False


@dataclass(frozen=True)
class StateInitializationResult:
    failed_items: int = 0
    migrated: bool = False


@dataclass(frozen=True)
class StateSaveResult:
    failed_items: int = 0


@dataclass(frozen=True)
class SyncResult:
    completed: bool = False
    failures_by_stage: dict[str, int] = field(default_factory=empty_failures)
    queue_counts: dict[str, int] = field(default_factory=empty_queue_counts)
