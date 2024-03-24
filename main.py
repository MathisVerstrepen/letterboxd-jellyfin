# pylint: disable=missing-module-docstring
import os
import pathlib
import asyncio
from dotenv import load_dotenv


from src.letterboxd import get_watchlist_tmdb_ids
from src.radarr import check_radarr_state, add_to_radarr_download_queue, RadarrState, get_disk_space
from src.jellyfin import Jellyfin
from src.discord_bot import update_discord_message

from src.exceptions import JellyfinException

JellyfinId = str

USERNAMES = ["Mathis_V", "Nimportnawak_", "arkc0s"]

if pathlib.Path("/.dockerenv").exists():
    print("Running in Docker")
    os.chdir("/app")
    load_dotenv("/app/.env")
else:
    print("Running locally")
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(".env")

if __name__ == "__main__":
    jellyfin = Jellyfin()

    for USERNAME in USERNAMES:
        print("Processing user " + USERNAME)

        # Extract TMDB IDs from Letterboxd watchlist
        tmdb_ids = get_watchlist_tmdb_ids(USERNAME)
        print("Found " + str(len(tmdb_ids)) + " movies in the watchlist")

        # Check status of extracted movies in Radarr
        radarr_states: list[RadarrState] = [
            check_radarr_state(tmdb_id) for tmdb_id in tmdb_ids
        ]
        radarr_states = [state for state in radarr_states if state]
        print(
            "Found "
            + str(len([state for state in radarr_states if state["hasFile"]]))
            + " movies in Jellyfin and "
            + str(len([state for state in radarr_states if not state["hasFile"]]))
            + " movies to download"
        )

        # Get movies in the watchlist collection of the user in Jellyfin
        collections_movies = jellyfin.get_collection_movies(USERNAME)
        print("Found " + str(len(collections_movies)) + " movies in user collection")

        items_with_file: set[JellyfinId] = set()
        items_to_add: set[JellyfinId] = set()
        items_to_download: list[RadarrState] = []
        for state in radarr_states:
            if state["hasFile"]:
                try:
                    jellyfin_id = jellyfin.get_movie_id(
                        state["name"], state["productionYear"]
                    )
                except JellyfinException as exc:
                    print(exc)
                    continue

                if jellyfin_id not in collections_movies:
                    items_to_add.add(jellyfin_id)
                items_with_file.add(jellyfin_id)

            elif not state["monitored"]:
                items_to_download.append(state)

        # Remove movies in the collection that are not in the watchlist
        items_to_remove: set[JellyfinId] = set()
        for movie in collections_movies:
            if movie not in items_with_file:
                items_to_remove.add(movie)
        jellyfin.remove_from_collection(items_to_remove, USERNAME)

        print("Adding " + str(len(items_to_add)) + " new movies to collection")
        jellyfin.add_to_user_collection(items_to_add, USERNAME)

        print("Adding " + str(len(items_to_download)) + " movies to download queue")
        add_to_radarr_download_queue(items_to_download)

    directors = jellyfin.get_directors_stats()

    for director in directors:
        if director[0] in jellyfin.params["directors"]:
            collection = jellyfin.get_collection_by_name(director[0])

            if collection:
                collection_id = collection
                jellyfin.add_to_collection(director[1]["movies"], collection_id)

    movies_stats = jellyfin.get_movies_stats()
    anime_movies_stats = jellyfin.get_animes_movies_stats()
    tv_show_stats = jellyfin.get_tv_show_stats()
    anime_show_stats = jellyfin.get_animes_show_stats()
    disk_stats = get_disk_space("/data")

    asyncio.run(update_discord_message(movies_stats, anime_movies_stats, tv_show_stats, anime_show_stats, disk_stats))
    