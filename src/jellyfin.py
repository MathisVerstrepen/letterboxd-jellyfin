import sys
from pathlib import Path
import requests
from src.logger import setup_logger

root_path = Path(__file__).parent.parent
sys.path.append(str(root_path))

from src.exceptions import JellyfinException


class Jellyfin:
    def __init__(self, url: str, api_key: str) -> None:
        if url.endswith("/"):
            url = url[:-1]
        self.base_url = url
        self.headers = {
            "Authorization": f'MediaBrowser Token="{api_key}"',
        }
        self._movie_cache: dict[tuple[str, int], str] | None = None
        self.logger = setup_logger()

        # Test connection on initialization
        self._test_connection()

    def _test_connection(self) -> None:
        """Test the connection to Jellyfin server."""
        url = self.base_url + "/System/Info"
        try:
            response = requests.get(url, headers=self.headers, timeout=10)
            if response.status_code != 200:
                raise JellyfinException(
                    f"Failed to connect to Jellyfin server: HTTP {response.status_code}"
                )
        except requests.exceptions.RequestException as e:
            raise JellyfinException(f"Unable to connect to Jellyfin server: {e}")

    def _get_movie_lookup_cache(self) -> dict:
        """
        Builds a cache for mapping (Title, Year) to Jellyfin ID.
        This is called once per sync instead of on every lookup.
        """
        if self._movie_cache is None:
            self._movie_cache = {}
            all_movies_response = self.get_movies()
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

    def get_movies(self) -> dict:
        """
        Get all movies in the Jellyfin library
        """

        url = self.base_url + "/Items"
        params = {
            "Recursive": "true",
            "IncludeItemTypes": "Movie",
            "fields": "MediaSources,People",
        }
        response = requests.get(url, params=params, headers=self.headers, timeout=20)
        if response.status_code != 200:
            raise JellyfinException(
                f"Unable to make request to {url}. Status code: {response.status_code}, Response: {response.text}"
            )

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

        url = self.base_url + "/Items"
        params = {
            "Recursive": "true",
            "IncludeItemTypes": "Series",
            "fields": "MediaSources",
        }
        response = requests.get(url, params=params, headers=self.headers, timeout=20)
        if response.status_code != 200:
            raise JellyfinException(
                f"Unable to make request to {url}. Status code: {response.status_code}, Response: {response.text}"
            )

        res = response.json()

        return res

    def get_serie_details(self, serie_id: str) -> dict:
        """
        Get serie details from its ID
        """

        url = self.base_url + "/Shows/" + serie_id + "/Episodes"
        params = {
            "Recursive": "true",
            "IncludeItemTypes": "Series",
            "fields": "MediaSources",
        }
        response = requests.get(url, params=params, headers=self.headers, timeout=20)
        if response.status_code != 200:
            raise JellyfinException(
                f"Unable to make request to {url}. Status code: {response.status_code}, Response: {response.text}"
            )

        return response.json()

    def add_to_user_collection(
        self, movie_ids: list[str], username: str, collection_id: str
    ) -> None:
        """
        Add movies to a collection, batching requests to avoid URL length limits
        """
        if not movie_ids:
            return

        if collection_id is None:
            raise JellyfinException("Unable to find collection ID for " + username)

        # Batch size to avoid URL length limits (HTTP 414 error)
        batch_size = 50
        url = self.base_url + "/Collections/" + collection_id + "/Items"

        total_added = 0
        for i in range(0, len(movie_ids), batch_size):
            batch = movie_ids[i : i + batch_size]
            params = {"ids": ",".join(batch)}
            response = requests.post(
                url, headers=self.headers, params=params, timeout=20
            )
            if response.status_code != 204:
                raise JellyfinException(
                    f"Unable to make request to {url}. Status code: {response.status_code}, Response: {response.text}"
                )

            total_added += len(batch)
            self.logger.debug(
                f"Added batch of {len(batch)} movies to collection {collection_id} for user {username}"
            )

        self.logger.info(
            f"Successfully added {total_added} movies to collection for user {username}"
        )

    def add_to_collection(self, movie_ids: list[str], collection_id: str) -> None:
        """
        Add movies to a collection, batching requests to avoid URL length limits
        """
        if not movie_ids:
            return

        # Batch size to avoid URL length limits (HTTP 414 error)
        batch_size = 50
        url = self.base_url + "/Collections/" + collection_id + "/Items"

        total_added = 0
        for i in range(0, len(movie_ids), batch_size):
            batch = movie_ids[i : i + batch_size]
            params = {"ids": ",".join(batch)}

            response = requests.post(
                url, headers=self.headers, params=params, timeout=20
            )

            if response.status_code != 204:
                self.logger.error(
                    f"Failed to add movies batch to collection. Status: {response.status_code}, Response: {response.text}"
                )
                raise JellyfinException(
                    f"Unable to make request to {url}. Status code: {response.status_code}, Response: {response.text}"
                )

            total_added += len(batch)
            self.logger.debug(
                f"Added batch of {len(batch)} movies to collection {collection_id}"
            )

        self.logger.info(
            f"Successfully added {total_added} movies to collection {collection_id}"
        )

    def get_played_movies_from_collection(
        self, collection_id: str, user_id: str
    ) -> list[str]:
        """Gets a list of movie IDs from a collection that a specific user has played."""
        url = f"{self.base_url}/Users/{user_id}/Items"
        params = {
            "ParentId": collection_id,
            "Recursive": "true",
            "IncludeItemTypes": "Movie",
            "Filters": "IsPlayed",
        }
        response = requests.get(url, params=params, headers=self.headers, timeout=20)

        played_movie_ids = []
        if response.status_code == 200:
            data = response.json()
            for item in data.get("Items", []):
                played_movie_ids.append(item.get("Id"))

        return played_movie_ids

    def get_collection_movies(self, username: str, collection_id: str) -> set[str]:
        """
        Get all movies in a collection
        """

        if collection_id is None:
            raise JellyfinException("Unable to find collection ID for " + username)

        url = self.base_url + "/Items"
        params = {
            "ParentId": collection_id,
            "Recursive": "true",
            "IncludeItemTypes": "Movie",
        }
        response = requests.get(url, params=params, headers=self.headers, timeout=20)
        if response.status_code != 200:
            raise JellyfinException(
                f"Unable to make request to {url}. Status code: {response.status_code}, Response: {response.text}"
            )

        res = response.json()

        return set(map(lambda x: x["Id"], res["Items"]))

    def remove_from_collection(self, movie_ids: list[str], collection_id: str) -> None:
        """Remove movies from a collection by their Jellyfin IDs."""
        if not movie_ids:
            return

        if collection_id is None:
            raise JellyfinException("Cannot remove from a null collection ID")

        url = self.base_url + "/Collections/" + collection_id + "/Items"
        params = {"ids": ",".join(movie_ids)}
        response = requests.delete(url, headers=self.headers, params=params, timeout=20)
        if response.status_code != 204:
            raise JellyfinException(
                f"Unable to make request to {url}. Status code: {response.status_code}, Response: {response.text}"
            )

    def get_user_id(self, username: str) -> str | None:
        """
        Get a user's ID from their username
        """

        url = self.base_url + "/Users"
        response = requests.get(url, headers=self.headers, timeout=20)
        if response.status_code != 200:
            raise JellyfinException(
                f"Unable to make request to {url}. Status code: {response.status_code}, Response: {response.text}"
            )

        res = response.json()

        for user in res:
            if user.get("Name") == username:
                return user.get("Id")

        return None
