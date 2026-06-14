"""Tests for GCP provider configuration."""

import pytest

from imbue.mngr_gcp.config import DEFAULT_GCE_IMAGE
from imbue.mngr_gcp.config import GcpProviderConfig
from imbue.mngr_gcp.errors import GcpProjectError
from imbue.mngr_gcp.errors import GcpZoneRegionMismatchError


def test_default_config_values() -> None:
    config = GcpProviderConfig(project_id="my-project")
    assert config.default_region == "us-west1"
    assert config.default_zone == "us-west1-a"
    assert config.default_machine_type == "e2-small"
    assert config.default_source_image == DEFAULT_GCE_IMAGE
    # The inherited base default_image is the Docker *container* image, distinct
    # from the GCE VM source image -- they must not be conflated.
    assert config.default_image == "debian:bookworm-slim"
    assert config.boot_disk_size_gb == 30
    assert config.boot_disk_type == "pd-balanced"
    assert config.network == "default"
    assert config.subnetwork is None
    # Open by default (fail-open) to match the AWS provider; a warning is logged
    # at prepare/create time and production users are expected to tighten it.
    assert config.allowed_ssh_cidrs == ("0.0.0.0/0",)
    assert config.firewall_target_tag == "mngr-ssh"
    assert config.associate_external_ip is True
    assert config.auto_shutdown_seconds is None


def test_backend_name_defaults_to_gcp() -> None:
    config = GcpProviderConfig(project_id="my-project")
    assert str(config.backend) == "gcp"


def test_resolve_project_id_returns_configured() -> None:
    config = GcpProviderConfig(project_id="my-project")
    # The configured project_id wins even when ADC resolved a different one.
    assert config.resolve_project_id(None) == "my-project"


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
