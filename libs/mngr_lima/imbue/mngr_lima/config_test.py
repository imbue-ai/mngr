from pathlib import Path

import pytest
from pydantic import ValidationError

from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr_lima.config import LimaProviderConfig
from imbue.mngr_lima.constants import LIMA_BACKEND_NAME
from imbue.mngr_lima.constants import MINIMUM_LIMA_VERSION


def test_default_config() -> None:
    config = LimaProviderConfig()
    assert config.backend == LIMA_BACKEND_NAME
    assert config.host_dir is None
    assert config.default_image_url_aarch64 is None
    assert config.default_image_url_x86_64 is None
    assert config.default_start_args == ()
    assert config.default_idle_timeout == 800
    assert config.minimum_lima_version == MINIMUM_LIMA_VERSION
    assert config.ssh_connect_timeout == 120.0
    assert config.is_run_as_root is False


def test_run_as_root_with_btrfs_layout_is_allowed() -> None:
    config = LimaProviderConfig(is_run_as_root=True, is_host_data_volume_exposed=False)
    assert config.is_run_as_root is True
    assert config.is_host_data_volume_exposed is False


def test_run_as_root_with_exposed_volume_layout_is_rejected() -> None:
    # Root cannot traverse the 9p/reverse-sshfs bind mount, so the combination
    # must fail fast at config construction. The LimaConfigError raised by the
    # validator is surfaced (wrapped) as a pydantic ValidationError.
    with pytest.raises(ValidationError, match="is_run_as_root"):
        LimaProviderConfig(is_run_as_root=True, is_host_data_volume_exposed=True)


def test_custom_config() -> None:
    config = LimaProviderConfig(
        host_dir=Path("/custom/mngr"),
        default_idle_timeout=300,
        default_start_args=("--cpus=2",),
        minimum_lima_version=(1, 2, 0),
    )
    assert config.host_dir == Path("/custom/mngr")
    assert config.default_idle_timeout == 300
    assert config.default_start_args == ("--cpus=2",)
    assert config.minimum_lima_version == (1, 2, 0)


def test_config_backend_is_lima() -> None:
    config = LimaProviderConfig()
    assert config.backend == ProviderBackendName("lima")


def test_volume_home_path_requires_btrfs_layout() -> None:
    with pytest.raises(ValidationError, match="volume_home_path requires the btrfs additional-disk layout"):
        LimaProviderConfig(volume_home_path=Path("/home/user"), is_host_data_volume_exposed=True)


def test_volume_home_path_requires_host_dir_inside_it() -> None:
    with pytest.raises(ValidationError, match="must be a path strictly inside"):
        LimaProviderConfig(
            volume_home_path=Path("/home/user"),
            is_host_data_volume_exposed=False,
            host_dir=Path("/mngr"),
        )


def test_volume_home_path_accepted_with_host_dir_inside() -> None:
    config = LimaProviderConfig(
        volume_home_path=Path("/home/user"),
        is_host_data_volume_exposed=False,
        host_dir=Path("/home/user/.mngr"),
    )
    assert config.volume_home_path == Path("/home/user")


def test_volume_home_path_defaults_to_none() -> None:
    assert LimaProviderConfig().volume_home_path is None
