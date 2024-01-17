# pylint: disable=missing-module-docstring
from src.letterboxd import get_watchlist_tmdb_ids
from src.radarr import check_radarr_state
from src.jellyfin import Jellyfin

from src.exceptions import JellyfinException

USERNAME = "Mathis_V"

if __name__ == "__main__":
    jellyfin = Jellyfin()
    
    tmdb_ids = get_watchlist_tmdb_ids(USERNAME)
    print(tmdb_ids)
    print(len(tmdb_ids))
    
    radarr_states = []
    for tmdb_id in tmdb_ids:
        radarr_states.append(check_radarr_state(tmdb_id))
        
    items_to_add = []
    for state in radarr_states:
        if state["hasFile"]:
            print(state["name"] + " (" + str(state["productionYear"]) + ") is already in the library")
            try :
                jellyfin_id = jellyfin.get_movie_id(state["name"], state["productionYear"])
                print(jellyfin_id)
            except JellyfinException as e:
                print(e)
                
            items_to_add.append(jellyfin_id)
            
    jellyfin.add_to_collection(items_to_add, USERNAME)