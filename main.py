# pylint: disable=missing-module-docstring
from src.letterboxd import get_watchlist_tmdb_ids
from src.radarr import check_radarr_state, add_to_radarr_download_queue, RadarrState
from src.jellyfin import Jellyfin

from src.exceptions import JellyfinException

JellyfinId = str

USERNAMES = ["Mathis_V", "Nimportnawak_"]

if __name__ == "__main__":
    jellyfin = Jellyfin()
    USERNAME = USERNAMES[1]

    # Extract TMDB IDs from Letterboxd watchlist
    tmdb_ids = get_watchlist_tmdb_ids(USERNAME)
    print("Found " + str(len(tmdb_ids)) + " movies in the watchlist")

    # Check status of extracted movies in Radarr
    radarr_states: list[RadarrState] = [
        check_radarr_state(tmdb_id) for tmdb_id in tmdb_ids
    ]
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
    jellyfin.add_to_collection(items_to_add, USERNAME)

    print("Adding " + str(len(items_to_download)) + " movies to download queue")
    add_to_radarr_download_queue(items_to_download)
