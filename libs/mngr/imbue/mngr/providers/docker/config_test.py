import pytest
from pydantic import ValidationError

from imbue.mngr.primitives import DockerBuilder
from imbue.mngr.providers.docker.config import DockerProviderConfig
from imbue.mngr.providers.docker.config import _emit_isolate_default_warning_once
from imbue.mngr.utils.testing import capture_loguru


def test_builder_defaults_to_docker() -> None:
    """Default is DOCKER -- depot is opt-in via settings.toml."""
    assert DockerProviderConfig(isolate_host_volumes=False).builder is DockerBuilder.DOCKER


def test_explicit_builder_is_honored() -> None:
    """`builder` is a plain config field; the constructor argument wins."""
    assert DockerProviderConfig(builder=DockerBuilder.DEPOT, isolate_host_volumes=False).builder is DockerBuilder.DEPOT


def test_build_timeout_seconds_defaults_to_ten_minutes() -> None:
    """Default build timeout is 10 minutes, matching slower base-image pulls."""
    assert DockerProviderConfig(isolate_host_volumes=False).build_timeout_seconds == 600


def test_explicit_build_timeout_seconds_is_honored() -> None:
    """`build_timeout_seconds` is configurable per provider instance."""
    assert DockerProviderConfig(build_timeout_seconds=1800, isolate_host_volumes=False).build_timeout_seconds == 1800


def test_isolate_host_volumes_defaults_to_none() -> None:
    """Default is unset (tri-state); behaves like False but warns once at load time."""
    _emit_isolate_default_warning_once.cache_clear()
    with capture_loguru(level="WARNING"):
        config = DockerProviderConfig()
    assert config.isolate_host_volumes is None


def test_isolate_host_volumes_default_emits_warning_once_per_process() -> None:
    """Leaving isolate_host_volumes unset must produce exactly one warning per process."""
    _emit_isolate_default_warning_once.cache_clear()
    with capture_loguru(level="WARNING") as log_output:
        DockerProviderConfig()
        DockerProviderConfig()
        DockerProviderConfig()
    output = log_output.getvalue()
    # Use a phrase that appears exactly once per emission, not the substring
    # "isolate_host_volumes" itself (which appears multiple times in the
    # warning body).
    assert output.count("default will change") == 1


def test_isolate_host_volumes_explicit_false_does_not_warn() -> None:
    """An explicit False is the user opting into the legacy behavior; stay silent."""
    _emit_isolate_default_warning_once.cache_clear()
    with capture_loguru(level="WARNING") as log_output:
        config = DockerProviderConfig(isolate_host_volumes=False)
    assert config.isolate_host_volumes is False
    assert "isolate_host_volumes" not in log_output.getvalue()


def test_isolate_host_volumes_explicit_true_does_not_warn() -> None:
    """An explicit True is the user opting into the new behavior; stay silent."""
    _emit_isolate_default_warning_once.cache_clear()
    with capture_loguru(level="WARNING") as log_output:
        config = DockerProviderConfig(isolate_host_volumes=True)
    assert config.isolate_host_volumes is True
    assert "isolate_host_volumes" not in log_output.getvalue()


def test_isolation_without_host_volume_is_rejected() -> None:
    """isolate_host_volumes=True without is_host_volume_created is meaningless and rejected."""
    _emit_isolate_default_warning_once.cache_clear()
    with pytest.raises(ValidationError, match="isolate_host_volumes=True requires is_host_volume_created=True"):
        DockerProviderConfig(is_host_volume_created=False, isolate_host_volumes=True)


def test_no_host_volume_with_isolation_false_is_fine() -> None:
    """The conflicting-combo check only fires when isolate=True. False / None are unrestricted."""
    _emit_isolate_default_warning_once.cache_clear()
    with capture_loguru(level="WARNING"):
        config_false = DockerProviderConfig(is_host_volume_created=False, isolate_host_volumes=False)
    assert config_false.isolate_host_volumes is False
    _emit_isolate_default_warning_once.cache_clear()
    with capture_loguru(level="WARNING"):
        config_none = DockerProviderConfig(is_host_volume_created=False)
    assert config_none.isolate_host_volumes is None
