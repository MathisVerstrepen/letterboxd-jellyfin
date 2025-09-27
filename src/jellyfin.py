import os
import json
import sys
from pathlib import Path
import requests

root_path = Path(__file__).parent.parent
sys.path.append(str(root_path))

from src.exceptions import RadarrException, JellyfinException

JELLYFIN_URL = "https://stream.diikstra.fr/"


class Jellyfin:
    def __init__(self) -> None:
        # Get all movies in the library
        self.headers = {
            "Authorization": f'MediaBrowser Token="{os.getenv("JELLYFIN_API_KEY")}"',
        }
        self.movies = self.get_movies()
        self.series = self.get_series()

        with open("params.json", "r", encoding="utf-8") as file:
            self.params = json.load(file)

    def get_movies(self) -> list[dict]:
        """
        Get all movies in the Jellyfin library
        """

        url = JELLYFIN_URL + "Items"
        params = {
            "Recursive": "true",
            "IncludeItemTypes": "Movie",
            "fields": "MediaSources,People",
        }
        response = requests.get(url, params=params, headers=self.headers, timeout=20)
        if response.status_code != 200:
            print(response.status_code)
            print(response.content)
            raise RadarrException("Unable to make request to " + url)

        res = response.json()

        for movie in res.get("Items", []):
            movie["Directors"] = [
                person
                for person in movie.get("People", [])
                if person.get("Type") == "Director"
            ]

        return res

    def get_series(self) -> list[dict]:
        """
        Get all series in the Jellyfin library
        """

        url = JELLYFIN_URL + "Items"
        params = {
            "Recursive": "true",
            "IncludeItemTypes": "Series",
            "fields": "MediaSources",
        }
        response = requests.get(url, params=params, headers=self.headers, timeout=20)
        if response.status_code != 200:
            print(response.status_code)
            print(response.content)
            raise RadarrException("Unable to make request to " + url)

        res = response.json()

        return res

    def get_serie_details(self, serie_id: str) -> dict:
        """
        Get serie details from its ID
        """

        url = JELLYFIN_URL + "Shows/" + serie_id + "/Episodes"
        params = {
            "Recursive": "true",
            "IncludeItemTypes": "Series",
            "fields": "MediaSources",
        }
        response = requests.get(url, params=params, headers=self.headers, timeout=20)
        if response.status_code != 200:
            print(response.status_code)
            print(response.content)
            raise RadarrException("Unable to make request to " + url)

        return response.json()

    def get_movie_id(self, movie_name: str, movie_year: str) -> str:
        """
        Get the Jellyfin ID of a movie from its name and year
        """

        for movie in self.movies["Items"]:
            if movie["Name"] == movie_name and movie["ProductionYear"] == movie_year:
                return movie["Id"]

        raise JellyfinException(
            "Unable to find movie "
            + movie_name
            + " ("
            + str(movie_year)
            + ") in the Jellyfin library"
        )

    def add_to_user_collection(self, movie_ids: list[str], username: str) -> None:
        """
        Add a movie to a collection
        """

        user_collection_id = self.params["collection_ids"][username]
        if user_collection_id is None:
            raise JellyfinException("Unable to find collection ID for " + username)

        url = JELLYFIN_URL + "Collections/" + user_collection_id + "/Items"
        params = {"ids": ",".join(movie_ids)}
        response = requests.post(url, headers=self.headers, params=params, timeout=20)
        if response.status_code != 204:
            print(response.status_code)
            print(response.content)
            raise RadarrException("Unable to make request to " + url)

    def add_to_collection(self, movie_ids: list[str], collection_id: str) -> None:
        """
        Add a movie to a collection
        """

        url = JELLYFIN_URL + "Collections/" + collection_id + "/Items"
        params = {"ids": ",".join(movie_ids)}
        response = requests.post(url, headers=self.headers, params=params, timeout=20)
        if response.status_code != 204:
            print(response.status_code)
            print(response.content)
            raise RadarrException("Unable to make request to " + url)

    def get_collection_movies(self, username: str) -> set[str]:
        """
        Get all movies in a collection
        """

        user_collection_id = self.params["collection_ids"][username]
        if user_collection_id is None:
            raise JellyfinException("Unable to find collection ID for " + username)

        url = JELLYFIN_URL + "Items"
        params = {
            "ParentId": user_collection_id,
            "Recursive": "true",
            "IncludeItemTypes": "Movie",
        }
        response = requests.get(url, params=params, headers=self.headers, timeout=20)
        if response.status_code != 200:
            print(response.status_code)
            print(response.content)
            raise RadarrException("Unable to make request to " + url)

        res = response.json()

        return set(map(lambda x: x["Id"], res["Items"]))

    def remove_from_collection(self, movie_tmdb: set[str], username: str) -> None:
        """
        Remove a movie from a collection
        """
        print("Removing " + str(len(movie_tmdb)) + " movies from collection")

        user_collection_id = self.params["collection_ids"][username]
        if user_collection_id is None:
            raise JellyfinException("Unable to find collection ID for " + username)

        url = JELLYFIN_URL + "Collections/" + user_collection_id + "/Items"
        params = {"ids": ",".join(movie_tmdb)}
        response = requests.delete(url, headers=self.headers, params=params, timeout=20)
        if response.status_code != 204:
            print(response.status_code)
            print(response.content)
            raise RadarrException("Unable to make request to " + url)

    def get_collection_by_name(self, collection_name: str) -> dict:
        """
        Get a collection from its name
        """

        url = JELLYFIN_URL + "Items"
        params = {
            "SearchTerm": collection_name,
            "IncludeItemTypes": "BoxSet",
            "Recursive": "true",
            "Limit": 1,
        }

        response = requests.get(url, headers=self.headers, params=params, timeout=20)

        if response.status_code == 200:
            res = response.json()
            items = res.get("Items", [])

            for item in items:
                if item.get("Name").lower() == collection_name.lower():
                    return item.get("Id", "")

        else:
            print(f"Erreur: {response.status_code}")
            print(response.content)

        return ""

    def create_collection(self, collection_name: str, parent_collection_id: str = ""):
        """
        Create a collection
        """

        url = JELLYFIN_URL + "Collections"
        params = {
            "name": collection_name,
            "parentId": parent_collection_id,
        }

        # Exécuter la requête
        response = requests.post(url, headers=self.headers, params=params, timeout=20)

        # Vérifier le succès de la requête
        if response.status_code == 200:
            return response.json()

        # Gérer les erreurs ou l'absence de résultats
        else:
            print(f"Erreur: {response.status_code}")
            print(response.content)
