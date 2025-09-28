from src.config import config
from src.logger import setup_logger
from src.letterboxd import get_new_watchlist_tmdb_ids
from src.radarr import RadarrClient
from src.jellyfin import Jellyfin
from src.proxies import ProxyManager


class SyncManager:
    def __init__(
        self,
        user_config: dict,
        jellyfin: Jellyfin,
        radarr: RadarrClient,
        latest_synced_tmdb_id: str | None,
    ):
        self.user_config = user_config
        self.letterboxd_username = user_config["letterboxd_username"]
        self.jellyfin_collection_id = user_config["jellyfin_collection_id"]
        self.jellyfin_username = user_config.get("jellyfin_username")
        self.latest_synced_tmdb_id = latest_synced_tmdb_id

        self.jellyfin = jellyfin
        self.radarr = radarr
        self.logger = setup_logger()

        letterboxd_config = config.get("letterboxd", {})
        self.max_workers = letterboxd_config.get("max_concurrent_requests", 5)
        self.proxy_manager = ProxyManager(letterboxd_config)

    def run(self) -> str | None:
        """
        Orchestrates the sync process for a single user.
        Returns the new latest_synced_tmdb_id if it has changed, otherwise None.
        """
        self.logger.info(f"[{self.letterboxd_username}] Starting sync...")
        new_latest_id = None

        if not self.jellyfin_username:
            self.logger.error(
                f"[{self.letterboxd_username}] 'jellyfin_username' is not defined in config. Aborting."
            )
            return None

        # 1. Get ONLY NEW movies from Letterboxd Watchlist
        new_tmdb_ids = get_new_watchlist_tmdb_ids(
            self.letterboxd_username,
            self.proxy_manager,
            self.max_workers,
            self.latest_synced_tmdb_id,
        )

        if not new_tmdb_ids:
            self.logger.info(
                f"[{self.letterboxd_username}] No new movies found on Letterboxd watchlist."
            )
        else:
            self.logger.info(
                f"[{self.letterboxd_username}] Found {len(new_tmdb_ids)} new movies on Letterboxd watchlist."
            )

            # 2. Process new movies: get Radarr state and immediately request download
            radarr_states_for_new_movies = []
            radarr_config = config.get("radarr", {})

            for tmdb_id in new_tmdb_ids:
                state = self.radarr.check_radarr_state(tmdb_id)
                if state:
                    # Immediately request in Radarr, mimicking Go version
                    self.radarr.add_to_radarr_download_queue(
                        [state],
                        radarr_config.get("root_folder_path"),
                        radarr_config.get("quality_profile_id"),
                    )
                    radarr_states_for_new_movies.append(state)

            # 3. Add newly available movies to Jellyfin collection
            if self.jellyfin_collection_id:
                jellyfin_ids_to_add = []
                for movie in radarr_states_for_new_movies:
                    if movie.get("hasFile"):
                        production_year = movie.get("productionYear")
                        movie_name = movie.get("name")
                        if production_year is not None and movie_name is not None:
                            jellyfin_id = self.jellyfin.get_movie_id(
                                movie_name, production_year
                            )
                            if jellyfin_id:
                                jellyfin_ids_to_add.append(jellyfin_id)

                if jellyfin_ids_to_add:
                    self.logger.info(
                        f"[{self.letterboxd_username}] Adding {len(jellyfin_ids_to_add)} new and available movies to Jellyfin collection."
                    )
                    self.jellyfin.add_to_collection(
                        jellyfin_ids_to_add, self.jellyfin_collection_id
                    )
            else:
                self.logger.warning(
                    f"[{self.letterboxd_username}] 'jellyfin_collection_id' is not defined in config. Skipping Jellyfin addition."
                )

            # Set the new latest ID to be returned
            new_latest_id = new_tmdb_ids[0]
            self.logger.info(
                f"[{self.letterboxd_username}] New latest synced movie TMDB ID: {new_latest_id}"
            )

        if not self.jellyfin_collection_id:
            self.logger.warning(
                f"[{self.letterboxd_username}] Collection '{self.jellyfin_collection_id}' not found for watched movie removal scan."
            )
            return new_latest_id  # Return the new ID even if this part fails

        # 5. Remove WATCHED movies from the Jellyfin collection
        user_id = self.jellyfin.get_user_id(self.jellyfin_username)
        if not user_id:
            self.logger.error(
                f"[{self.letterboxd_username}] Could not find Jellyfin user ID for '{self.jellyfin_username}'."
            )
            return new_latest_id

        played_movie_ids = self.jellyfin.get_played_movies_from_collection(
            self.jellyfin_collection_id, user_id
        )
        if played_movie_ids:
            self.logger.info(
                f"[{self.letterboxd_username}] Removing {len(played_movie_ids)} watched movies from Jellyfin collection."
            )
            self.jellyfin.remove_from_collection(
                played_movie_ids, self.jellyfin_collection_id
            )
        else:
            self.logger.info(
                f"[{self.letterboxd_username}] No new watched movies to remove from collection."
            )

        self.logger.info(f"[{self.letterboxd_username}] Sync complete.")
        return new_latest_id
