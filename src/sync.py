from src.config import config
from src.logger import setup_logger
from src.letterboxd import get_watchlist_tmdb_ids
from src.radarr import RadarrClient
from src.jellyfin import Jellyfin
from src.proxies import ProxyManager

class SyncManager:
    def __init__(self, user_config: dict, jellyfin: Jellyfin, radarr: RadarrClient):
        self.user_config = user_config
        self.letterboxd_username = user_config["letterboxd_username"]
        self.jellyfin_collection_name = user_config["jellyfin_collection_name"]
        self.jellyfin = jellyfin
        self.radarr = radarr
        self.logger = setup_logger()
        
        letterboxd_config = config.get("letterboxd", {})
        self.max_workers = letterboxd_config.get("max_concurrent_requests", 5)
        self.proxy_manager = ProxyManager(letterboxd_config)

    def run(self):
        """Orchestrates the sync process for a single user."""
        self.logger.info(f"[{self.letterboxd_username}] Starting sync...")

        # 1. Get Letterboxd Watchlist
        tmdb_ids = get_watchlist_tmdb_ids(self.letterboxd_username, self.proxy_manager, self.max_workers)
        self.logger.info(
            f"[{self.letterboxd_username}] Found {len(tmdb_ids)} movies in Letterboxd watchlist."
        )

        # 2. Check Radarr Status
        radarr_states = self.radarr.get_movies_state(tmdb_ids)
        self.logger.info(
            f"[{self.letterboxd_username}] Found {len(radarr_states)} movies in Radarr."
        )

        # 3. Get or Create Jellyfin Collection
        collection_id = self.jellyfin.get_or_create_collection(
            self.jellyfin_collection_name
        )
        if not collection_id:
            self.logger.error(
                f"[{self.letterboxd_username}] Could not find or create Jellyfin collection '{self.jellyfin_collection_name}'. Aborting."
            )
            return

        # 4. Sync logic...
        # ... The rest of the logic for comparing lists, adding/removing from
        # ... Jellyfin, and adding to Radarr's download queue goes here.
        # ... This logic will use the class instance variables (self.jellyfin, self.radarr).

        self.logger.info(f"[{self.letterboxd_username}] Sync complete.")
