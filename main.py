# pylint: disable=missing-module-docstring
from src.letterboxd import get_watchlist_tmdb_ids
from src.radarr import check_radarr_state, add_to_radarr_download_queue
from src.jellyfin import Jellyfin

from src.exceptions import JellyfinException

USERNAME = ["Mathis_V", 'Nimportnawak_']

if __name__ == "__main__":
    jellyfin = Jellyfin()
    username = USERNAME[0]
    
    tmdb_ids = get_watchlist_tmdb_ids(username)
    print(tmdb_ids)
    print(len(tmdb_ids))
    
    radarr_states = []
    for tmdb_id in tmdb_ids:
        radarr_states.append(check_radarr_state(tmdb_id))
    print(radarr_states)
        
    collections_movies = jellyfin.get_collection_movies(username)
    print(collections_movies)
        
    items_to_add = set()
    items_to_download = []
    for state in radarr_states:
        if state["hasFile"]:
            print(state["name"] + " (" + str(state["productionYear"]) + ") is already in the library")
            try :
                jellyfin_id = jellyfin.get_movie_id(state["name"], state["productionYear"])
                print(jellyfin_id)
            except JellyfinException as e:
                print(e)
                continue
                
            if jellyfin_id not in collections_movies:
                items_to_add.add(jellyfin_id)
            else:
                print(state["name"] + " (" + str(state["productionYear"]) + ") is already in the collection")
                
        elif not state["monitored"]:
            print(state["name"] + " (" + str(state["productionYear"]) + ") is not monitored")
            items_to_download.append(state)
        
            
    jellyfin.add_to_collection(items_to_add, username)
    
    add_to_radarr_download_queue(items_to_download)