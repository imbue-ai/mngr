"""Tests for GCP provider configuration."""

import pytest
from google.auth.credentials import AnonymousCredentials
from pydantic import ValidationError

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr_gcp.config import GcpProviderConfig
from imbue.mngr_gcp.config import get_gcloud_compute_zone
from imbue.mngr_gcp.errors import GcpProjectError
from imbue.mngr_gcp.errors import GcpZoneRegionMismatchError
from imbue.mngr_gcp.state_bucket import GcsStateBucket


def test_backend_name_defaults_to_gcp() -> None:
    config = GcpProviderConfig(project_id="my-project")
    assert str(config.backend) == "gcp"


def test_resolve_state_bucket_name_derives_from_project_when_unset() -> None:
    """Without an explicit name, the bucket is derived from the project id."""
    config = GcpProviderConfig(project_id="my-project")
    assert config.resolve_state_bucket_name("my-project") == "mngr-state-my-project"


def test_resolve_state_bucket_name_prefers_explicit_override() -> None:
    """An explicit ``state_bucket_name`` wins over the derived name."""
    config = GcpProviderConfig(project_id="my-project", state_bucket_name="custom-bucket")
    assert config.resolve_state_bucket_name("my-project") == "custom-bucket"


def test_is_offline_host_dir_enabled_defaults_on() -> None:
    """The offline host_dir feature is on by default (matches AWS / Azure)."""
    config = GcpProviderConfig(project_id="my-project")
    assert config.is_offline_host_dir_enabled is True


def test_build_state_bucket_returns_gcs_state_bucket() -> None:
    """``build_state_bucket`` constructs a ``GcsStateBucket`` with the resolved name."""
    config = GcpProviderConfig(project_id="my-project")
    bucket = config.build_state_bucket(
        credentials=AnonymousCredentials(),
        project_id="my-project",
        region="us-west1",
    )
    assert isinstance(bucket, GcsStateBucket)
    assert bucket.bucket_name == "mngr-state-my-project"
    assert bucket.project_id == "my-project"
    assert bucket.region == "us-west1"


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


def test_default_source_image_is_global_debian_family() -> None:
    # GCE image families are global (no per-region map), unlike AWS AMIs. Debian 13
    # matches the rest of the fleet; GCP bootstraps via the GCE startup-script, so
    # it does not need the image to ship cloud-init.
    config = GcpProviderConfig(project_id="p")
    assert "global/images/family/debian-13" in config.default_source_image


def test_root_disk_defaults_to_thirty_gb_and_the_legacy_boot_disk_field_is_unset() -> None:
    config = GcpProviderConfig(project_id="my-project")

    assert config.root_disk_size_gb == 30
    assert config.boot_disk_size_gb is None


def test_legacy_boot_disk_size_gb_sets_the_unified_root_disk_size() -> None:
    config = GcpProviderConfig.model_validate({"project_id": "my-project", "boot_disk_size_gb": 40})

    assert config.root_disk_size_gb == 40
    assert config.boot_disk_size_gb is None
    assert "root_disk_size_gb" in config.model_fields_set


def test_legacy_boot_disk_size_gb_disagreeing_with_root_disk_size_gb_is_rejected() -> None:
    with pytest.raises(ValidationError, match="boot_disk_size_gb .* and root_disk_size_gb .* disagree"):
        GcpProviderConfig.model_validate({"boot_disk_size_gb": 40, "root_disk_size_gb": 50})
