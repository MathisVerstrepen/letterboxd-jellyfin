# pylint: disable=missing-module-docstring
from typing import TypedDict
import os
import json
import requests

from src.exceptions import RadarrException

# get env variables from .env file

RADARR_URL = "http://192.168.2.64:7878/api/v3/"

class RadarrState(TypedDict):
    """
    Data describing the existence of a movie in the Radarr library
    """

    hasFile: bool
    monitored: bool
    name: str
    tmdbId: int
    productionYear: int
    is_animation: bool


def check_radarr_state(tmdb_id: str) -> RadarrState:
    """
    Check if a file exists at a given URL
    """

    url = RADARR_URL + "movie/lookup"
    params = {"term": "tmdb:" + tmdb_id}
    headers = {
        "X-Api-Key": os.getenv("RADARR_API_KEY"),
    }
    response = requests.get(url, params=params, headers=headers, timeout=20)
    if response.status_code != 200:
        print(response.status_code)
        print(response.content)
        raise RadarrException("Unable to make request to " + url)

    res = response.json()
    if len(res) == 0:
        print("No results found for " + tmdb_id)
        return RadarrState()

    return {
        "hasFile": res[0].get("movieFile", {}).get("relativePath", None) is not None,
        "monitored": res[0]["monitored"],
        "name": res[0]["title"],
        "tmdbId": res[0]["tmdbId"],
        "productionYear": res[0]["year"],
        "is_animation": "Animation" in res[0]["genres"],
    }


def add_to_radarr_download_queue(movies: list[str]) -> None:
    """
    Add a movie to the download queue
    """

    with open("params.json", "r", encoding="utf-8") as file:
        params = json.load(file)

    bodies = [
        {
            "tmdbId": movie["tmdbId"],
            "title": movie["name"],
            "year": movie["productionYear"],
            "qualityProfileId": 11,
            "monitored": True,
            "rootFolderPath": params["radarr_root_paths"][
                "movies" if not movie["is_animation"] else "anime_movies"
            ],
            "addOptions": {"searchForMovie": True},
        }
        for movie in movies
    ]

    url = RADARR_URL + "movie"
    headers = {
        "X-Api-Key": os.getenv("RADARR_API_KEY"),
    }

    for body in bodies:
        response = requests.post(url, json=body, headers=headers, timeout=20)
        if response.status_code != 201:
            print(response.status_code)
            print(response.content)
            
def get_disk_space(folder: str) -> dict:
    """ Get the disk space of the server where Radarr is running
    
    Params:
        folder (str): The folder to check the disk space of

    Raises:
        RadarrException: Unable to make request to /api/v3/diskspace

    Returns:
        dict: The response from the API in JSON format
    """
    url = RADARR_URL + "diskspace"
    headers = {
        "X-Api-Key": os.getenv("RADARR_API_KEY"),
    }
    response = requests.get(url, headers=headers, timeout=20)
    if response.status_code != 200:
        print(response.status_code)
        print(response.content)
        raise RadarrException("Unable to make request to " + url)
    
    res = response.json()
    
    folder_stats = {}
    for disk in res:
        if disk["path"] == folder:
            folder_stats = disk
            break
        
    return folder_stats