"""Tests for GCP provider configuration."""

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr_gcp.config import GcpProviderConfig
from imbue.mngr_gcp.config import get_gcloud_compute_zone
from imbue.mngr_gcp.errors import GcpProjectError
from imbue.mngr_gcp.errors import GcpZoneRegionMismatchError


def test_backend_name_defaults_to_gcp() -> None:
    config = GcpProviderConfig(project_id="my-project")
    assert str(config.backend) == "gcp"


def test_resolve_project_id_prefers_configured_over_adc_fallback() -> None:
    config = GcpProviderConfig(project_id="explicit-project")
    assert config.resolve_project_id("adc-project") == "explicit-project"


def test_resolve_project_id_falls_back_to_adc_when_unset() -> None:
    # No explicit project_id: use the project ADC resolved from the environment
    # (the gcloud config / GOOGLE_CLOUD_PROJECT default).
    config = GcpProviderConfig()
    assert config.resolve_project_id("adc-project") == "adc-project"


def test_resolve_project_id_raises_when_unset_and_no_fallback() -> None:
    config = GcpProviderConfig()
    with pytest.raises(GcpProjectError, match="No GCP project_id configured"):
        config.resolve_project_id(None)


def test_resolve_zone_and_region_prefers_explicit_zone_over_gcloud() -> None:
    config = GcpProviderConfig(project_id="p", default_zone="us-west1-b")
    # Explicit default_zone wins over the gcloud-derived fallback; region is
    # derived from the resolved zone.
    assert config.resolve_zone_and_region("europe-west1-c") == ("us-west1-b", "us-west1")


def test_resolve_zone_and_region_uses_gcloud_zone_when_unset() -> None:
    config = GcpProviderConfig(project_id="p")
    # No explicit default_zone: the injected gcloud zone is used, region derived.
    assert config.resolve_zone_and_region("europe-west1-c") == ("europe-west1-c", "europe-west1")


def test_resolve_zone_and_region_falls_back_to_hardcoded_default() -> None:
    config = GcpProviderConfig(project_id="p")
    # No explicit default_zone and no gcloud zone: the hardcoded default applies.
    assert config.resolve_zone_and_region(None) == ("us-west1-a", "us-west1")


def test_resolve_zone_and_region_accepts_matching_explicit_region() -> None:
    config = GcpProviderConfig(project_id="p", default_region="us-west1", default_zone="us-west1-b")
    assert config.resolve_zone_and_region(None) == ("us-west1-b", "us-west1")


def test_resolve_zone_and_region_rejects_mismatched_explicit_region() -> None:
    config = GcpProviderConfig(project_id="p", default_region="us-west1", default_zone="us-central1-a")
    with pytest.raises(GcpZoneRegionMismatchError, match="is not in region"):
        config.resolve_zone_and_region(None)


def test_get_gcloud_compute_zone_honors_contract(temp_mngr_ctx: MngrContext) -> None:
    # Best-effort boundary helper: it must never raise and must return either
    # None (gcloud absent / unset / error / timeout) or a non-empty zone string,
    # regardless of the host's gcloud state. We assert the contract, not a
    # specific zone, so the test is hermetic across machines with and without a
    # configured gcloud CLI.
    result = get_gcloud_compute_zone(temp_mngr_ctx.concurrency_group)
    assert result is None or (isinstance(result, str) and result != "")


def test_default_source_image_is_global_ubuntu_family() -> None:
    # GCE image families are global (no per-region map), unlike AWS AMIs. Ubuntu
    # (not Debian) because the stock GCE Debian images do not run cloud-init.
    config = GcpProviderConfig(project_id="p")
    assert "global/images/family/ubuntu-2204-lts" in config.default_source_image
