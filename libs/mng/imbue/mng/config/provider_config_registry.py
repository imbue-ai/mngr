from imbue.mng.config.data_types import ProviderInstanceConfig
from imbue.mng.errors import UnknownBackendError
from imbue.mng.primitives import ProviderBackendName

# =============================================================================
# Provider Config Registry
# =============================================================================

_provider_config_registry: dict[ProviderBackendName, type[ProviderInstanceConfig]] = {}


def register_provider_config(
    backend_name: str,
    config_class: type[ProviderInstanceConfig],
) -> None:
    """Register a config class for a provider backend."""
    _provider_config_registry[ProviderBackendName(backend_name)] = config_class


def get_provider_config_class(backend_name: str) -> type[ProviderInstanceConfig]:
    """Get the config class for a provider backend.

    Raises UnknownBackendError if the backend is not registered.
    """
    key = ProviderBackendName(backend_name)
    if key not in _provider_config_registry:
        registered = ", ".join(sorted(str(k) for k in _provider_config_registry.keys()))
        raise UnknownBackendError(f"Unknown provider backend: {key}. Registered backends: {registered or '(none)'}")
    return _provider_config_registry[key]


def reset_provider_config_registry() -> None:
    """Reset the registry. Used for test isolation."""
    _provider_config_registry.clear()
