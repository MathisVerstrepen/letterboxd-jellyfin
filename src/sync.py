from collections.abc import Callable
from typing import Any

from src.jellyfin import Jellyfin
from src.letterboxd import (
    extract_tmdb_id_from_endpoint,
    get_new_watchlist_entries,
)
from src.logger import get_logger
from src.proxies import ProxyManager
from src.radarr import RadarrClient
from src.results import StateSaveResult, SyncResult, empty_failures, empty_queue_counts
from src.state_manager import MovieStateChange, StateCheckpoint


def _movie_record(
    status: str,
    tmdb_id: str | None = None,
    title: str | None = None,
    year: int | None = None,
    completion_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "tmdb_id": tmdb_id,
        "status": status,
        "title": title,
        "year": year,
        "completion_reason": completion_reason,
    }


class SyncManager:
    def __init__(
        self,
        user_config: dict[str, Any],
        jellyfin: Jellyfin,
        radarr: RadarrClient,
        user_state: dict[str, Any],
        checkpoint: Callable[[StateCheckpoint], StateSaveResult],
        letterboxd_config: dict[str, Any],
        radarr_config: dict[str, Any],
    ) -> None:
        self.letterboxd_username = user_config["letterboxd_username"]
        self.jellyfin_collection_id = user_config.get("jellyfin_collection_id")
        self.jellyfin_username = user_config.get("jellyfin_username")
        self.user_state = user_state
        self.checkpoint = checkpoint
        self.jellyfin = jellyfin
        self.radarr = radarr
        self.radarr_config = radarr_config
        self.max_workers = letterboxd_config.get("max_concurrent_requests", 5)
        self.proxy_manager = ProxyManager(letterboxd_config)
        self.logger = get_logger("sync")

    def _checkpoint(
        self,
        checkpoint: StateCheckpoint,
        failures: dict[str, int],
        queue_counts: dict[str, int],
    ) -> SyncResult | None:
        result = self.checkpoint(checkpoint)
        if result.failed_items:
            failures["state"] += result.failed_items
            return SyncResult(
                completed=False,
                failures_by_stage=failures,
                queue_counts=queue_counts,
            )
        return None

    def _merge_discovery(self, watchlist) -> StateCheckpoint | None:
        movie_changes = []
        movies = self.user_state["movies"]
        cursor = self.user_state["cursor"]

        if cursor and cursor["kind"] == "legacy_tmdb" and watchlist.boundary_uri:
            boundary = movies.get(watchlist.boundary_uri)
            if boundary and boundary["status"] == "retry_letterboxd":
                del movies[watchlist.boundary_uri]
                movie_changes.append(
                    MovieStateChange("delete", watchlist.boundary_uri)
                )

        for entry in watchlist.entries:
            existing = movies.get(entry.endpoint)
            if existing is None:
                if entry.detail.outcome == "movie":
                    movies[entry.endpoint] = _movie_record(
                        "pending_radarr", tmdb_id=entry.detail.tmdb_id
                    )
                elif entry.detail.outcome == "retry":
                    movies[entry.endpoint] = _movie_record("retry_letterboxd")
                else:
                    continue
                movie_changes.append(
                    MovieStateChange("upsert", entry.endpoint, movies[entry.endpoint])
                )
            elif (
                existing["status"] == "retry_letterboxd"
                and entry.detail.outcome == "movie"
            ):
                movies[entry.endpoint] = _movie_record(
                    "pending_radarr", tmdb_id=entry.detail.tmdb_id
                )
                movie_changes.append(
                    MovieStateChange("upsert", entry.endpoint, movies[entry.endpoint])
                )
            elif (
                existing["status"] == "retry_letterboxd"
                and entry.detail.outcome == "not_movie"
            ):
                del movies[entry.endpoint]
                movie_changes.append(MovieStateChange("delete", entry.endpoint))

        cursor_changed = False
        if watchlist.scan_complete:
            new_cursor = (
                {"kind": "letterboxd", "value": watchlist.cursor_uri}
                if watchlist.cursor_uri
                else None
            )
            if cursor != new_cursor:
                self.user_state["cursor"] = new_cursor
                cursor_changed = True
        if not cursor_changed and not movie_changes:
            return None
        return StateCheckpoint(
            cursor_changed=cursor_changed,
            cursor=self.user_state["cursor"] if cursor_changed else None,
            movie_changes=tuple(movie_changes),
        )

    def _retry_letterboxd(
        self,
        endpoint: str,
        failures: dict[str, int],
        queue_counts: dict[str, int],
    ) -> SyncResult | None:
        detail = extract_tmdb_id_from_endpoint(endpoint, self.proxy_manager)
        if detail.outcome == "retry":
            failures["letterboxd"] += 1
            return None
        if detail.outcome == "not_movie":
            del self.user_state["movies"][endpoint]
            change = MovieStateChange("delete", endpoint)
        else:
            self.user_state["movies"][endpoint] = _movie_record(
                "pending_radarr", tmdb_id=detail.tmdb_id
            )
            change = MovieStateChange(
                "upsert", endpoint, self.user_state["movies"][endpoint]
            )
        return self._checkpoint(StateCheckpoint(movie_changes=(change,)), failures, queue_counts)

    def _process_radarr(
        self,
        endpoint: str,
        failures: dict[str, int],
        queue_counts: dict[str, int],
        queue_totals: dict[str, int],
    ) -> SyncResult | None:
        movie = self.user_state["movies"][endpoint]
        lookup = self.radarr.check_radarr_state(movie["tmdb_id"])
        failures["radarr"] += lookup.failed_items
        if lookup.state is None:
            if movie["status"] != "retry_radarr":
                movie["status"] = "retry_radarr"
                return self._checkpoint(
                    StateCheckpoint(
                        movie_changes=(MovieStateChange("upsert", endpoint, movie),)
                    ),
                    failures,
                    queue_counts,
                )
            return None

        state = lookup.state
        folder_path = self.radarr_config.get("root_folder_path", "")
        animated = self.radarr_config.get("animated_movies", {})
        if animated.get("enabled") and state.get("is_animation"):
            folder_path = animated.get("root_folder_path", folder_path)
        queue_result = self.radarr.add_to_radarr_download_queue(
            [state],
            folder_path,
            self.radarr_config.get("quality_profile_id"),
        )
        queue_counts["radarr_add"] += queue_result.attempted
        queue_totals["attempted"] += queue_result.attempted
        queue_totals["succeeded"] += queue_result.succeeded
        queue_totals["failed_items"] += queue_result.failed_items
        failures["radarr"] += queue_result.failed_items
        if queue_result.succeeded != 1 or queue_result.failed_items:
            if movie["status"] != "retry_radarr":
                movie["status"] = "retry_radarr"
                return self._checkpoint(
                    StateCheckpoint(
                        movie_changes=(MovieStateChange("upsert", endpoint, movie),)
                    ),
                    failures,
                    queue_counts,
                )
            return None

        if not state.get("hasFile"):
            self.user_state["movies"][endpoint] = _movie_record(
                "completed", movie["tmdb_id"], completion_reason="radarr_no_file"
            )
        elif not self.jellyfin_collection_id:
            self.user_state["movies"][endpoint] = _movie_record(
                "completed", movie["tmdb_id"], completion_reason="collection_disabled"
            )
        else:
            self.user_state["movies"][endpoint] = _movie_record(
                "pending_jellyfin",
                movie["tmdb_id"],
                title=state["name"],
                year=state["productionYear"],
            )
        return self._checkpoint(
            StateCheckpoint(
                movie_changes=(
                    MovieStateChange("upsert", endpoint, self.user_state["movies"][endpoint]),
                )
            ),
            failures,
            queue_counts,
        )

    def _process_jellyfin(
        self,
        endpoint: str,
        failures: dict[str, int],
        queue_counts: dict[str, int],
    ) -> SyncResult | None:
        movie = self.user_state["movies"][endpoint]
        if not self.jellyfin_collection_id:
            self.user_state["movies"][endpoint] = _movie_record(
                "completed", movie["tmdb_id"], completion_reason="collection_disabled"
            )
            return self._checkpoint(
                StateCheckpoint(
                    movie_changes=(
                        MovieStateChange(
                            "upsert", endpoint, self.user_state["movies"][endpoint]
                        ),
                    )
                ),
                failures,
                queue_counts,
            )
        try:
            jellyfin_id = self.jellyfin.get_movie_id(movie["title"], movie["year"])
        except Exception:
            failures["jellyfin"] += 1
            if movie["status"] != "retry_jellyfin":
                movie["status"] = "retry_jellyfin"
                return self._checkpoint(
                    StateCheckpoint(
                        movie_changes=(MovieStateChange("upsert", endpoint, movie),)
                    ),
                    failures,
                    queue_counts,
                )
            return None
        if not jellyfin_id:
            self.user_state["movies"][endpoint] = _movie_record(
                "completed",
                movie["tmdb_id"],
                movie["title"],
                movie["year"],
                "jellyfin_not_found",
            )
            return self._checkpoint(
                StateCheckpoint(
                    movie_changes=(
                        MovieStateChange(
                            "upsert", endpoint, self.user_state["movies"][endpoint]
                        ),
                    )
                ),
                failures,
                queue_counts,
            )

        add_result = self.jellyfin.add_to_collection(
            [jellyfin_id], self.jellyfin_collection_id
        )
        queue_counts["jellyfin_add"] += add_result.attempted
        failures["jellyfin"] += add_result.failed_items
        if add_result.succeeded == 1 and not add_result.failed_items:
            self.user_state["movies"][endpoint] = _movie_record(
                "completed",
                movie["tmdb_id"],
                movie["title"],
                movie["year"],
                "jellyfin_added",
            )
        elif movie["status"] != "retry_jellyfin":
            movie["status"] = "retry_jellyfin"
        else:
            return None
        return self._checkpoint(
            StateCheckpoint(
                movie_changes=(
                    MovieStateChange("upsert", endpoint, self.user_state["movies"][endpoint]),
                )
            ),
            failures,
            queue_counts,
        )

    def run(self) -> SyncResult:
        """Run one user's discovery, retryable movie work, and watched removal."""
        failures = empty_failures()
        queue_counts = empty_queue_counts()
        radarr_queue_totals = {"attempted": 0, "succeeded": 0, "failed_items": 0}
        self.logger.info("User sync started", extra={"event": "sync_user_started"})
        if not self.jellyfin_username:
            failures["configuration"] += 1
            return SyncResult(failures_by_stage=failures, queue_counts=queue_counts)

        try:
            watchlist = get_new_watchlist_entries(
                self.letterboxd_username,
                self.proxy_manager,
                self.max_workers,
                self.user_state["cursor"],
            )
            failures["letterboxd"] += watchlist.failed_items
            if watchlist.outcome == "success" and not watchlist.entries:
                message = "No new Letterboxd movies found"
            elif watchlist.outcome == "failed":
                message = "Letterboxd watchlist fetch failed"
            else:
                message = "Letterboxd watchlist scrape completed"
            self.logger.info(
                message,
                extra={
                    "event": "letterboxd_scrape_completed",
                    "count": len(watchlist.entries),
                    "skipped_items": watchlist.skipped_items,
                    "failed_items": watchlist.failed_items,
                    "outcome": watchlist.outcome,
                },
            )
            discovery_checkpoint = self._merge_discovery(watchlist)
            if discovery_checkpoint:
                stopped = self._checkpoint(
                    discovery_checkpoint, failures, queue_counts
                )
                if stopped:
                    return stopped

            endpoints = list(self.user_state["movies"])
            scrape_retry_endpoints = {
                entry.endpoint
                for entry in watchlist.entries
                if entry.detail.outcome == "retry"
            }
            cursor = self.user_state["cursor"]
            for endpoint in endpoints:
                movie = self.user_state["movies"].get(endpoint)
                if movie is None or movie["status"] == "completed":
                    continue
                if movie["status"] == "retry_letterboxd":
                    if (
                        endpoint not in scrape_retry_endpoints
                        and cursor
                        and cursor["kind"] == "letterboxd"
                    ):
                        stopped = self._retry_letterboxd(
                            endpoint, failures, queue_counts
                        )
                        if stopped:
                            return stopped
                        movie = self.user_state["movies"].get(endpoint)
                    if movie is None or movie["status"] == "retry_letterboxd":
                        continue
                if movie["status"] in {"pending_radarr", "retry_radarr"}:
                    stopped = self._process_radarr(
                        endpoint,
                        failures,
                        queue_counts,
                        radarr_queue_totals,
                    )
                    if stopped:
                        return stopped
                    movie = self.user_state["movies"].get(endpoint)
                if movie and movie["status"] in {
                    "pending_jellyfin",
                    "retry_jellyfin",
                }:
                    stopped = self._process_jellyfin(endpoint, failures, queue_counts)
                    if stopped:
                        return stopped

            if radarr_queue_totals["attempted"]:
                if not radarr_queue_totals["failed_items"]:
                    queue_outcome = "success"
                elif radarr_queue_totals["succeeded"]:
                    queue_outcome = "partial"
                else:
                    queue_outcome = "failed"
                log_queue_result = (
                    self.logger.info
                    if queue_outcome == "success"
                    else self.logger.warning
                )
                log_queue_result(
                    "Radarr queue processing completed",
                    extra={
                        "event": "radarr_queue_completed",
                        "outcome": queue_outcome,
                        **radarr_queue_totals,
                    },
                )

            if not self.jellyfin_collection_id:
                return self._completed(failures, queue_counts)
            try:
                user_id = self.jellyfin.get_user_id(self.jellyfin_username)
            except Exception:
                failures["jellyfin"] += 1
                return SyncResult(
                    completed=False,
                    failures_by_stage=failures,
                    queue_counts=queue_counts,
                )
            if not user_id:
                failures["jellyfin"] += 1
                return self._completed(failures, queue_counts)
            played_result = self.jellyfin.get_played_movies_from_collection(
                self.jellyfin_collection_id, user_id
            )
            failures["jellyfin"] += played_result.failed_items
            if played_result.fatal:
                return SyncResult(
                    completed=False,
                    failures_by_stage=failures,
                    queue_counts=queue_counts,
                )
            if played_result.movie_ids:
                remove_result = self.jellyfin.remove_from_collection(
                    played_result.movie_ids, self.jellyfin_collection_id
                )
                queue_counts["jellyfin_remove"] += remove_result.attempted
                failures["jellyfin"] += remove_result.failed_items
                if remove_result.fatal:
                    return SyncResult(
                        completed=False,
                        failures_by_stage=failures,
                        queue_counts=queue_counts,
                    )
            return self._completed(failures, queue_counts)
        except Exception:
            failures["runtime"] += 1
            self.logger.error(
                "Unexpected user sync failure",
                extra={"event": "sync_user_failed", "stage": "runtime"},
                exc_info=True,
            )
            return SyncResult(
                completed=False,
                failures_by_stage=failures,
                queue_counts=queue_counts,
            )

    def _completed(
        self, failures: dict[str, int], queue_counts: dict[str, int]
    ) -> SyncResult:
        failed_items = sum(failures.values())
        self.logger.info(
            "User sync completed",
            extra={
                "event": "sync_user_completed",
                "outcome": "success" if failed_items == 0 else "partial",
                "failed_items": failed_items,
                "queue_counts": queue_counts,
            },
        )
        return SyncResult(
            completed=True,
            failures_by_stage=failures,
            queue_counts=queue_counts,
        )
