import atexit

from loguru import logger

from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import MngError
from imbue.mng.primitives import ProviderBackendName
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.providers.base_provider import BaseProviderInstance
from imbue.mng.providers.registry import build_provider_instance
from imbue.mng.providers.registry import get_config_class
from imbue.mng.providers.registry import list_backends

# Track all created provider instances for cleanup at exit
_created_instances: list[BaseProviderInstance] = []
_atexit_registered: dict[str, bool] = {"registered": False}


def _close_all_provider_instances() -> None:
    """Close all created provider instances.

    Called via atexit to ensure proper cleanup of resources like Modal app contexts.
    """
    for instance in _created_instances:
        try:
            instance.close()
        except (MngError, OSError) as e:
            logger.warning("Error closing provider instance {}: {}", instance.name, e)
    _created_instances.clear()


def _ensure_atexit_registered() -> None:
    """Register the atexit handler if not already registered."""
    if not _atexit_registered["registered"]:
        atexit.register(_close_all_provider_instances)
        _atexit_registered["registered"] = True


def reset_provider_instances() -> None:
    """Reset the provider instances tracking.

    Closes all tracked provider instances and clears the tracking list.
    This is primarily used for test isolation to ensure a clean state between tests.
    """
    _close_all_provider_instances()
    _atexit_registered["registered"] = False


def get_provider_instance(
    name: ProviderInstanceName,
    mng_ctx: MngContext,
) -> BaseProviderInstance:
    """Get or create a provider instance by name.

    Resolution order: check config.providers, then try as backend name with defaults.
    The returned instance is tracked for cleanup at process exit via atexit.
    """
    _ensure_atexit_registered()

    # Check if there's a configured provider instance with this name
    if name in mng_ctx.config.providers:
        provider_config = mng_ctx.config.providers[name]
        instance = build_provider_instance(
            instance_name=name,
            backend_name=provider_config.backend,
            config=provider_config,
            mng_ctx=mng_ctx,
        )
        logger.trace("Built provider instance {} from config with backend {}", name, provider_config.backend)
        _created_instances.append(instance)
        return instance

    # Otherwise, treat the name as a backend name and use defaults
    # This supports the common case of just specifying "--provider local" or "--provider docker"
    backend_name = ProviderBackendName(str(name))
    config_class = get_config_class(backend_name)
    default_config = config_class(backend=backend_name)
    instance = build_provider_instance(
        instance_name=name,
        backend_name=backend_name,
        config=default_config,
        mng_ctx=mng_ctx,
    )
    logger.trace("Built provider instance {} using backend name as default", name)
    _created_instances.append(instance)
    return instance


def _is_backend_enabled(backend_name: str, mng_ctx: MngContext) -> bool:
    """Check if a backend is enabled based on enabled_backends config.

    If enabled_backends is empty, all backends are enabled.
    If enabled_backends is non-empty, only listed backends are enabled.
    """
    enabled_backends = mng_ctx.config.enabled_backends
    if not enabled_backends:
        return True
    return ProviderBackendName(backend_name) in enabled_backends


def get_all_provider_instances(
    mng_ctx: MngContext,
    provider_names: tuple[str, ...] | None = None,
    reset_caches: bool = False,
) -> list[BaseProviderInstance]:
    """Get all available provider instances.

    If provider_names is provided, only returns providers matching those names,
    allowing skipping expensive initialization of providers that won't be used.

    Returns configured providers plus default instances for all registered backends,
    excluding:
    - Backends disabled via --disable-plugin
    - Provider instances with is_enabled=False in their config
    - Backends not in enabled_backends list (if the list is non-empty)
    - Providers not in provider_names (if provider_names is specified)
    """
    providers: list[BaseProviderInstance] = []
    seen_names: set[str] = set()
    disabled = mng_ctx.config.disabled_plugins

    # Convert provider_names to a set for efficient lookup
    provider_filter: set[str] | None = set(provider_names) if provider_names else None

    # First, add all configured providers (unless disabled or not enabled)
    for name, provider_config in mng_ctx.config.providers.items():
        seen_names.add(str(name))
        if provider_filter is not None and str(name) not in provider_filter:
            logger.trace("Skipped provider {} (not in provider filter)", name)
            continue
        if str(name) in disabled:
            logger.trace("Skipped disabled provider {}", name)
            continue
        if provider_config.is_enabled is False:
            logger.trace("Skipped provider {} (is_enabled=False)", name)
            continue
        if not _is_backend_enabled(str(provider_config.backend), mng_ctx):
            logger.trace("Skipped provider {} (backend {} not in enabled_backends)", name, provider_config.backend)
            continue
        providers.append(get_provider_instance(name, mng_ctx))

    # Then, add default instances for backends not already configured (unless disabled)
    for backend_name in list_backends():
        if provider_filter is not None and backend_name not in provider_filter:
            logger.trace("Skipped backend {} (not in provider filter)", backend_name)
            continue
        if backend_name in disabled:
            logger.trace("Skipped disabled backend {}", backend_name)
            continue
        if not _is_backend_enabled(backend_name, mng_ctx):
            logger.trace("Skipped backend {} (not in enabled_backends)", backend_name)
            continue
        if backend_name not in seen_names:
            provider_name = ProviderInstanceName(backend_name)
            providers.append(get_provider_instance(provider_name, mng_ctx))
            seen_names.add(backend_name)

    if reset_caches:
        for provider in providers:
            provider.reset_caches()

    logger.trace("Loaded {} total provider instances", len(providers))
    return providers
