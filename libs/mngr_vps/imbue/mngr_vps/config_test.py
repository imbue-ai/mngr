"""Tests for VPS provider configuration."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import DockerBuilder
from imbue.mngr.primitives import IdleMode
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr_vps.config import OfflineCapableVpsProviderConfig
from imbue.mngr_vps.config import VpsProviderConfig
from imbue.mngr_vps.config import fold_legacy_root_disk_size_key
from imbue.mngr_vps.errors import VpsConfigError


def test_default_config_values() -> None:
    # Deliberate change-detector on the public default contract (also documented
    # in README.md): a typo'd default (e.g. an idle timeout or port flip) must
    # fail here. Update these intentionally when defaults change.
    config = VpsProviderConfig(backend=ProviderBackendName("test-backend"))
    assert config.host_dir == Path("/mngr")
    assert config.default_image == "debian:bookworm-slim"
    assert config.default_idle_timeout == 800
    assert config.default_idle_mode == IdleMode.IO
    assert config.ssh_connect_timeout == 60.0
    assert config.instance_boot_timeout == 300.0
    assert config.docker_install_timeout == 300.0
    assert config.container_ssh_port == 2222
    assert config.default_region == "ewr"
    # default_plan moved off the shared base; each provider's config carries its
    # own native field (Vultr/OVH ``default_plan``, AWS ``default_instance_type``).
    assert not hasattr(config, "default_plan")
    assert config.default_start_args == ()
    assert config.builder is DockerBuilder.DOCKER


def test_default_activity_sources_includes_all() -> None:
    config = VpsProviderConfig(backend=ProviderBackendName("test-backend"))
    # Should contain all ActivitySource values
    for source in ActivitySource:
        assert source in config.default_activity_sources


def test_volume_home_path_requires_host_dir_inside_it() -> None:
    with pytest.raises(ValidationError, match="must be a path strictly inside"):
        VpsProviderConfig(
            backend=ProviderBackendName("test-backend"),
            volume_home_path=Path("/home/user"),
            host_dir=Path("/mngr"),
        )


def test_volume_home_path_accepted_with_host_dir_inside() -> None:
    config = VpsProviderConfig(
        backend=ProviderBackendName("test-backend"),
        volume_home_path=Path("/home/user"),
        host_dir=Path("/home/user/.mngr"),
    )
    assert config.volume_home_path == Path("/home/user")


def test_volume_home_path_defaults_to_none() -> None:
    assert VpsProviderConfig(backend=ProviderBackendName("test-backend")).volume_home_path is None


def test_fold_legacy_root_disk_size_key_moves_the_legacy_value_onto_the_unified_key() -> None:
    folded = fold_legacy_root_disk_size_key({"backend": "aws", "root_volume_size_gb": 40}, "root_volume_size_gb")

    assert folded == {"backend": "aws", "root_disk_size_gb": 40}


def test_fold_legacy_root_disk_size_key_leaves_a_config_without_the_legacy_key_alone() -> None:
    raw_config = {"backend": "aws", "root_disk_size_gb": 40}

    assert fold_legacy_root_disk_size_key(raw_config, "root_volume_size_gb") is raw_config


def test_fold_legacy_root_disk_size_key_ignores_a_none_legacy_value() -> None:
    folded = fold_legacy_root_disk_size_key(
        {"root_volume_size_gb": None, "root_disk_size_gb": 50}, "root_volume_size_gb"
    )

    assert folded == {"root_volume_size_gb": None, "root_disk_size_gb": 50}


def test_fold_legacy_root_disk_size_key_accepts_both_keys_when_they_agree() -> None:
    folded = fold_legacy_root_disk_size_key(
        {"root_volume_size_gb": 40, "root_disk_size_gb": 40}, "root_volume_size_gb"
    )

    assert folded == {"root_disk_size_gb": 40}


def test_fold_legacy_root_disk_size_key_rejects_disagreeing_keys() -> None:
    with pytest.raises(VpsConfigError, match="root_volume_size_gb .* and root_disk_size_gb .* disagree"):
        fold_legacy_root_disk_size_key({"root_volume_size_gb": 40, "root_disk_size_gb": 50}, "root_volume_size_gb")


def test_fold_legacy_root_disk_size_key_passes_a_non_mapping_through() -> None:
    config = VpsProviderConfig(backend=ProviderBackendName("test-backend"))

    assert fold_legacy_root_disk_size_key(config, "root_volume_size_gb") is config


def test_offline_capable_config_defaults_the_root_disk_to_thirty_gb() -> None:
    assert OfflineCapableVpsProviderConfig(backend=ProviderBackendName("test-backend")).root_disk_size_gb == 30
