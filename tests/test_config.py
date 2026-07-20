from pathlib import Path

import pytest

import src.config as config
from src.exceptions import ConfigurationError

BASE = """
jellyfin: {url: http://jellyfin.invalid, api_key: jellyfin-key}
radarr: {url: http://radarr.invalid, api_key: radarr-key}
users: []
"""


def load(tmp_path: Path, monkeypatch, suffix: str = ""):
    path = tmp_path / "config.yaml"
    path.write_text(BASE + suffix, encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    return config.load_config()


def test_sonarr_is_optional(tmp_path, monkeypatch):
    assert "sonarr" not in load(tmp_path, monkeypatch)


def test_complete_sonarr_configuration_loads(tmp_path, monkeypatch):
    loaded = load(
        tmp_path,
        monkeypatch,
        """
sonarr:
  url: http://sonarr.invalid
  api_key: sonarr-key
  root_folder_path: /series
  quality_profile_id: 3
  timeout: 20
""",
    )
    assert loaded["sonarr"]["quality_profile_id"] == 3


def test_sonarr_only_configuration_loads(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
jellyfin: {url: http://jellyfin.invalid, api_key: jellyfin-key}
sonarr:
  url: http://sonarr.invalid
  api_key: sonarr-key
  root_folder_path: /series
  quality_profile_id: 3
users: []
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    assert "radarr" not in config.load_config()


def test_jellyfin_only_configuration_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(
        "jellyfin: {url: http://jellyfin.invalid, api_key: jellyfin-key}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    with pytest.raises(ConfigurationError) as error:
        config.load_config()
    assert str(error.value) == "At least one media provider configuration is required"


def test_malformed_present_radarr_fails_even_with_valid_sonarr(tmp_path, monkeypatch):
    with pytest.raises(ConfigurationError) as error:
        load(
            tmp_path,
            monkeypatch,
            """
sonarr:
  url: http://sonarr.invalid
  api_key: sonarr-key
  root_folder_path: /series
  quality_profile_id: 3
radarr: {url: private-provider-value}
""",
        )
    assert str(error.value) == "Radarr configuration is incomplete or invalid"
    assert "private-provider-value" not in str(error.value)


@pytest.mark.parametrize(
    "animated_tv",
    [
        "  animated_tv: {enabled: false}\n",
        "  animated_tv: {enabled: false, root_folder_path: /animation}\n",
        "  animated_tv: {enabled: true, root_folder_path: /animation}\n",
    ],
)
def test_valid_animated_tv_configuration_loads(tmp_path, monkeypatch, animated_tv):
    loaded = load(
        tmp_path,
        monkeypatch,
        """
sonarr:
  url: http://sonarr.invalid
  api_key: sonarr-key
  root_folder_path: /series
  quality_profile_id: 3
"""
        + animated_tv,
    )
    assert loaded["sonarr"]["animated_tv"]["enabled"] in {True, False}


@pytest.mark.parametrize(
    "animated_tv",
    [
        "  animated_tv: []\n",
        "  animated_tv: {}\n",
        "  animated_tv: {enabled: 1, root_folder_path: /animation}\n",
        "  animated_tv: {enabled: true}\n",
        "  animated_tv: {enabled: true, root_folder_path: ''}\n",
        "  animated_tv: {enabled: false, root_folder_path: 1}\n",
        "  animated_tv: {enabled: false, root_folder_path: '   '}\n",
    ],
)
def test_invalid_animated_tv_configuration_is_generic(
    tmp_path, monkeypatch, animated_tv
):
    with pytest.raises(ConfigurationError) as error:
        load(
            tmp_path,
            monkeypatch,
            """
sonarr:
  url: http://sonarr.invalid
  api_key: sonarr-key
  root_folder_path: /series
  quality_profile_id: 3
"""
            + animated_tv,
        )
    assert str(error.value) == "Sonarr configuration is incomplete or invalid"
    assert "/animation" not in str(error.value)


@pytest.mark.parametrize(
    "sonarr",
    [
        "sonarr: {}\n",
        "sonarr: {url: x, api_key: y, root_folder_path: /series}\n",
        "sonarr: {url: x, api_key: y, root_folder_path: /series, quality_profile_id: true}\n",
        "sonarr: {url: x, api_key: y, root_folder_path: /series, "
        "quality_profile_id: 1, timeout: 0}\n",
    ],
)
def test_invalid_sonarr_configuration_is_rejected(tmp_path, monkeypatch, sonarr):
    with pytest.raises(ConfigurationError, match="Sonarr configuration"):
        load(tmp_path, monkeypatch, sonarr)
