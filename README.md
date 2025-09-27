![ReadMe Banner](https://github.com/MathisVerstrepen/github-visual-assets/blob/main/banner/Letterboxd-Jellyfin.png?raw=true)

# Letterboxd-Jellyfin Integration

This is a simple script that allows you to import your Letterboxd watchlist into Jellyfin. It will create a new Jellyfin collection for each Letterboxd user and add all movies from their watchlist to it. 

![Splitter-1](https://raw.githubusercontent.com/MathisVerstrepen/github-visual-assets/main/splitter/splitter-1.png)

## Features

- Scrapes Letterboxd watchlist 
- Link Letterboxd movies to Radarr
- Create Jellyfin collections for each Letterboxd user
- Add movies to Jellyfin collections

![Splitter-1](https://raw.githubusercontent.com/MathisVerstrepen/github-visual-assets/main/splitter/splitter-1.png)

## How it works

- The script will scrape the Letterboxd watchlist of each user and obtain the tmdb ID of each movie
- Then for each movie it will search for a match in Radarr and obtain information about file presence and monitored status
- The script will then get all movies from the actual Jellyfin user watchlist collection 
- For each movie that has a file in Radarr, we search for a corresponding movie in Jellyfin and get its ID
- We remove all movies from the collection that are not in the Letterboxd watchlist
- We add all movies to the collection that are in the Letterboxd watchlist but not in the Jellyfin watchlist and are already downloaded in Radarr
- We add all movies to the download queue in Radarr if they are not monitored yet

Detailed information about the process can be found in here :

```mermaid
%%{ init : { "theme" : "default" }}%%
sequenceDiagram
    actor Scheduler
    participant main as "main.py"
    participant letterboxd as "letterboxd.py"
    participant proxies as "proxies.py"
    participant radarr as "radarr.py"
    participant jellyfin as "jellyfin.py"
    participant discord as "discord_bot.py"

    Scheduler->>main: Run script

    Note over main, radarr: Phase 1: User Watchlist Synchronization

    loop for each username
        %% 1. Get Letterboxd Watchlist
        main->>letterboxd: get_watchlist_tmdb_ids(username)
        activate letterboxd
        loop for each watchlist page
            letterboxd->>proxies: make_request(watchlist_page_url)
            activate proxies
            proxies-->>letterboxd: HTML content
            deactivate proxies
            
            par for each movie on page
                letterboxd->>proxies: make_request(movie_page_url)
                activate proxies
                proxies-->>letterboxd: HTML content
                deactivate proxies
            end
        end
        letterboxd-->>main: Set of tmdb_ids
        deactivate letterboxd

        %% 2. Check movie status in Radarr
        loop for each tmdb_id
            main->>radarr: check_radarr_state(tmdb_id)
            activate radarr
            radarr-->>main: RadarrState
            deactivate radarr
        end

        %% 3. Sync with Jellyfin Collection
        main->>jellyfin: get_collection_movies(username)
        activate jellyfin
        jellyfin-->>main: Set of existing Jellyfin IDs
        deactivate jellyfin

        loop for each RadarrState
            opt if movie has file
                main->>jellyfin: get_movie_id(name, year)
                activate jellyfin
                jellyfin-->>main: JellyfinId
                deactivate jellyfin
            end
        end
        note over main: Build lists: items_to_add,<br>items_to_remove, items_to_download

        main->>jellyfin: remove_from_collection(items_to_remove, username)
        activate jellyfin
        jellyfin-->>main: 
        deactivate jellyfin

        main->>jellyfin: add_to_user_collection(items_to_add, username)
        activate jellyfin
        jellyfin-->>main: 
        deactivate jellyfin

        %% 4. Add missing movies to Radarr
        main->>radarr: add_to_radarr_download_queue(items_to_download)
        activate radarr
        radarr-->>main: 
        deactivate radarr
    end

    Note over main, discord: Phase 2: Post-Sync Tasks & Reporting

    %% 5. Update Director Collections
    main->>jellyfin: get_directors_stats()
    activate jellyfin
    jellyfin-->>main: Director stats
    deactivate jellyfin
    
    loop for each specified director
        main->>jellyfin: get_collection_by_name(director_name)
        activate jellyfin
        jellyfin-->>main: Collection ID or None
        deactivate jellyfin

        main->>jellyfin: add_to_collection(movie_ids, collection_id)
        activate jellyfin
        jellyfin-->>main: 
        deactivate jellyfin
    end

    %% 6. Gather Library Stats
    main->>jellyfin: get_movies_stats()
    activate jellyfin
    jellyfin-->>main: movies_stats
    deactivate jellyfin
    
    note right of jellyfin: Get stats for animes, TV shows, etc.
    main->>jellyfin: get...stats()
    
    main->>radarr: get_disk_space()
    activate radarr
    radarr-->>main: disk_stats
    deactivate radarr

    %% 7. Update Discord
    main->>discord: update_discord_message(all_stats)
    activate discord
    discord-->>main: 
    deactivate discord
```

![Splitter-1](https://raw.githubusercontent.com/MathisVerstrepen/github-visual-assets/main/splitter/splitter-1.png)


## Deployment

This script is not meant to be run on another system than mine. It is not very user friendly and I have no intention of making it so. If you want to use it, you will have to modify it to suit your needs. 

Currently, the script is deployed as a docker container and run as a cronjob every 10 minutes. The docker container is built automatically using my custom deployment pipeline that can be found [here](https://github.com/MathisVerstrepen/ApolloLaunchCore).
