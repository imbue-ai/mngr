from pathlib import Path

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


def test_volume_mount_path_requires_isolation() -> None:
    """volume_mount_path only works with the isolated volume-subpath mount."""
    with pytest.raises(ValidationError, match="volume_mount_path requires isolate_host_volumes=true"):
        DockerProviderConfig(volume_mount_path=Path("/home/user"), isolate_host_volumes=False)


def test_volume_mount_path_requires_host_dir_inside_it() -> None:
    """host_dir must live strictly inside volume_mount_path so mngr data rides the volume."""
    with pytest.raises(ValidationError, match="must be a path strictly inside"):
        DockerProviderConfig(
            volume_mount_path=Path("/home/user"),
            isolate_host_volumes=True,
            host_dir=Path("/mngr"),
        )
    # host_dir equal to the mount path is also rejected (mngr data would BE the volume root).
    with pytest.raises(ValidationError, match="must be a path strictly inside"):
        DockerProviderConfig(
            volume_mount_path=Path("/home/user"),
            isolate_host_volumes=True,
            host_dir=Path("/home/user"),
        )


def test_volume_mount_path_accepted_with_host_dir_inside() -> None:
    """The intended home-as-volume configuration validates cleanly."""
    config = DockerProviderConfig(
        volume_mount_path=Path("/home/user"),
        isolate_host_volumes=True,
        host_dir=Path("/home/user/.mngr"),
    )
    assert config.volume_mount_path == Path("/home/user")


def test_volume_mount_path_defaults_to_none() -> None:
    """Unset volume_mount_path preserves today's mount-at-host_dir behavior."""
    assert DockerProviderConfig(isolate_host_volumes=True).volume_mount_path is None
