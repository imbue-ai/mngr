"""Tests for VPS Docker provider configuration."""

from pathlib import Path

from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import DockerBuilder
from imbue.mngr.primitives import IdleMode
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr_vps_docker.config import VpsDockerProviderConfig


def test_default_config_values() -> None:
    # Deliberate change-detector on the public default contract (also documented
    # in README.md): a typo'd default (e.g. an idle timeout or port flip) must
    # fail here. Update these intentionally when defaults change.
    config = VpsDockerProviderConfig(backend=ProviderBackendName("test-backend"))
    assert config.host_dir == Path("/mngr")
    assert config.default_image == "debian:bookworm-slim"
    assert config.default_idle_timeout == 800
    assert config.default_idle_mode == IdleMode.IO
    assert config.ssh_connect_timeout == 60.0
    assert config.vps_boot_timeout == 300.0
    assert config.docker_install_timeout == 300.0
    assert config.container_ssh_port == 2222
    assert config.default_region == "ewr"
    assert config.default_plan == "vc2-1c-1gb"
    assert config.default_os_id == 2136
    assert config.default_start_args == ()
    assert config.builder is DockerBuilder.DOCKER


def test_default_activity_sources_includes_all() -> None:
    config = VpsDockerProviderConfig(backend=ProviderBackendName("test-backend"))
    # Should contain all ActivitySource values
    for source in ActivitySource:
        assert source in config.default_activity_sources
