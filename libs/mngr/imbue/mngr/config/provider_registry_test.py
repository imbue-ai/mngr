"""Tests for provider config classes and registry."""

import pytest

from imbue.mngr.errors import ConfigParseError
from imbue.mngr.errors import UnknownBackendError
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.providers.docker.config import DockerProviderConfig
from imbue.mngr.providers.local.config import LocalProviderConfig
from imbue.mngr.providers.registry import get_config_class

# =============================================================================
# Tests for get_config_class
# =============================================================================


def test_get_config_class_returns_local_config() -> None:
    """get_config_class should return LocalProviderConfig for 'local'."""
    config_class = get_config_class("local")
    assert config_class is LocalProviderConfig


def test_get_config_class_raises_for_unknown_backend() -> None:
    """get_config_class should raise UnknownBackendError for unknown backend."""
    with pytest.raises(UnknownBackendError, match="Unknown provider backend"):
        get_config_class("nonexistent")


# =============================================================================
# Tests for LocalProviderConfig
# =============================================================================


def test_local_provider_config_default_backend() -> None:
    """LocalProviderConfig should have 'local' as default backend."""
    config = LocalProviderConfig()
    assert config.backend == ProviderBackendName("local")


def test_local_provider_config_merge_with_returns_override_backend() -> None:
    """LocalProviderConfig.merge_with should return override's backend."""
    base = LocalProviderConfig(backend=ProviderBackendName("local"))
    override = LocalProviderConfig(backend=ProviderBackendName("local"))
    merged = base.merge_with(override)
    assert merged.backend == ProviderBackendName("local")


def test_local_provider_config_merge_with_raises_for_different_type() -> None:
    """LocalProviderConfig.merge_with should raise for different config type."""
    base = LocalProviderConfig()
    override = DockerProviderConfig()
    with pytest.raises(ConfigParseError, match="Cannot merge LocalProviderConfig"):
        base.merge_with(override)


# =============================================================================
# Tests for DockerProviderConfig
# =============================================================================


def test_docker_provider_config_default_values() -> None:
    """DockerProviderConfig should have correct default values."""
    config = DockerProviderConfig()
    assert config.backend == ProviderBackendName("docker")
    assert config.host == ""


def test_docker_provider_config_merge_with_overrides_host() -> None:
    """DockerProviderConfig.merge_with should override host."""
    base = DockerProviderConfig(host="ssh://base@server")
    override = DockerProviderConfig(host="ssh://override@server")
    merged = base.merge_with(override)
    assert isinstance(merged, DockerProviderConfig)
    assert merged.host == "ssh://override@server"


def test_docker_provider_config_merge_with_raises_for_different_type() -> None:
    """DockerProviderConfig.merge_with should raise for different config type."""
    base = DockerProviderConfig()
    override = LocalProviderConfig()
    with pytest.raises(ConfigParseError, match="Cannot merge DockerProviderConfig"):
        base.merge_with(override)
