from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr_sbx.backend import SbxProviderBackend
from imbue.mngr_sbx.backend import register_provider_backend
from imbue.mngr_sbx.config import SbxProviderConfig


def test_sbx_provider_backend_returns_sbx_name() -> None:
    assert SbxProviderBackend.get_name() == "sbx"


def test_sbx_provider_backend_description_mentions_sandboxes() -> None:
    description = SbxProviderBackend.get_description()
    assert "Sandbox" in description or "sbx" in description


def test_sbx_provider_backend_get_config_class_is_sbx_config() -> None:
    assert SbxProviderBackend.get_config_class() is SbxProviderConfig


def test_register_provider_backend_returns_backend_and_config_pair() -> None:
    result = register_provider_backend()
    backend_class, config_class = result
    assert backend_class is SbxProviderBackend
    assert config_class is SbxProviderConfig
    # The returned types must match the interface so pluggy can dispatch.
    assert issubclass(backend_class, ProviderBackendInterface)


def test_sbx_provider_backend_build_args_help_mentions_workspace() -> None:
    help_text = SbxProviderBackend.get_build_args_help()
    assert "workspace" in help_text


def test_sbx_provider_backend_start_args_help_mentions_sbx_create() -> None:
    help_text = SbxProviderBackend.get_start_args_help()
    assert "sbx create" in help_text
