# pylint: disable=missing-module-docstring
from src.letterboxd import get_watchlist_tmdb_ids
from src.radarr import check_radarr_state

USERNAME = "mathis_v"

if __name__ == "__main__":
    tmdb_ids = get_watchlist_tmdb_ids(USERNAME)
    print(tmdb_ids)
    print(len(tmdb_ids))
    
    for tmdb_id in tmdb_ids:
        print(check_radarr_state(tmdb_id))