from pathlib import Path

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
