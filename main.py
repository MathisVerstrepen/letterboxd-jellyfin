from src.config import config
from src.logger import setup_logger
from src.radarr import RadarrClient
from src.jellyfin import Jellyfin
from src.sync import SyncManager

JellyfinId = str

# Initialize logger
logger = setup_logger(config.get("system", {}).get("log_level", "INFO"))

if __name__ == "__main__":
    logger.info("--- Starting Letterboxd-Jellyfin Sync ---")

    # Initialize API clients from config
    try:
        jellyfin_client = Jellyfin(
            url=config["jellyfin"]["url"], api_key=config["jellyfin"]["api_key"]
        )
        radarr_client = RadarrClient(
            url=config["radarr"]["url"], api_key=config["radarr"]["api_key"]
        )
    except KeyError as e:
        logger.error(f"Configuration error: Missing required key {e} in config.yaml")
        exit(1)

    # Loop through users defined in the config file
    for user_config in config.get("users", []):
        username = user_config.get("letterboxd_username")
        if not username:
            logger.warning("Skipping user entry with no 'letterboxd_username'")
            continue

        logger.info(f"Processing user: {username}")
        try:
            manager = SyncManager(user_config, jellyfin_client, radarr_client)
            manager.run()
        except Exception as e:
            logger.error(
                f"An unexpected error occurred for user {user_config.get('letterboxd_username')}: {e}",
                exc_info=True,
            )

    logger.info("--- Sync process finished ---")
