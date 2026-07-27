"""Tests for VPS provider configuration."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import DockerBuilder
from imbue.mngr.primitives import IdleMode
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr_vps.config import VpsProviderConfig


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
