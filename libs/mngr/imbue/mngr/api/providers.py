import atexit

from loguru import logger

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.base_provider import BaseProviderInstance
from imbue.mngr.providers.registry import build_provider_instance
from imbue.mngr.providers.registry import get_config_class
from imbue.mngr.providers.registry import list_backends

# Cache provider instances by (name, mngr_ctx identity) so the same instance
# is reused across calls within the same context. This prevents accumulating
# duplicate instances (and their SSH connections) when discovery runs repeatedly.
_instance_cache: dict[tuple[ProviderInstanceName, int], BaseProviderInstance] = {}
_atexit_registered: dict[str, bool] = {"registered": False}


def _close_all_provider_instances() -> None:
    """Close all cached provider instances.

    Called via atexit to ensure proper cleanup of resources like Modal app contexts.
    """
    for instance in _instance_cache.values():
        try:
            instance.close()
        except (MngrError, OSError) as e:
            logger.warning("Error closing provider instance {}: {}", instance.name, e)
    _instance_cache.clear()


def _ensure_atexit_registered() -> None:
    """Register the atexit handler if not already registered."""
    if not _atexit_registered["registered"]:
        atexit.register(_close_all_provider_instances)
        _atexit_registered["registered"] = True


def reset_provider_instances() -> None:
    """Reset the provider instances tracking.

    Closes all cached provider instances and clears the instance cache.
    This is primarily used for test isolation to ensure a clean state between tests.
    """
    _close_all_provider_instances()
    _atexit_registered["registered"] = False


def get_provider_instance(
    name: ProviderInstanceName,
    mngr_ctx: MngrContext,
) -> BaseProviderInstance:
    """Get or create a provider instance by name.

    Returns a cached instance if one already exists for this name and context.
    Otherwise, creates a new instance: checks config.providers first, then falls
    back to treating the name as a backend name with defaults.
    The returned instance is tracked for cleanup at process exit via atexit.
    """
    _ensure_atexit_registered()

    # Return the cached instance if one already exists for this name and context
    cache_key = (name, id(mngr_ctx))
    if cache_key in _instance_cache:
        logger.trace("Returning cached provider instance {}", name)
        return _instance_cache[cache_key]

    # Check if there's a configured provider instance with this name
    if name in mngr_ctx.config.providers:
        provider_config = mngr_ctx.config.providers[name]
        instance = build_provider_instance(
            instance_name=name,
            backend_name=provider_config.backend,
            config=provider_config,
            mngr_ctx=mngr_ctx,
        )
        logger.trace("Built provider instance {} from config with backend {}", name, provider_config.backend)
    else:
        # Otherwise, treat the name as a backend name and use defaults
        # This supports the common case of just specifying "--provider local" or "--provider docker"
        backend_name = ProviderBackendName(str(name))
        config_class = get_config_class(backend_name)
        default_config = config_class(backend=backend_name)
        instance = build_provider_instance(
            instance_name=name,
            backend_name=backend_name,
            config=default_config,
            mngr_ctx=mngr_ctx,
        )
        logger.trace("Built provider instance {} using backend name as default", name)

    _instance_cache[cache_key] = instance
    return instance


def _is_backend_enabled(backend_name: str, mngr_ctx: MngrContext) -> bool:
    """Check if a backend is enabled based on enabled_backends config.

    If enabled_backends is empty, all backends are enabled.
    If enabled_backends is non-empty, only listed backends are enabled.
    """
    enabled_backends = mngr_ctx.config.enabled_backends
    if not enabled_backends:
        return True
    return ProviderBackendName(backend_name) in enabled_backends


def list_provider_names_to_load(
    mngr_ctx: MngrContext,
    provider_names: tuple[str, ...] | None = None,
) -> list[ProviderInstanceName]:
    """Return the names of providers that should be loaded for the given context.

    Applies the same filtering rules as get_all_provider_instances but does not
    instantiate any providers. This lets callers construct each provider in a
    per-thread try/except so a single broken provider does not block others
    (e.g. Modal failing auth should not prevent Lima from being listed).

    Returns names from configured providers (config.providers) plus default
    instances for all registered backends not already configured, excluding:
    - Backends disabled via --disable-plugin
    - Provider instances with is_enabled=False in their config
    - Backends not in enabled_backends list (if the list is non-empty)
    - Providers not in provider_names (if provider_names is specified)
    """
    names: list[ProviderInstanceName] = []
    seen_names: set[str] = set()
    disabled = mngr_ctx.config.disabled_plugins

    provider_filter: set[str] | None = set(provider_names) if provider_names else None

    # First, configured providers
    for name, provider_config in mngr_ctx.config.providers.items():
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
        if not _is_backend_enabled(str(provider_config.backend), mngr_ctx):
            logger.trace("Skipped provider {} (backend {} not in enabled_backends)", name, provider_config.backend)
            continue
        names.append(name)

    # Then, default instances for backends not already configured
    for backend_name in list_backends():
        if provider_filter is not None and backend_name not in provider_filter:
            logger.trace("Skipped backend {} (not in provider filter)", backend_name)
            continue
        if backend_name in disabled:
            logger.trace("Skipped disabled backend {}", backend_name)
            continue
        if not _is_backend_enabled(backend_name, mngr_ctx):
            logger.trace("Skipped backend {} (not in enabled_backends)", backend_name)
            continue
        if backend_name not in seen_names:
            names.append(ProviderInstanceName(backend_name))
            seen_names.add(backend_name)

    return names


def get_all_provider_instances(
    mngr_ctx: MngrContext,
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

    Raises MngrError if any provider fails to instantiate. Callers that want to
    tolerate per-provider instantiation errors (e.g. mngr list --on-error continue)
    should use list_provider_names_to_load and construct each provider themselves
    via get_provider_instance, wrapping in try/except.
    """
    providers: list[BaseProviderInstance] = [
        get_provider_instance(name, mngr_ctx) for name in list_provider_names_to_load(mngr_ctx, provider_names)
    ]

    if reset_caches:
        for provider in providers:
            provider.reset_caches()

    logger.trace("Loaded {} total provider instances", len(providers))
    return providers
