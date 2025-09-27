import yaml
from typing import Any

CONFIG_PATH = "config.yaml"


def load_config() -> dict[str, Any]:
    """Loads the YAML configuration file."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        print(f"ERROR: Configuration file not found at '{CONFIG_PATH}'")
        print("Please copy 'config.example.yaml' to 'config.yaml' and fill it out.")
        exit(1)
    except yaml.YAMLError as e:
        print(f"ERROR: Could not parse configuration file: {e}")
        exit(1)


config = load_config()
