import copy
import time
from typing import Any

import requests
from requests.exceptions import JSONDecodeError

from src.exceptions import SonarrException
from src.logger import get_logger
from src.results import MutationResult, SonarrLookupResult


class SonarrClient:
    """Small Sonarr v3 client with cycle-local inventory and lookup caches."""

    def __init__(self, url: str, api_key: str, timeout: int = 60) -> None:
        self.base_url = url if url.endswith("/api/v3") else url.rstrip("/") + "/api/v3"
        self.headers = {"X-Api-Key": api_key}
        self.timeout = timeout
        self.logger = get_logger("sonarr")
        self._inventory_loaded = False
        self._inventory_available = False
        self._inventory: dict[str, dict[str, Any]] = {}
        self._lookup_cache: dict[str, SonarrLookupResult] = {}
        self._test_connection()
        self.logger.info(
            "Sonarr client initialized", extra={"event": "sonarr_client_initialized"}
        )

    def _test_connection(self) -> None:
        try:
            response = requests.get(
                f"{self.base_url}/system/status",
                headers=self.headers,
                timeout=self.timeout,
            )
            if response.status_code != 200:
                raise SonarrException("Sonarr connection test failed")
        except requests.exceptions.RequestException as exc:
            raise SonarrException("Sonarr connection test failed") from exc

    @staticmethod
    def _valid_resource(resource: Any, tmdb_id: str) -> dict[str, Any] | None:
        if not isinstance(resource, dict) or str(resource.get("tmdbId")) != str(tmdb_id):
            return None
        required_strings = ("title", "titleSlug", "seriesType")
        if any(
            not isinstance(resource.get(field), str) or not resource[field].strip()
            for field in required_strings
        ):
            return None
        year = resource.get("year")
        tvdb_id = resource.get("tvdbId")
        seasons = resource.get("seasons")
        if (
            isinstance(year, bool)
            or not isinstance(year, int)
            or year <= 0
            or isinstance(tvdb_id, bool)
            or not isinstance(tvdb_id, int)
            or tvdb_id <= 0
            or not isinstance(seasons, list)
            or any(
                not isinstance(season, dict)
                or isinstance(season.get("seasonNumber"), bool)
                or not isinstance(season.get("seasonNumber"), int)
                for season in seasons
            )
        ):
            return None
        return resource

    def _load_inventory(self, *, force: bool = False) -> None:
        if self._inventory_loaded and not force:
            return
        self._inventory_loaded = True
        self._inventory_available = False
        try:
            response = requests.get(
                f"{self.base_url}/series",
                headers=self.headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            if "application/json" not in response.headers.get("Content-Type", ""):
                raise ValueError("non-JSON inventory")
            resources = response.json()
            if not isinstance(resources, list):
                raise ValueError("invalid inventory")
            inventory: dict[str, dict[str, Any]] = {}
            for resource in resources:
                if not isinstance(resource, dict) or resource.get("tmdbId") is None:
                    continue
                inventory[str(resource["tmdbId"])] = resource
            self._inventory = inventory
            self._inventory_available = True
        except (requests.exceptions.RequestException, JSONDecodeError, ValueError):
            self.logger.warning(
                "Sonarr inventory could not be loaded",
                extra={"event": "sonarr_inventory_failed", "stage": "sonarr"},
            )

    def check_sonarr_state(self, tmdb_id: str) -> SonarrLookupResult:
        cache_key = str(tmdb_id)
        self._load_inventory()
        installed = self._inventory.get(cache_key)
        if installed is not None:
            return SonarrLookupResult(state=installed, installed=True)
        cached = self._lookup_cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            response = requests.get(
                f"{self.base_url}/series/lookup",
                params={"term": f"tmdb:{cache_key}"},
                headers=self.headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            if "application/json" not in response.headers.get("Content-Type", ""):
                raise ValueError("non-JSON lookup")
            resources = response.json()
            if not isinstance(resources, list):
                raise ValueError("invalid lookup")
            matches = [
                resource
                for candidate in resources
                if (resource := self._valid_resource(candidate, cache_key)) is not None
            ]
            result = (
                SonarrLookupResult(state=matches[0])
                if len(matches) == 1
                else SonarrLookupResult(state=None, failed_items=1)
            )
        except (requests.exceptions.RequestException, JSONDecodeError, ValueError):
            result = SonarrLookupResult(state=None, failed_items=1)
        if result.failed_items:
            self.logger.error(
                "Sonarr lookup failed",
                extra={"event": "sonarr_lookup_failed", "stage": "sonarr"},
            )
        self._lookup_cache[cache_key] = result
        return result

    def add_to_sonarr_download_queue(
        self, resource: dict[str, Any], root_path: str, quality_profile_id: int
    ) -> MutationResult:
        body = copy.deepcopy(resource)
        body.pop("id", None)
        body.update(
            {
                "rootFolderPath": root_path,
                "qualityProfileId": quality_profile_id,
                "monitored": True,
                "monitorNewItems": "all",
                "seasonFolder": True,
                "addOptions": {
                    "monitor": "all",
                    "searchForMissingEpisodes": True,
                },
            }
        )
        body["seasons"] = [
            {**season, "monitored": True} for season in body.get("seasons", [])
        ]
        tmdb_id = str(body["tmdbId"])
        attempted = 0
        response = None
        for attempt in range(3):
            try:
                attempted += 1
                response = requests.post(
                    f"{self.base_url}/series",
                    json=body,
                    headers=self.headers,
                    timeout=self.timeout,
                )
                break
            except requests.exceptions.RequestException:
                if attempt < 2:
                    self.logger.warning(
                        "Sonarr add request failed; retrying",
                        extra={"event": "sonarr_add_retry", "attempt": attempt + 1},
                    )
                    time.sleep(2**attempt)
        if response is not None and 200 <= response.status_code < 300:
            self._inventory[tmdb_id] = body
            return MutationResult(attempted=attempted, succeeded=1)

        self._load_inventory(force=True)
        if tmdb_id in self._inventory:
            return MutationResult(attempted=attempted, succeeded=1)
        self.logger.error(
            "Sonarr add failed",
            extra={"event": "sonarr_add_failed", "stage": "sonarr"},
        )
        return MutationResult(attempted=attempted, failed_items=1)

    lookup_series = check_sonarr_state
