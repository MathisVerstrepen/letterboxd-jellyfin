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
from src.results import (
    CompletedEndpointsResult,
    StateSaveResult,
    SyncResult,
    empty_failures,
    empty_queue_counts,
)
from src.state_manager import MovieStateChange, SeriesStateChange, StateCheckpoint


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


def _series_record(
    status: str,
    tmdb_id: str | None = None,
    completion_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "tmdb_id": tmdb_id,
        "status": status,
        "completion_reason": completion_reason,
    }


class SyncManager:
    def __init__(
        self,
        user_config: dict[str, Any],
        jellyfin: Jellyfin,
        radarr: RadarrClient | None,
        user_state: dict[str, Any],
        checkpoint: Callable[[StateCheckpoint], StateSaveResult],
        letterboxd_config: dict[str, Any],
        radarr_config: dict[str, Any] | None,
        *,
        proxy_manager: ProxyManager,
        completed_endpoint_lookup: Callable[
            [tuple[str, ...]], CompletedEndpointsResult
        ],
        sonarr: Any = None,
        sonarr_config: dict[str, Any] | None = None,
        sonarr_enabled: bool = False,
        radarr_enabled: bool = True,
    ) -> None:
        self.letterboxd_username = user_config["letterboxd_username"]
        self.jellyfin_collection_id = user_config.get("jellyfin_collection_id")
        self.jellyfin_username = user_config.get("jellyfin_username")
        self.user_state = user_state
        self.checkpoint = checkpoint
        self.jellyfin = jellyfin
        self.radarr = radarr
        self.radarr_config = radarr_config or {}
        self.radarr_enabled = radarr_enabled
        self.max_workers = letterboxd_config.get("max_concurrent_requests", 5)
        self.proxy_manager = proxy_manager
        self.completed_endpoint_lookup = completed_endpoint_lookup
        self.sonarr = sonarr
        self.sonarr_config = sonarr_config or {}
        self.sonarr_enabled = sonarr_enabled
        self.user_state.setdefault("series", {})
        self.user_state.setdefault("series_backfill_complete", False)
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

    def _merge_discovery(
        self,
        watchlist,
        completed_endpoints: frozenset[str],
        *,
        series_only: bool = False,
        update_cursor: bool = True,
        complete_backfill: bool = False,
    ) -> StateCheckpoint | None:
        movie_changes = []
        series_changes = []
        movies = self.user_state["movies"]
        series_state = self.user_state["series"]
        cursor = self.user_state["cursor"]

        if (
            self.radarr_enabled
            and cursor
            and cursor["kind"] == "legacy_tmdb"
            and watchlist.boundary_uri
        ):
            boundary = movies.get(watchlist.boundary_uri)
            if boundary and boundary["status"] == "retry_letterboxd":
                del movies[watchlist.boundary_uri]
                movie_changes.append(
                    MovieStateChange("delete", watchlist.boundary_uri)
                )

        for entry in watchlist.entries:
            if entry.endpoint in completed_endpoints:
                continue
            existing = movies.get(entry.endpoint)
            existing_series = series_state.get(entry.endpoint)
            if not self.radarr_enabled and existing is not None:
                continue
            if entry.detail.outcome == "retry":
                if existing is not None or existing_series is not None:
                    continue
                target = series_state if series_only else movies
                changes = series_changes if series_only else movie_changes
                target[entry.endpoint] = (
                    _series_record("retry_letterboxd")
                    if series_only
                    else _movie_record("retry_letterboxd")
                )
                change_type = SeriesStateChange if series_only else MovieStateChange
                changes.append(change_type("upsert", entry.endpoint, target[entry.endpoint]))
                continue
            if entry.detail.media_type == "series":
                if existing and existing["status"] == "retry_letterboxd":
                    del movies[entry.endpoint]
                    movie_changes.append(MovieStateChange("delete", entry.endpoint))
                if not self.sonarr_enabled:
                    continue
                if existing_series is None or existing_series["status"] == "retry_letterboxd":
                    series_state[entry.endpoint] = _series_record(
                        "pending_sonarr", entry.detail.tmdb_id
                    )
                    series_changes.append(
                        SeriesStateChange(
                            "upsert", entry.endpoint, series_state[entry.endpoint]
                        )
                    )
                continue
            if entry.detail.media_type == "movie":
                if existing_series and existing_series["status"] == "retry_letterboxd":
                    del series_state[entry.endpoint]
                    series_changes.append(SeriesStateChange("delete", entry.endpoint))
                if series_only:
                    continue
                if existing is None:
                    movies[entry.endpoint] = _movie_record(
                        "pending_radarr", tmdb_id=entry.detail.tmdb_id
                    )
                    movie_changes.append(
                        MovieStateChange("upsert", entry.endpoint, movies[entry.endpoint])
                    )
                elif existing["status"] == "retry_letterboxd":
                    movies[entry.endpoint] = _movie_record(
                        "pending_radarr", tmdb_id=entry.detail.tmdb_id
                    )
                    movie_changes.append(
                        MovieStateChange("upsert", entry.endpoint, movies[entry.endpoint])
                    )

        cursor_changed = False
        if update_cursor and watchlist.scan_complete:
            new_cursor = (
                {"kind": "letterboxd", "value": watchlist.cursor_uri}
                if watchlist.cursor_uri
                else None
            )
            if cursor != new_cursor:
                self.user_state["cursor"] = new_cursor
                cursor_changed = True
        marker_changed = complete_backfill and watchlist.scan_complete and not self.user_state[
            "series_backfill_complete"
        ]
        if marker_changed:
            self.user_state["series_backfill_complete"] = True
        if not cursor_changed and not movie_changes and not series_changes and not marker_changed:
            return None
        return StateCheckpoint(
            cursor_changed=cursor_changed,
            cursor=self.user_state["cursor"] if cursor_changed else None,
            movie_changes=tuple(movie_changes),
            series_changes=tuple(series_changes),
            series_backfill_changed=marker_changed,
            series_backfill_complete=marker_changed,
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
        if detail.media_type == "series":
            del self.user_state["movies"][endpoint]
            movie_change = MovieStateChange("delete", endpoint)
            series_changes = ()
            if self.sonarr_enabled:
                self.user_state["series"][endpoint] = _series_record(
                    "pending_sonarr", detail.tmdb_id
                )
                series_changes = (
                    SeriesStateChange(
                        "upsert", endpoint, self.user_state["series"][endpoint]
                    ),
                )
            return self._checkpoint(
                StateCheckpoint(
                    movie_changes=(movie_change,), series_changes=series_changes
                ),
                failures,
                queue_counts,
            )
        else:
            self.user_state["movies"][endpoint] = _movie_record(
                "pending_radarr", tmdb_id=detail.tmdb_id
            )
            change = MovieStateChange(
                "upsert", endpoint, self.user_state["movies"][endpoint]
            )
        return self._checkpoint(StateCheckpoint(movie_changes=(change,)), failures, queue_counts)

    def _retry_series_letterboxd(
        self,
        endpoint: str,
        failures: dict[str, int],
        queue_counts: dict[str, int],
    ) -> SyncResult | None:
        detail = extract_tmdb_id_from_endpoint(endpoint, self.proxy_manager)
        if detail.outcome == "retry":
            failures["letterboxd"] += 1
            return None
        if detail.media_type == "movie":
            del self.user_state["series"][endpoint]
            change = SeriesStateChange("delete", endpoint)
        else:
            self.user_state["series"][endpoint] = _series_record(
                "pending_sonarr", detail.tmdb_id
            )
            change = SeriesStateChange(
                "upsert", endpoint, self.user_state["series"][endpoint]
            )
        return self._checkpoint(
            StateCheckpoint(series_changes=(change,)), failures, queue_counts
        )

    def _process_sonarr(
        self,
        endpoint: str,
        failures: dict[str, int],
        queue_counts: dict[str, int],
    ) -> SyncResult | None:
        if self.sonarr is None:
            return None
        item = self.user_state["series"][endpoint]
        lookup = self.sonarr.check_sonarr_state(item["tmdb_id"])
        failures["sonarr"] += lookup.failed_items
        succeeded = lookup.installed
        if lookup.resource is not None and not lookup.installed:
            folder_path = self.sonarr_config["root_folder_path"]
            animated_tv = self.sonarr_config.get("animated_tv", {})
            if animated_tv.get("enabled") is True and lookup.resource.get(
                "is_animation"
            ) is True:
                folder_path = animated_tv["root_folder_path"]
            result = self.sonarr.add_to_sonarr_download_queue(
                lookup.resource,
                folder_path,
                self.sonarr_config["quality_profile_id"],
            )
            queue_counts["sonarr_add"] += result.attempted
            failures["sonarr"] += result.failed_items
            succeeded = result.succeeded == 1 and not result.failed_items
        if succeeded:
            self.user_state["series"][endpoint] = _series_record(
                "completed", item["tmdb_id"], "sonarr_processed"
            )
        elif item["status"] != "retry_sonarr":
            item["status"] = "retry_sonarr"
        else:
            return None
        return self._checkpoint(
            StateCheckpoint(
                series_changes=(
                    SeriesStateChange("upsert", endpoint, self.user_state["series"][endpoint]),
                )
            ),
            failures,
            queue_counts,
        )

    def _process_radarr(
        self,
        endpoint: str,
        failures: dict[str, int],
        queue_counts: dict[str, int],
        queue_totals: dict[str, int],
    ) -> SyncResult | None:
        if self.radarr is None:
            return None
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
            original_cursor = self.user_state["cursor"]
            discovery_passes = []
            include_movies = self.radarr_enabled
            sonarr_only = self.sonarr_enabled and not self.radarr_enabled
            if self.sonarr_enabled and not self.user_state["series_backfill_complete"]:
                if original_cursor is None:
                    discovery_passes.append(
                        (
                            get_new_watchlist_entries(
                                self.letterboxd_username,
                                self.proxy_manager,
                                self.max_workers,
                                None,
                                include_series=True,
                                include_movies=include_movies,
                            ),
                            sonarr_only,
                            True,
                            True,
                        )
                    )
                else:
                    discovery_passes.append(
                        (
                            get_new_watchlist_entries(
                                self.letterboxd_username,
                                self.proxy_manager,
                                self.max_workers,
                                None,
                                include_series=True,
                                include_movies=False,
                            ),
                            True,
                            False,
                            True,
                        )
                    )
                    discovery_passes.append(
                        (
                            get_new_watchlist_entries(
                                self.letterboxd_username,
                                self.proxy_manager,
                                self.max_workers,
                                original_cursor,
                                include_series=True,
                                include_movies=include_movies,
                            ),
                            sonarr_only,
                            True,
                            False,
                        )
                    )
            elif self.sonarr_enabled:
                discovery_passes.append(
                    (
                        get_new_watchlist_entries(
                            self.letterboxd_username,
                            self.proxy_manager,
                            self.max_workers,
                            original_cursor,
                            include_series=True,
                            include_movies=include_movies,
                        ),
                        sonarr_only,
                        True,
                        False,
                    )
                )
            else:
                discovery_passes.append(
                    (
                        get_new_watchlist_entries(
                            self.letterboxd_username,
                            self.proxy_manager,
                            self.max_workers,
                            original_cursor,
                        ),
                        False,
                        True,
                        False,
                    )
                )

            watchlist = discovery_passes[-1][0]
            failures["letterboxd"] += sum(item[0].failed_items for item in discovery_passes)
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
            for discovered, series_only, update_cursor, complete_backfill in discovery_passes:
                completed_endpoints = frozenset()
                if discovered.entries:
                    pending_endpoints = {
                        *self.user_state["movies"],
                        *self.user_state["series"],
                    }
                    candidates = tuple(
                        dict.fromkeys(
                            entry.endpoint
                            for entry in discovered.entries
                            if entry.endpoint not in pending_endpoints
                        )
                    )
                    if candidates:
                        completed_lookup = self.completed_endpoint_lookup(candidates)
                        failures["state"] += completed_lookup.failed_items
                        if completed_lookup.failed_items:
                            return SyncResult(
                                completed=False,
                                failures_by_stage=failures,
                                queue_counts=queue_counts,
                            )
                        completed_endpoints = completed_lookup.endpoints
                discovery_checkpoint = self._merge_discovery(
                    discovered,
                    completed_endpoints,
                    series_only=series_only,
                    update_cursor=update_cursor,
                    complete_backfill=complete_backfill,
                )
                if discovery_checkpoint:
                    stopped = self._checkpoint(
                        discovery_checkpoint, failures, queue_counts
                    )
                    if stopped:
                        return stopped

            endpoints = list(self.user_state["movies"]) if self.radarr_enabled else []
            scrape_retry_endpoints = {
                entry.endpoint
                for item in discovery_passes
                for entry in item[0].entries
                if entry.detail.outcome == "retry"
            }
            cursor = self.user_state["cursor"]
            for endpoint in endpoints:
                movie = self.user_state["movies"].get(endpoint)
                if movie is None or movie["status"] == "completed":
                    continue
                if movie["status"] == "retry_letterboxd":
                    if self.radarr is None:
                        continue
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
                if (
                    self.radarr is not None
                    and movie["status"] in {"pending_radarr", "retry_radarr"}
                ):
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

            if self.sonarr_enabled:
                for endpoint in list(self.user_state["series"]):
                    item = self.user_state["series"].get(endpoint)
                    if item is None or item["status"] == "completed":
                        continue
                    if item["status"] == "retry_letterboxd":
                        if endpoint not in scrape_retry_endpoints:
                            stopped = self._retry_series_letterboxd(
                                endpoint, failures, queue_counts
                            )
                            if stopped:
                                return stopped
                            item = self.user_state["series"].get(endpoint)
                        if item is None or item["status"] == "retry_letterboxd":
                            continue
                    if item["status"] in {"pending_sonarr", "retry_sonarr"}:
                        stopped = self._process_sonarr(endpoint, failures, queue_counts)
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
