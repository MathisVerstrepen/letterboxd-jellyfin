from typing import Any

from src.jellyfin import Jellyfin
from src.letterboxd import get_new_watchlist_tmdb_ids
from src.logger import setup_logger
from src.proxies import ProxyManager
from src.radarr import RadarrClient
from src.results import SyncResult, empty_failures, empty_queue_counts


class SyncManager:
    def __init__(
        self,
        user_config: dict[str, Any],
        jellyfin: Jellyfin,
        radarr: RadarrClient,
        latest_synced_tmdb_id: str | None,
        letterboxd_config: dict[str, Any],
        radarr_config: dict[str, Any],
    ) -> None:
        self.letterboxd_username = user_config["letterboxd_username"]
        self.jellyfin_collection_id = user_config.get("jellyfin_collection_id")
        self.jellyfin_username = user_config.get("jellyfin_username")
        self.latest_synced_tmdb_id = latest_synced_tmdb_id
        self.jellyfin = jellyfin
        self.radarr = radarr
        self.radarr_config = radarr_config
        self.max_workers = letterboxd_config.get("max_concurrent_requests", 5)
        self.proxy_manager = ProxyManager(letterboxd_config)
        self.logger = setup_logger()

    def run(self) -> SyncResult:
        """Run one user's work while preserving the existing side-effect order."""
        failures = empty_failures()
        queue_counts = empty_queue_counts()
        context = {"letterboxd_username": self.letterboxd_username}
        self.logger.info(
            "User sync started", extra={"event": "sync_user_started", **context}
        )

        if not self.jellyfin_username:
            failures["configuration"] += 1
            self.logger.error(
                "Required Jellyfin username is missing",
                extra={"event": "sync_user_failed", "stage": "configuration", **context},
            )
            return SyncResult(failures_by_stage=failures, queue_counts=queue_counts)

        try:
            watchlist = get_new_watchlist_tmdb_ids(
                self.letterboxd_username,
                self.proxy_manager,
                self.max_workers,
                self.latest_synced_tmdb_id,
            )
            failures["letterboxd"] += watchlist.failed_items
            new_tmdb_ids = watchlist.tmdb_ids
            state_advance_id: str | None = None

            if not new_tmdb_ids:
                self.logger.info(
                    "No new Letterboxd movies found",
                    extra={"event": "letterboxd_scrape_completed", "count": 0, **context},
                )
            else:
                self.logger.info(
                    "New Letterboxd movies found",
                    extra={"event": "letterboxd_scrape_completed", "count": len(new_tmdb_ids), **context},
                )
                radarr_states = []
                for tmdb_id in new_tmdb_ids:
                    lookup = self.radarr.check_radarr_state(tmdb_id)
                    failures["radarr"] += lookup.failed_items
                    if lookup.state is None:
                        continue

                    state = lookup.state
                    folder_path = self.radarr_config.get("root_folder_path", "")
                    animated = self.radarr_config.get("animated_movies", {})
                    if animated.get("enabled") and state.get("is_animation"):
                        folder_path = animated.get("root_folder_path", folder_path)

                    queue_counts["radarr_add"] += 1
                    queue_result = self.radarr.add_to_radarr_download_queue(
                        [state],
                        folder_path,
                        self.radarr_config.get("quality_profile_id"),
                    )
                    failures["radarr"] += queue_result.failed_items
                    radarr_states.append(state)

                if self.jellyfin_collection_id:
                    jellyfin_ids_to_add = []
                    for movie in radarr_states:
                        if not movie.get("hasFile"):
                            continue
                        year = movie.get("productionYear")
                        name = movie.get("name")
                        if year is not None and name is not None:
                            try:
                                jellyfin_id = self.jellyfin.get_movie_id(name, year)
                            except Exception:
                                failures["jellyfin"] += 1
                                self.logger.error(
                                    "Jellyfin library lookup failed",
                                    extra={"event": "sync_user_failed", "stage": "jellyfin", **context},
                                    exc_info=True,
                                )
                                return SyncResult(
                                    completed=False,
                                    failures_by_stage=failures,
                                    queue_counts=queue_counts,
                                )
                            if jellyfin_id:
                                jellyfin_ids_to_add.append(jellyfin_id)

                    if jellyfin_ids_to_add:
                        queue_counts["jellyfin_add"] += len(jellyfin_ids_to_add)
                        add_result = self.jellyfin.add_to_collection(
                            jellyfin_ids_to_add, self.jellyfin_collection_id
                        )
                        failures["jellyfin"] += add_result.failed_items
                        if add_result.fatal:
                            return SyncResult(
                                completed=False,
                                failures_by_stage=failures,
                                queue_counts=queue_counts,
                            )
                else:
                    self.logger.warning(
                        "Jellyfin collection is not configured; skipping additions",
                        extra={"event": "jellyfin_collection_add_skipped", **context},
                    )

                # This remains the newest scraped ID, exactly as in the previous flow.
                state_advance_id = new_tmdb_ids[0]

            if not self.jellyfin_collection_id:
                self.logger.warning(
                    "Jellyfin collection is not configured; skipping watched removal",
                    extra={"event": "jellyfin_collection_remove_skipped", **context},
                )
                return self._completed(state_advance_id, failures, queue_counts)

            try:
                user_id = self.jellyfin.get_user_id(self.jellyfin_username)
            except Exception:
                failures["jellyfin"] += 1
                self.logger.error(
                    "Jellyfin user lookup failed",
                    extra={"event": "sync_user_failed", "stage": "jellyfin", **context},
                    exc_info=True,
                )
                return SyncResult(
                    completed=False,
                    failures_by_stage=failures,
                    queue_counts=queue_counts,
                )

            if not user_id:
                failures["jellyfin"] += 1
                self.logger.error(
                    "Jellyfin user was not found",
                    extra={"event": "jellyfin_user_lookup_failed", "stage": "jellyfin", **context},
                )
                return self._completed(state_advance_id, failures, queue_counts)

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
                queue_counts["jellyfin_remove"] += len(played_result.movie_ids)
                remove_result = self.jellyfin.remove_from_collection(
                    played_result.movie_ids, self.jellyfin_collection_id
                )
                failures["jellyfin"] += remove_result.failed_items
                if remove_result.fatal:
                    return SyncResult(
                        completed=False,
                        failures_by_stage=failures,
                        queue_counts=queue_counts,
                    )

            return self._completed(state_advance_id, failures, queue_counts)
        except Exception:
            failures["runtime"] += 1
            self.logger.error(
                "Unexpected user sync failure",
                extra={"event": "sync_user_failed", "stage": "runtime", **context},
                exc_info=True,
            )
            return SyncResult(
                completed=False,
                failures_by_stage=failures,
                queue_counts=queue_counts,
            )

    def _completed(
        self,
        state_advance_id: str | None,
        failures: dict[str, int],
        queue_counts: dict[str, int],
    ) -> SyncResult:
        failed_items = sum(failures.values())
        self.logger.info(
            "User sync completed",
            extra={
                "event": "sync_user_completed",
                "outcome": "success" if failed_items == 0 else "partial",
                "failed_items": failed_items,
                "queue_counts": queue_counts,
                "letterboxd_username": self.letterboxd_username,
            },
        )
        return SyncResult(
            state_advance_id=state_advance_id,
            completed=True,
            failures_by_stage=failures,
            queue_counts=queue_counts,
        )
