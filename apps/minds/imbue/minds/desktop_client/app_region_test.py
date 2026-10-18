"""Unit tests for the create-form region resolution helpers in ``workspace_create``."""

from pathlib import Path

from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.region_preference import GeoLocationCache
from imbue.minds.desktop_client.region_preference import MODAL_PROVIDER_KEY
from imbue.minds.desktop_client.region_preference import MODAL_REGION_ANY
from imbue.minds.desktop_client.region_preference import MODAL_REGION_NEAREST
from imbue.minds.desktop_client.region_preference import modal_region_choices
from imbue.minds.desktop_client.workspace_create import ResolvedRegion
from imbue.minds.desktop_client.workspace_create import default_modal_region_choice
from imbue.minds.desktop_client.workspace_create import persist_region_for_launch_mode
from imbue.minds.desktop_client.workspace_create import region_provider_key_for_launch_mode
from imbue.minds.desktop_client.workspace_create import resolve_effective_region
from imbue.minds.primitives import LaunchMode


def _config(tmp_path: Path) -> MindsConfig:
    return MindsConfig(data_dir=tmp_path)


def test_region_provider_key_maps_only_region_bearing_modes() -> None:
    assert region_provider_key_for_launch_mode(LaunchMode.IMBUE_CLOUD) == "imbue_cloud"
    assert region_provider_key_for_launch_mode(LaunchMode.VULTR) == "vultr"
    # The cloud modes are connection-backed with placement pinned per
    # account entry, so they carry no ambient region key.
    assert region_provider_key_for_launch_mode(LaunchMode.AWS) is None
    assert region_provider_key_for_launch_mode(LaunchMode.DOCKER) is None
    assert region_provider_key_for_launch_mode(LaunchMode.LIMA) is None


def test_resolve_effective_region_uses_submitted_known_region(tmp_path: Path) -> None:
    region = resolve_effective_region(
        LaunchMode.IMBUE_CLOUD, "US-WEST-OR", _config(tmp_path), GeoLocationCache()
    ).region
    assert region == "US-WEST-OR"


def test_resolve_effective_region_ignores_unknown_submitted_and_falls_back_to_default(tmp_path: Path) -> None:
    # No stored value, no geo -> hardcoded default for the provider.
    region = resolve_effective_region(LaunchMode.VULTR, "not-a-region", _config(tmp_path), GeoLocationCache()).region
    assert region == "ewr"


def test_resolve_effective_region_prefers_stored_value_when_no_submission(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.set_region("imbue_cloud", "US-WEST-OR")
    region = resolve_effective_region(LaunchMode.IMBUE_CLOUD, "", config, GeoLocationCache()).region
    assert region == "US-WEST-OR"


def test_resolve_effective_region_is_empty_for_region_less_provider(tmp_path: Path) -> None:
    assert (
        resolve_effective_region(LaunchMode.DOCKER, "US-WEST-OR", _config(tmp_path), GeoLocationCache()).region == ""
    )


def test_persist_region_writes_back_for_region_bearing_provider(tmp_path: Path) -> None:
    config = _config(tmp_path)
    persist_region_for_launch_mode(config, LaunchMode.VULTR, "lhr")
    assert config.get_region("vultr") == "lhr"


def test_persist_region_is_noop_for_region_less_provider(tmp_path: Path) -> None:
    config = _config(tmp_path)
    persist_region_for_launch_mode(config, LaunchMode.DOCKER, "US-EAST-VA")
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
    persist_region_for_launch_mode(config, LaunchMode.VULTR, "lhr")


def test_modal_region_defaults_to_any_and_remembers_the_choice(tmp_path: Path) -> None:
    config = _config(tmp_path)
    # No submission and nothing stored: no placement constraint at all.
    resolved = resolve_effective_region(LaunchMode.MODAL, "", config, GeoLocationCache())
    assert resolved.region == ""
    assert resolved.choice_to_remember == MODAL_REGION_ANY
    assert modal_region_choices()[:2] == (MODAL_REGION_ANY, MODAL_REGION_NEAREST)
    assert default_modal_region_choice(config) == MODAL_REGION_ANY

    # A named region rides through and is remembered as such.
    named = resolve_effective_region(LaunchMode.MODAL, "eu-west", config, GeoLocationCache())
    assert named == ResolvedRegion(region="eu-west", choice_to_remember="eu-west")
    persist_region_for_launch_mode(config, LaunchMode.MODAL, named.choice_to_remember)
    assert default_modal_region_choice(config) == "eu-west"

    # "any" wins over the stored region and is itself remembered.
    any_region = resolve_effective_region(LaunchMode.MODAL, MODAL_REGION_ANY, config, GeoLocationCache())
    assert any_region == ResolvedRegion(region="", choice_to_remember=MODAL_REGION_ANY)
    persist_region_for_launch_mode(config, LaunchMode.MODAL, any_region.choice_to_remember)
    assert config.get_region(MODAL_PROVIDER_KEY) == MODAL_REGION_ANY
    assert resolve_effective_region(LaunchMode.MODAL, "not-a-region", config, GeoLocationCache()).region == ""


def test_modal_nearest_resolves_at_create_time_but_remembers_nearest(tmp_path: Path) -> None:
    config = _config(tmp_path)
    cache = GeoLocationCache()
    # Dublin.
    cache.set_coordinates((53.35, -6.26))
    resolved = resolve_effective_region(LaunchMode.MODAL, MODAL_REGION_NEAREST, config, cache)
    assert resolved == ResolvedRegion(region="eu-west", choice_to_remember=MODAL_REGION_NEAREST)
    # Without a geolocation the hardcoded fallback stands in; the choice is still "nearest".
    assert resolve_effective_region(LaunchMode.MODAL, MODAL_REGION_NEAREST, config, None) == ResolvedRegion(
        region="us-east", choice_to_remember=MODAL_REGION_NEAREST
    )
    persist_region_for_launch_mode(config, LaunchMode.MODAL, resolved.choice_to_remember)
    assert default_modal_region_choice(config) == MODAL_REGION_NEAREST
