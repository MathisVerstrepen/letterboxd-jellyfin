from typing import Any

import yaml

from src.exceptions import ConfigurationError

CONFIG_PATH = "config.yaml"


def load_config() -> dict[str, Any]:
    """Load and validate configuration without terminating the process."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            loaded = yaml.safe_load(f)
    except (FileNotFoundError, OSError) as exc:
        raise ConfigurationError("Configuration file could not be read") from exc
    except yaml.YAMLError as exc:
        raise ConfigurationError("Configuration file could not be parsed") from exc

    if not isinstance(loaded, dict):
        raise ConfigurationError("Configuration root must be a mapping")

    system = loaded.get("system", {})
    if not isinstance(system, dict):
        raise ConfigurationError("System configuration must be a mapping")
    sync_interval = system.get("sync_interval", 10)
    if isinstance(sync_interval, bool) or not isinstance(sync_interval, int):
        raise ConfigurationError("Sync interval must be a positive integer")
    if sync_interval <= 0:
        raise ConfigurationError("Sync interval must be a positive integer")

    observability = loaded.get("observability", {})
    if not isinstance(observability, dict):
        raise ConfigurationError("Observability configuration must be a mapping")
    host = observability.get("host", "127.0.0.1")
    port = observability.get("port", 8000)
    if not isinstance(host, str) or not host.strip():
        raise ConfigurationError("Observability host must be a non-empty string")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigurationError("Observability port must be an integer from 1 to 65535")

    jellyfin = loaded.get("jellyfin")
    if not isinstance(jellyfin, dict) or any(
        not jellyfin.get(key) for key in ("url", "api_key")
    ):
        raise ConfigurationError("Required jellyfin configuration is missing")

    radarr_present = "radarr" in loaded
    sonarr_present = "sonarr" in loaded
    if not radarr_present and not sonarr_present:
        raise ConfigurationError("At least one media provider configuration is required")
    if radarr_present:
        radarr = loaded["radarr"]
        if not isinstance(radarr, dict) or any(
            not radarr.get(key) for key in ("url", "api_key")
        ):
            raise ConfigurationError("Radarr configuration is incomplete or invalid")

    if sonarr_present:
        sonarr = loaded["sonarr"]
        required = ("url", "api_key", "root_folder_path", "quality_profile_id")
        if not isinstance(sonarr, dict) or any(
            not isinstance(sonarr.get(key), str) or not sonarr[key].strip()
            for key in required[:3]
        ):
            raise ConfigurationError("Sonarr configuration is incomplete or invalid")
        profile_id = sonarr.get("quality_profile_id")
        timeout = sonarr.get("timeout", 60)
        if (
            isinstance(profile_id, bool)
            or not isinstance(profile_id, int)
            or profile_id <= 0
            or isinstance(timeout, bool)
            or not isinstance(timeout, int)
            or timeout <= 0
        ):
            raise ConfigurationError("Sonarr configuration is incomplete or invalid")
        if "animated_tv" in sonarr:
            animated_tv = sonarr["animated_tv"]
            if not isinstance(animated_tv, dict) or not isinstance(
                animated_tv.get("enabled"), bool
            ):
                raise ConfigurationError("Sonarr configuration is incomplete or invalid")
            animated_root = animated_tv.get("root_folder_path")
            if animated_tv["enabled"] and (
                not isinstance(animated_root, str) or not animated_root.strip()
            ):
                raise ConfigurationError("Sonarr configuration is incomplete or invalid")
            if animated_root is not None and (
                not isinstance(animated_root, str) or not animated_root.strip()
            ):
                raise ConfigurationError("Sonarr configuration is incomplete or invalid")

    users = loaded.get("users", [])
    if not isinstance(users, list):
        raise ConfigurationError("Users configuration must be a list")

    return loaded
