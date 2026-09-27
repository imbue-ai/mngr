from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr_lima.backend import LimaProviderBackend
from imbue.mngr_lima.config import LimaProviderConfig
from imbue.mngr_lima.constants import LIMA_BACKEND_NAME


def test_backend_name() -> None:
    assert LimaProviderBackend.get_name() == LIMA_BACKEND_NAME
    assert LimaProviderBackend.get_name() == ProviderBackendName("lima")


def test_backend_description() -> None:
    desc = LimaProviderBackend.get_description()
    assert "Lima" in desc
    assert "VM" in desc


def test_backend_config_class() -> None:
    config_class = LimaProviderBackend.get_config_class()
    assert config_class is LimaProviderConfig
    assert issubclass(config_class, ProviderInstanceConfig)


def test_backend_build_args_help() -> None:
    help_text = LimaProviderBackend.get_build_args_help()
    assert "--file" in help_text
    assert "Lima YAML" in help_text


def test_backend_start_args_help() -> None:
    help_text = LimaProviderBackend.get_start_args_help()
    assert "limactl start" in help_text
    assert "--cpus" in help_text
