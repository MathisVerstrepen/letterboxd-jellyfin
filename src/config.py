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

    for section, required_keys in (
        ("jellyfin", ("url", "api_key")),
        ("radarr", ("url", "api_key")),
    ):
        values = loaded.get(section)
        if not isinstance(values, dict) or any(not values.get(key) for key in required_keys):
            raise ConfigurationError(f"Required {section} configuration is missing")

    users = loaded.get("users", [])
    if not isinstance(users, list):
        raise ConfigurationError("Users configuration must be a list")

    return loaded
