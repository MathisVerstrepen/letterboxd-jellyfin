from typing import TypedDict
import time
import requests

from requests.exceptions import JSONDecodeError
from src.exceptions import RadarrException
from src.logger import get_logger
from src.results import MutationResult, RadarrLookupResult


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
        self.logger = get_logger("radarr")

        self.logger.info(
            "Radarr client initialized",
            extra={"event": "radarr_client_initialized"},
        )

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

    def check_radarr_state(self, tmdb_id: str) -> RadarrLookupResult:
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
                    "Radarr lookup returned unusable content",
                    extra={"event": "radarr_lookup_failed", "stage": "radarr"},
                )
                return RadarrLookupResult(state=None, failed_items=1)

            res = response.json()
        except requests.exceptions.RequestException:
            self.logger.error(
                "Radarr lookup request failed",
                extra={"event": "radarr_lookup_failed", "stage": "radarr"},
            )
            return RadarrLookupResult(state=None, failed_items=1)
        except JSONDecodeError:
            self.logger.error(
                "Radarr lookup returned invalid JSON",
                extra={"event": "radarr_lookup_failed", "stage": "radarr"},
            )
            return RadarrLookupResult(state=None, failed_items=1)

        if not isinstance(res, list) or not res or not isinstance(res[0], dict):
            self.logger.warning(
                "Radarr lookup returned no result",
                extra={"event": "radarr_lookup_failed", "stage": "radarr"},
            )
            return RadarrLookupResult(state=None, failed_items=1)

        movie_data = res[0]
        genres = movie_data.get("genres", [])
        state = {
            "hasFile": movie_data.get("movieFile") is not None,
            "monitored": movie_data.get("monitored", False),
            "name": movie_data.get("title"),
            "tmdbId": movie_data.get("tmdbId"),
            "productionYear": movie_data.get("year"),
            "is_animation": isinstance(genres, list) and "Animation" in genres,
        }
        if not state["name"] or state["tmdbId"] is None or state["productionYear"] is None:
            self.logger.error(
                "Radarr lookup returned unusable movie data",
                extra={"event": "radarr_lookup_failed", "stage": "radarr"},
            )
            return RadarrLookupResult(state=None, failed_items=1)
        return RadarrLookupResult(state=state)

    def get_movies_state(self, tmdb_ids: set[str]) -> list[RadarrState]:
        """Processes a list of TMDB IDs and returns their Radarr states."""
        states = []
        for tmdb_id in tmdb_ids:
            result = self.check_radarr_state(tmdb_id)
            if result.state:
                states.append(result.state)
        return states

    def add_to_radarr_download_queue(
        self, movies: list[dict], root_path: str, quality_profile_id: int
    ) -> MutationResult:
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

        succeeded = 0
        failed_items = 0
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
                            succeeded += 1
                            break
                        self.logger.error(
                            "Radarr rejected queue request",
                            extra={"event": "radarr_queue_result", "outcome": "failed", "attempt": attempt + 1, "status_code": response.status_code},
                        )
                        failed_items += 1
                        break
                    else:
                        succeeded += 1
                        break
                except requests.exceptions.RequestException:
                    if attempt < 2:
                        wait_time = 2**attempt
                        self.logger.warning(
                            "Radarr queue request failed; retrying",
                            extra={"event": "radarr_queue_retry", "attempt": attempt + 1},
                        )
                        time.sleep(wait_time)
                    else:
                        self.logger.error(
                            "Radarr queue request exhausted retries",
                            extra={"event": "radarr_queue_result", "outcome": "failed", "attempt": attempt + 1},
                        )
                        failed_items += 1
        return MutationResult(
            attempted=len(bodies), succeeded=succeeded, failed_items=failed_items
        )
