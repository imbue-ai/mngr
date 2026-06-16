"""Unit tests for the create-form region resolution helpers in ``app``."""

from pathlib import Path

from imbue.minds.desktop_client.app import _build_region_form_context
from imbue.minds.desktop_client.app import _persist_region_for_launch_mode
from imbue.minds.desktop_client.app import _region_provider_key_for_launch_mode
from imbue.minds.desktop_client.app import _resolve_effective_region
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.region_preference import GeoLocationCache
from imbue.minds.primitives import DEFAULT_AWS_REGION
from imbue.minds.primitives import LaunchMode


def _config(tmp_path: Path) -> MindsConfig:
    return MindsConfig(data_dir=tmp_path)


def test_region_provider_key_maps_only_region_bearing_modes() -> None:
    assert _region_provider_key_for_launch_mode(LaunchMode.IMBUE_CLOUD) == "imbue_cloud"
    assert _region_provider_key_for_launch_mode(LaunchMode.VULTR) == "vultr"
    assert _region_provider_key_for_launch_mode(LaunchMode.AWS) == "aws"
    assert _region_provider_key_for_launch_mode(LaunchMode.DOCKER) is None
    assert _region_provider_key_for_launch_mode(LaunchMode.LIMA) is None


def test_resolve_effective_region_uses_submitted_known_region(tmp_path: Path) -> None:
    region = _resolve_effective_region(LaunchMode.IMBUE_CLOUD, "US-WEST-OR", _config(tmp_path), GeoLocationCache())
    assert region == "US-WEST-OR"


def test_resolve_effective_region_ignores_unknown_submitted_and_falls_back_to_default(tmp_path: Path) -> None:
    # No stored value, no geo -> hardcoded default for the provider.
    region = _resolve_effective_region(LaunchMode.VULTR, "not-a-region", _config(tmp_path), GeoLocationCache())
    assert region == "ewr"


def test_resolve_effective_region_prefers_stored_value_when_no_submission(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.set_region("imbue_cloud", "US-WEST-OR")
    region = _resolve_effective_region(LaunchMode.IMBUE_CLOUD, "", config, GeoLocationCache())
    assert region == "US-WEST-OR"


def test_resolve_effective_region_is_empty_for_region_less_provider(tmp_path: Path) -> None:
    assert _resolve_effective_region(LaunchMode.DOCKER, "US-WEST-OR", _config(tmp_path), GeoLocationCache()) == ""


def test_build_region_form_context_covers_all_region_bearing_providers(tmp_path: Path) -> None:
    options, selected = _build_region_form_context(_config(tmp_path), GeoLocationCache())
    assert options[LaunchMode.IMBUE_CLOUD.value] == ["US-EAST-VA", "US-WEST-OR"]
    assert "ewr" in options[LaunchMode.VULTR.value]
    assert DEFAULT_AWS_REGION in options[LaunchMode.AWS.value]
    # With no stored value and no geo, defaults are the hardcoded per-provider values.
    assert selected[LaunchMode.IMBUE_CLOUD.value] == "US-EAST-VA"
    assert selected[LaunchMode.VULTR.value] == "ewr"
    assert selected[LaunchMode.AWS.value] == DEFAULT_AWS_REGION


def test_persist_region_writes_back_for_region_bearing_provider(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _persist_region_for_launch_mode(config, LaunchMode.VULTR, "lhr")
    assert config.get_region("vultr") == "lhr"


def test_persist_region_is_noop_for_region_less_provider(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _persist_region_for_launch_mode(config, LaunchMode.DOCKER, "US-EAST-VA")
    assert config.get_region("imbue_cloud") is None
    assert config.get_region("vultr") is None


def test_persist_region_swallows_config_write_failure(tmp_path: Path) -> None:
    """A config write failure (OSError) must not escape -- it runs inside on_created,
    whose caller would otherwise mark the already-successful create as FAILED.

    Pointing the config's data_dir at an existing regular file makes
    ``set_region`` -> ``_write_raw`` -> ``data_dir.mkdir(...)`` raise a bare OSError
    (FileExistsError), which the persist helper must catch.
    """
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    config = MindsConfig(data_dir=blocker / "config-root")
    # Must not raise; the failure is swallowed at debug level.
    _persist_region_for_launch_mode(config, LaunchMode.VULTR, "lhr")
