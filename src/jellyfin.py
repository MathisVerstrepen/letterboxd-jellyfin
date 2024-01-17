# pylint: disable=missing-module-docstring
import os
import json
import requests
from dotenv import load_dotenv

from src.exceptions import RadarrException, JellyfinException

JELLYFIN_URL = "https://stream.diikstra.fr/"

load_dotenv()

headers = {
    "Authorization": f'MediaBrowser Token="{os.getenv("JELLYFIN_API_KEY")}"',
}



class Jellyfin():
    def __init__(self) -> None:
        # Get all movies in the library
        self.movies = self.get_movies()
        
        with open("params.json", "r", encoding="utf-8") as file:
            self.params = json.load(file)
        
    def get_movies(self) -> list[dict]:
        """
        Get all movies in the Jellyfin library
        """

        url = JELLYFIN_URL + "Items" 
        params = {"Recursive": "true", "IncludeItemTypes": "Movie"}
        response = requests.get(url, params=params, headers=headers, timeout=5)
        if response.status_code != 200:
            print(response.status_code)
            print(response.content)
            raise RadarrException("Unable to make request to " + url)
        
        res = response.json()
            
        return res
    
    def get_movie_id(self, movie_name: str, movie_year: str) -> str:
        """
        Get the Jellyfin ID of a movie from its name and year
        """

        for movie in self.movies["Items"]:
            if movie["Name"] == movie_name and movie["ProductionYear"] == movie_year:
                return movie["Id"]
        
        raise JellyfinException("Unable to find movie " + movie_name + " (" + str(movie_year) + ") in the Jellyfin library")
    
    def add_to_collection(self, movie_ids: list[str], username: str) -> None:
        """
        Add a movie to a collection
        """
        
        user_collection_id = self.params["collection_ids"][username]
        if user_collection_id is None:
            raise JellyfinException("Unable to find collection ID for " + username)

        url = JELLYFIN_URL + "Collections/" + user_collection_id + "/Items"
        params = {"ids": ",".join(movie_ids)}
        response = requests.post(url, headers=headers, params=params, timeout=5)
        if response.status_code != 204:
            print(response.status_code)
            print(response.content)
            raise RadarrException("Unable to make request to " + url)