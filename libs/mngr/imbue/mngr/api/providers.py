import atexit

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderEmptyError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.base_provider import BaseProviderInstance
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.registry import build_provider_instance
from imbue.mngr.providers.registry import list_backends
from imbue.mngr.providers.registry import resolve_backend_and_config

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

    Always treated as read-only-or-existing-host construction: backends must
    not bootstrap one-time resources here. Callers about to create a host
    should first call ``backend.bootstrap_for_host_creation(...)`` directly
    (see ``api/create.py``).
    """
    _ensure_atexit_registered()

    # Return the cached instance if one already exists for this name and context
    cache_key = (name, id(mngr_ctx))
    if cache_key in _instance_cache:
        logger.trace("Returning cached provider instance {}", name)
        return _instance_cache[cache_key]

    _, provider_config = resolve_backend_and_config(name, mngr_ctx)
    instance = build_provider_instance(
        instance_name=name,
        backend_name=provider_config.backend,
        config=provider_config,
        mngr_ctx=mngr_ctx,
    )
    logger.trace("Built provider instance {} with backend {}", name, provider_config.backend)

    _instance_cache[cache_key] = instance
    return instance


def get_local_host(mngr_ctx: MngrContext) -> OnlineHostInterface:
    """Resolve the local host as an OnlineHostInterface.

    This is the canonical way to obtain a local host to use as an rsync/copy
    source (e.g. for ``remote_host.copy_directory(local_host, ...)``) or to run
    local commands through the host interface.
    """
    provider = get_provider_instance(LOCAL_PROVIDER_NAME, mngr_ctx)
    host_interface = provider.get_host(HostName(LOCAL_HOST_NAME))
    if not isinstance(host_interface, OnlineHostInterface):
        raise MngrError("Local host is not online")
    return host_interface


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
    """Return name of the providers that should be loaded for the given context.

    Returns names from configured providers plus default instances for all registered backends not already configured, excluding:
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


class ProviderInstancesResult(FrozenModel):
    """The provider instances that loaded, plus any that were skipped as unavailable.

    ``instances`` holds every provider that constructed successfully.
    ``unavailable_provider_names`` holds providers that raised
    ``ProviderUnavailableError`` (backend unreachable, state UNKNOWN -- agents
    managed by them may still exist). Because this means the instance set is
    *incomplete*, callers that operate on it as if it were authoritative (e.g.
    ``mngr gc``) should surface the names so the user knows the run was degraded
    against an unreachable backend. Providers skipped as ``ProviderEmptyError``
    (provably empty -- nothing there) are NOT reported here, since dropping them
    loses nothing.
    """

    instances: tuple[BaseProviderInstance, ...] = Field(
        default=(), description="Provider instances that constructed successfully"
    )
    unavailable_provider_names: tuple[ProviderInstanceName, ...] = Field(
        default=(),
        description="Providers skipped because they were unreachable (ProviderUnavailableError); the set is degraded",
    )


def get_all_provider_instances(
    mngr_ctx: MngrContext,
    provider_names: tuple[str, ...] | None = None,
    reset_caches: bool = False,
) -> ProviderInstancesResult:
    """Get all available provider instances.

    If provider_names is provided, only returns providers matching those names,
    allowing skipping expensive initialization of providers that won't be used.

    Returns a :class:`ProviderInstancesResult` carrying the configured providers
    plus default instances for all registered backends, excluding:
    - Backends disabled via --disable-plugin
    - Provider instances with is_enabled=False in their config
    - Backends not in enabled_backends list (if the list is non-empty)
    - Providers not in provider_names (if provider_names is specified)
    - Provider instances that declare themselves empty at construction time
      (by raising ``ProviderEmptyError``). This is how the Modal backend
      disables itself when its per-user environment doesn't exist yet -- so
      commands like ``mngr list`` and ``mngr gc`` do not silently bootstrap
      a Modal environment.
    - Provider instances that declare themselves unreachable at construction
      time (by raising ``ProviderUnavailableError``). The backend's state is
      unknown in this case, but for ``mngr gc`` we still want to keep going
      against the providers we *can* reach. Unlike the empty case, these are
      reported in ``unavailable_provider_names`` so callers can surface that
      the result is incomplete rather than treating a partial set as complete.

    Raises MngrError if ANY provider fails to instantiate for a reason other
    than ``ProviderEmptyError`` / ``ProviderUnavailableError``. Callers that want
    to tolerate per-provider instantiation errors should use
    ``list_provider_names_to_load``.
    """
    instances: list[BaseProviderInstance] = []
    unavailable_provider_names: list[ProviderInstanceName] = []
    for name in list_provider_names_to_load(mngr_ctx, provider_names):
        try:
            instances.append(get_provider_instance(name, mngr_ctx))
        except ProviderEmptyError as e:
            logger.debug("Skipping provider {} (empty -- nothing to list): {}", name, e)
            continue
        except ProviderUnavailableError as e:
            logger.debug("Skipping provider {} (unavailable): {}", name, e)
            unavailable_provider_names.append(name)
            continue

    if reset_caches:
        for provider in instances:
            provider.reset_caches()

    logger.trace(
        "Loaded {} total provider instances ({} unavailable)", len(instances), len(unavailable_provider_names)
    )
    return ProviderInstancesResult(
        instances=tuple(instances),
        unavailable_provider_names=tuple(unavailable_provider_names),
    )
