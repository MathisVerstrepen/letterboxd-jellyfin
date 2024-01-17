# pylint: disable=missing-module-docstring
from typing import TypedDict
import os
import requests
from dotenv import load_dotenv

from src.exceptions import RadarrException

# get env variables from .env file

load_dotenv()

RADARR_URL = "http://192.168.2.64:7878/api/v3/"

headers = {
    "X-Api-Key": os.getenv("RADARR_API_KEY"),
}

class FileExistsResponse(TypedDict):
    """
    Data describing the existence of a movie in the Radarr library
    """

    hasFile: bool
    monitored: bool
    name: str
    tmdbId: int


def check_radarr_state(tmdb_id: str) -> FileExistsResponse:
    """
    Check if a file exists at a given URL
    """

    url = RADARR_URL + "movie/lookup"
    params = {"term": "tmdb:" + tmdb_id}
    response = requests.get(url, params=params, headers=headers, timeout=5)
    if response.status_code != 200:
        print(response.status_code)
        print(response.content)
        raise RadarrException("Unable to make request to " + url)

    res = response.json()
    if len(res) == 0:
        raise RadarrException("No results found for " + tmdb_id)

    return {
        "hasFile": res[0]["hasFile"],
        "monitored": res[0]["monitored"],
        "name": res[0]["title"],
        "tmdbId": res[0]["tmdbId"],
        "productionYear": res[0]["year"],
    }