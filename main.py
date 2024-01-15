# pylint: disable=missing-module-docstring
from src.letterboxd import get_watchlist_tmdb_ids

USERNAME = "mathis_v"

if __name__ == "__main__":
    tmdb_ids = get_watchlist_tmdb_ids(USERNAME)
    print(tmdb_ids)
    print(len(tmdb_ids))