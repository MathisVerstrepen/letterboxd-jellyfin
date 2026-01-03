from typing import TypedDict
import time
import requests

from requests.exceptions import JSONDecodeError
from src.exceptions import RadarrException
from src.logger import setup_logger


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


class RadarrClient:
    def __init__(self, url: str, api_key: str, timeout: int = 60):
        if not url.endswith("/api/v3"):
            url = url.rstrip("/") + "/api/v3"

        self.base_url = url
        self.headers = {"X-Api-Key": api_key}
        self.timeout = timeout
        self.logger = setup_logger()

        self.logger.info(f"RadarrClient initialized with base URL: {self.base_url}")

        # Test connection on initialization
        self._test_connection()

    def _test_connection(self) -> None:
        """Test the connection to Radarr server."""
        url = self.base_url + "/system/status"
        try:
            response = requests.get(url, headers=self.headers, timeout=self.timeout)
            if response.status_code != 200:
                raise RadarrException(
                    f"Failed to connect to Radarr server: HTTP {response.status_code}"
                )
        except requests.exceptions.RequestException as e:
            raise RadarrException(f"Unable to connect to Radarr server: {e}")

    def check_radarr_state(self, tmdb_id: str) -> RadarrState | None:
        """
        Check if a file exists for a given TMDB ID in Radarr.
        """
        url = f"{self.base_url}/movie/lookup"
        params = {"term": f"tmdb:{tmdb_id}"}

        try:
            response = requests.get(
                url, params=params, headers=self.headers, timeout=self.timeout
            )
            response.raise_for_status()

            content_type = response.headers.get("Content-Type", "")
            if "application/json" not in content_type:
                self.logger.error(
                    f"Radarr returned non-JSON response for TMDB ID {tmdb_id}. "
                    f"Content-Type: '{content_type}'. This often indicates a proxy/auth issue."
                )
                self.logger.debug(f"Response text: {response.text[:200]}...")
                return None

            res = response.json()
        except requests.exceptions.RequestException as e:
            self.logger.error(
                f"Failed to make request to Radarr for TMDB ID {tmdb_id}: {e}"
            )
            return None
        except JSONDecodeError:
            self.logger.error(
                f"Failed to decode JSON from Radarr for TMDB ID {tmdb_id}."
            )
            return None

        if not res:
            self.logger.info(f"No results found in Radarr for TMDB ID: {tmdb_id}")
            return None

        movie_data = res[0]
        return {
            "hasFile": movie_data.get("movieFile") is not None,
            "monitored": movie_data.get("monitored", False),
            "name": movie_data.get("title"),
            "tmdbId": movie_data.get("tmdbId"),
            "productionYear": movie_data.get("year"),
            "is_animation": "Animation" in movie_data.get("genres", []),
        }

    def get_movies_state(self, tmdb_ids: set[str]) -> list[RadarrState]:
        """Processes a list of TMDB IDs and returns their Radarr states."""
        states = []
        for tmdb_id in tmdb_ids:
            state = self.check_radarr_state(tmdb_id)
            if state:
                states.append(state)
        return states

    def add_to_radarr_download_queue(
        self, movies: list[dict], root_path: str, quality_profile_id: int
    ):
        bodies = [
            {
                "tmdbId": movie["tmdbId"],
                "title": movie["name"],
                "year": movie["productionYear"],
                "qualityProfileId": quality_profile_id,
                "monitored": True,
                "rootFolderPath": root_path,
                "addOptions": {"searchForMovie": True},
            }
            for movie in movies
        ]

        url = self.base_url + "/movie"

        for body in bodies:
            for attempt in range(3):
                try:
                    response = requests.post(
                        url, json=body, headers=self.headers, timeout=self.timeout
                    )
                    if response.status_code != 201:
                        if (
                            response.status_code == 400
                            and "has already been added" in response.text
                        ):
                            self.logger.info(
                                f"Movie {body.get('title')} already exists in Radarr."
                            )
                            break
                        self.logger.error(
                            f"Failed to add movie {body.get('title')} to Radarr. Status: {response.status_code}, Response: {response.text}"
                        )
                        break
                    else:
                        self.logger.info(
                            f"Added movie {body.get('title')} to Radarr download queue."
                        )
                        break
                except requests.exceptions.RequestException as e:
                    if attempt < 2:
                        wait_time = 2**attempt
                        self.logger.warning(
                            f"Request to Radarr timed out/failed for '{body.get('title')}'. Retrying in {wait_time}s... Error: {e}"
                        )
                        time.sleep(wait_time)
                    else:
                        self.logger.error(
                            f"Failed to add movie {body.get('title')} to Radarr after 3 attempts: {e}"
                        )
