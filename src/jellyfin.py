import os
import json
import sys
from pathlib import Path
import requests

root_path = Path(__file__).parent.parent
sys.path.append(str(root_path))

from src.exceptions import RadarrException, JellyfinException

class Jellyfin:
    def __init__(self, url: str, api_key: str) -> None:
        if url.endswith("/"):
            url = url[:-1]
        self.base_url = url
        self.headers = {
            "Authorization": f'MediaBrowser Token="{api_key}"',
        }
        self._movie_cache: dict[tuple[str, int], str] | None = None  # Cache for movie lookups

    def _get_movie_lookup_cache(self) -> dict:
        """
        Builds a cache for mapping (Title, Year) to Jellyfin ID.
        This is called once per sync instead of on every lookup.
        """
        if self._movie_cache is None:
            self._movie_cache = {}
            all_movies_response = self.get_movies()  # Fetch all movies
            for movie in all_movies_response.get("Items", []):
                key = (movie.get("Name"), movie.get("ProductionYear"))
                self._movie_cache[key] = movie.get("Id")
        return self._movie_cache

    def get_movie_id(self, movie_name: str, movie_year: int) -> str | None:
        """
        Get the Jellyfin ID of a movie from its name and year using the cache.
        """
        cache = self._get_movie_lookup_cache()
        return cache.get((movie_name, movie_year))

    def get_or_create_collection(self, collection_name: str) -> str | None:
        """
        Gets the ID of a collection by name. If it doesn't exist, creates it.
        """
        collection_id = self.get_collection_by_name(collection_name)
        if collection_id:
            return collection_id

        # If not found, create it
        new_collection = self.create_collection(collection_name)
        return new_collection.get("Id") if new_collection else None

    def get_movies(self) -> dict:
        """
        Get all movies in the Jellyfin library
        """

        url = self.base_url + "Items"
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

        url = self.base_url + "Items"
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

        url = self.base_url + "Shows/" + serie_id + "/Episodes"
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

    def add_to_user_collection(self, movie_ids: list[str], username: str, collection_id: str) -> None:
        """
        Add a movie to a collection
        """

        if collection_id is None:
            raise JellyfinException("Unable to find collection ID for " + username)

        url = self.base_url + "Collections/" + collection_id + "/Items"
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

        url = self.base_url + "Collections/" + collection_id + "/Items"
        params = {"ids": ",".join(movie_ids)}
        response = requests.post(url, headers=self.headers, params=params, timeout=20)
        if response.status_code != 204:
            print(response.status_code)
            print(response.content)
            raise RadarrException("Unable to make request to " + url)

    def get_collection_movies(self, username: str, collection_id: str) -> set[str]:
        """
        Get all movies in a collection
        """

        if collection_id is None:
            raise JellyfinException("Unable to find collection ID for " + username)

        url = self.base_url + "Items"
        params = {
            "ParentId": collection_id,
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

    def remove_from_collection(self, movie_tmdb: set[str], username: str, collection_id: str) -> None:
        """
        Remove a movie from a collection
        """
        print("Removing " + str(len(movie_tmdb)) + " movies from collection")

        if collection_id is None:
            raise JellyfinException("Unable to find collection ID for " + username)

        url = self.base_url + "Collections/" + collection_id + "/Items"
        params = {"ids": ",".join(movie_tmdb)}
        response = requests.delete(url, headers=self.headers, params=params, timeout=20)
        if response.status_code != 204:
            print(response.status_code)
            print(response.content)
            raise RadarrException("Unable to make request to " + url)

    def get_collection_by_name(self, collection_name: str) -> str:
        """
        Get a collection from its name
        """

        url = self.base_url + "/Items"
        params: dict[str, str | int] = {
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

        url = self.base_url + "/Collections"
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
