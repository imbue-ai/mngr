from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr_smolvm.backend import SmolvmProviderBackend
from imbue.mngr_smolvm.backend import register_provider_backend
from imbue.mngr_smolvm.config import SmolvmProviderConfig
from imbue.mngr_smolvm.constants import SMOLVM_BACKEND_NAME


def test_backend_name() -> None:
    assert SmolvmProviderBackend.get_name() == SMOLVM_BACKEND_NAME


def test_backend_description_mentions_smolvm() -> None:
    assert "smolvm" in SmolvmProviderBackend.get_description()


def test_backend_config_class() -> None:
    config_class = SmolvmProviderBackend.get_config_class()
    assert config_class is SmolvmProviderConfig
    assert issubclass(config_class, ProviderInstanceConfig)


def test_backend_build_args_help_documents_archive_import() -> None:
    help_text = SmolvmProviderBackend.get_build_args_help()
    assert "--image-archive" in help_text
    assert "--from" in help_text


def test_backend_start_args_help_documents_resources() -> None:
    help_text = SmolvmProviderBackend.get_start_args_help()
    assert "--cpus" in help_text
    assert "--mem" in help_text


def test_register_provider_backend_returns_backend_and_config() -> None:
    backend_class, config_class = register_provider_backend()
    assert backend_class is SmolvmProviderBackend
    assert config_class is SmolvmProviderConfig
