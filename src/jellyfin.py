# pylint: disable=missing-module-docstring, wrong-import-position
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
        response = requests.get(url, params=params, headers=self.headers, timeout=5)
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
        response = requests.get(url, params=params, headers=self.headers, timeout=5)
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
        response = requests.get(url, params=params, headers=self.headers, timeout=5)
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
        response = requests.post(url, headers=self.headers, params=params, timeout=5)
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
        response = requests.post(url, headers=self.headers, params=params, timeout=5)
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
        response = requests.get(url, params=params, headers=self.headers, timeout=5)
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
        response = requests.delete(url, headers=self.headers, params=params, timeout=5)
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

        response = requests.get(url, headers=self.headers, params=params, timeout=5)

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

    def create_collection(
        self, collection_name: str, parent_collection_id: str = ""
    ) -> dict:
        """
        Create a collection
        """

        url = JELLYFIN_URL + "Collections"
        params = {
            "name": collection_name,
            "parentId": parent_collection_id,
        }

        # Exécuter la requête
        response = requests.post(url, headers=self.headers, params=params, timeout=5)

        # Vérifier le succès de la requête
        if response.status_code == 200:
            return response.json()

        # Gérer les erreurs ou l'absence de résultats
        else:
            print(f"Erreur: {response.status_code}")
            print(response.content)

    def get_movies_stats(self) -> dict:
        """
        Get the stats of movies in the Jellyfin library
        """
        n_movies = 0
        n_movies_4k = 0
        n_movies_remux = 0
        movies_size = 0

        for movie in self.movies["Items"]:
            if "/movies/" in movie["MediaSources"][0]["Path"]:
                n_movies += 1
                for media_stream in movie["MediaSources"][0]["MediaStreams"]:
                    if "4K" in media_stream.get("DisplayTitle", ""):
                        n_movies_4k += 1
                movies_size += movie["MediaSources"][0]["Size"]
            elif "/remux/" in movie["MediaSources"][0]["Path"]:
                n_movies_remux += 1
                n_movies += 1
                n_movies_4k += 1
        return {
            "nb_movies": n_movies,
            "nb_movies_4k": n_movies_4k,
            "size_movies": movies_size,
            "nb_movies_remux": n_movies_remux,
        }

    def get_animes_movies_stats(self):
        """
        Get the stats of anime movies in the Jellyfin library
        """
        n_movies = 0
        n_movies_4k = 0
        movies_size = 0

        for movie in self.movies["Items"]:
            if "/animemovies/" in movie["MediaSources"][0]["Path"]:
                n_movies += 1
                for media_stream in movie["MediaSources"][0]["MediaStreams"]:
                    if "4K" in media_stream.get("DisplayTitle", ""):
                        n_movies_4k += 1
                movies_size += movie["MediaSources"][0]["Size"]

        return {
            "nb_movies_anim": n_movies,
            "nb_movies_anim_4k": n_movies_4k,
            "size_movies_anim": movies_size,
        }

    def get_tv_show_stats(self):
        """
        Get the stats of the TV shows in the Jellyfin library
        """
        n_series = 0
        n_tv_episode = 0
        n_series_4k = 0
        series_size = 0

        for serie in self.series["Items"]:
            serie_id = serie["Id"]
            episodes = self.get_serie_details(serie_id)

            n_series += 1
            is_4k = False
            for episode in episodes["Items"]:
                if "/tvshows/" in episode["MediaSources"][0]["Path"]:
                    n_tv_episode += 1
                    for media_stream in episode["MediaSources"][0]["MediaStreams"]:
                        if "4K" in media_stream.get("DisplayTitle", ""):
                            is_4k = True
                    series_size += episode["MediaSources"][0]["Size"]

            if is_4k:
                n_series_4k += 1

        return {
            "nb_tv": n_series,
            "nb_tv_episode": n_tv_episode,
            "nb_tv_4k": n_series_4k,
            "size_tv": series_size,
        }

    def get_animes_show_stats(self):
        """
        Get the stats of anime shows in the Jellyfin library
        """
        n_series = 0
        n_tv_episode = 0
        n_series_4k = 0
        series_size = 0

        for serie in self.series["Items"]:
            serie_id = serie["Id"]
            episodes = self.get_serie_details(serie_id)

            n_series += 1
            is_4k = False
            for episode in episodes["Items"]:
                if "/animeshows/" in episode["MediaSources"][0]["Path"]:
                    n_tv_episode += 1
                    for media_stream in episode["MediaSources"][0]["MediaStreams"]:
                        if "4K" in media_stream.get("DisplayTitle", ""):
                            is_4k = True
                    series_size += episode["MediaSources"][0]["Size"]

            if is_4k:
                n_series_4k += 1

        return {
            "nb_series_anim": n_series,
            "nb_series_anim_episode": n_tv_episode,
            "nb_series_anim_4k": n_series_4k,
            "size_series_anim": series_size,
        }

    def get_directors_stats(self):
        """
        Return the number of movies and the list of id of movies for each director
        sorted by number of movies
        """
        directors = {}
        for movie in self.movies["Items"]:
            for director in movie["Directors"]:
                if director["Name"] not in directors:
                    directors[director["Name"]] = {
                        "nb_movies": 0,
                        "movies": [],
                    }
                directors[director["Name"]]["nb_movies"] += 1
                directors[director["Name"]]["movies"].append(movie["Id"])

        return sorted(directors.items(), key=lambda x: x[1]["nb_movies"], reverse=True)


if __name__ == "__main__":
    jellyfin = Jellyfin()
    directors = jellyfin.get_directors_stats()

    for director in directors:
        if director[0] in jellyfin.params["directors"]:
            collection = jellyfin.get_collection_by_name(director[0])

            if collection:
                collection_id = collection
            else:
                collection = jellyfin.create_collection(
                    director[0], jellyfin.params["collection_ids"]["directors"]
                )
                print(f"Collection {director[0]} created")
                collection_id = collection.get("Id")

            jellyfin.add_to_collection(director[1]["movies"], collection_id)
