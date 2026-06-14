"""Tests for GCP provider configuration."""

import pytest

from imbue.mngr_gcp.config import GcpProviderConfig
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


def test_validate_zone_in_region_accepts_matching() -> None:
    config = GcpProviderConfig(project_id="p", default_region="us-west1", default_zone="us-west1-b")
    # No exception raised.
    config.validate_zone_in_region()


def test_validate_zone_in_region_rejects_mismatch() -> None:
    config = GcpProviderConfig(project_id="p", default_region="us-west1", default_zone="us-central1-a")
    with pytest.raises(GcpZoneRegionMismatchError, match="is not in default_region"):
        config.validate_zone_in_region()


def test_default_source_image_is_global_ubuntu_family() -> None:
    # GCE image families are global (no per-region map), unlike AWS AMIs. Ubuntu
    # (not Debian) because the stock GCE Debian images do not run cloud-init.
    config = GcpProviderConfig(project_id="p")
    assert "global/images/family/ubuntu-2204-lts" in config.default_source_image
