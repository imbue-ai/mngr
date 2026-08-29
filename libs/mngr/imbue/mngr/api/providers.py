import atexit
import threading

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
# Guards ``_instance_cache`` and the atexit-registration flag. The discovery
# stream builds one provider per poller thread, so these are read and written
# concurrently; without the lock, the atexit close below can iterate the cache
# while a still-running poller inserts into it.
_instance_cache_lock = threading.Lock()
_atexit_registered: dict[str, bool] = {"registered": False}


def _close_all_provider_instances() -> None:
    """Close all cached provider instances.

    Called via atexit to ensure proper cleanup of resources like Modal app contexts.
    """
    with _instance_cache_lock:
        instances = list(_instance_cache.values())
        _instance_cache.clear()
    # Closed outside the lock: a close can block (an SSH teardown, a Modal app
    # context exit), and holding the lock through it would stall any provider
    # construction still in flight on another thread.
    for instance in instances:
        try:
            instance.close()
        except (MngrError, OSError) as e:
            logger.warning("Error closing provider instance {}: {}", instance.name, e)


def _ensure_atexit_registered() -> None:
    """Register the atexit handler if not already registered."""
    with _instance_cache_lock:
        if _atexit_registered["registered"]:
            return
        _atexit_registered["registered"] = True
    atexit.register(_close_all_provider_instances)


def reset_provider_instances() -> None:
    """Reset the provider instances tracking.

    Closes all cached provider instances and clears the instance cache.
    This is primarily used for test isolation to ensure a clean state between tests.
    """
    _close_all_provider_instances()
    with _instance_cache_lock:
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

    Only successful constructions are cached, so a caller that retries after a
    failure (the discovery stream's per-provider pollers do, every poll) gets a
    fresh attempt rather than a memoized failure.

    Safe to call concurrently. Callers racing on *different* names -- one poller
    thread per provider -- each build in parallel; two callers racing on the same
    name may both build, and the loser's instance is closed rather than cached.
    """
    _ensure_atexit_registered()

    # Return the cached instance if one already exists for this name and context
    cache_key = (name, id(mngr_ctx))
    with _instance_cache_lock:
        cached = _instance_cache.get(cache_key)
    if cached is not None:
        logger.trace("Returning cached provider instance {}", name)
        return cached

    # Built outside the lock: construction can take a network round trip (Modal
    # checks whether the per-user environment exists), and serializing that across
    # providers is exactly what the per-provider discovery pollers exist to avoid.
    _, provider_config = resolve_backend_and_config(name, mngr_ctx)
    instance = build_provider_instance(
        instance_name=name,
        backend_name=provider_config.backend,
        config=provider_config,
        mngr_ctx=mngr_ctx,
    )
    logger.trace("Built provider instance {} with backend {}", name, provider_config.backend)

    with _instance_cache_lock:
        existing = _instance_cache.get(cache_key)
        if existing is None:
            _instance_cache[cache_key] = instance
    if existing is not None:
        # Another thread cached one for this name first. Hand back the cached
        # instance so every caller shares one, and close the duplicate rather
        # than dropping it: it may already hold a connection nothing will reap.
        _close_unused_provider_instance(instance)
        return existing
    return instance


def _close_unused_provider_instance(instance: BaseProviderInstance) -> None:
    """Close a provider instance that lost a construction race and will never be handed out."""
    try:
        instance.close()
    except (MngrError, OSError) as e:
        logger.warning("Error closing duplicate provider instance {}: {}", instance.name, e)


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


class SkippedProviderConstruction(FrozenModel):
    """A provider instance whose construction was skipped during enumeration.

    Carries what a caller needs to surface the skip (e.g. as a per-provider
    discovery snapshot) without holding onto the exception itself.
    """

    provider_name: ProviderInstanceName = Field(description="Name of the skipped provider instance")
    error_type_name: str = Field(description="The type name of the construction exception")
    error_message: str = Field(description="The construction exception's message")
    user_help_text: str | None = Field(
        default=None,
        description=(
            "The construction exception's curated remediation, or None if it carried none. Held "
            "because a caller that turns the skip back into an error (see "
            "``_raise_for_unmatched_identifiers``) would otherwise fall back to the generic "
            "'start Docker' guidance, which the cloud backends curate this text precisely to avoid."
        ),
    )
    is_empty: bool = Field(
        description="True when the provider was reached and is known-empty, False when unavailable/unauthorized"
    )


def get_all_provider_instances_and_skipped(
    mngr_ctx: MngrContext,
    provider_names: tuple[str, ...] | None = None,
    reset_caches: bool = False,
) -> tuple[list[BaseProviderInstance], list[SkippedProviderConstruction]]:
    """Get all available provider instances, plus details of the ones that were skipped.

    If provider_names is provided, only returns providers matching those names,
    allowing skipping expensive initialization of providers that won't be used.

    Returns configured providers plus default instances for all registered backends,
    excluding (only the last two exclusions -- the construction skips -- are
    reported in the skipped list):
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
      against the providers we *can* reach.

    Only the empty/unavailable construction skips are reported in the skipped
    list -- disabled/filtered providers were deliberately excluded and are not
    "skipped" in any surfaceable sense.

    Raises MngrError if ANY provider fails to instantiate for a reason other
    than ``ProviderEmptyError`` / ``ProviderUnavailableError``. Callers that want
    to tolerate per-provider instantiation errors should use
    ``list_provider_names_to_load``.
    """
    providers: list[BaseProviderInstance] = []
    skipped: list[SkippedProviderConstruction] = []
    for name in list_provider_names_to_load(mngr_ctx, provider_names):
        try:
            providers.append(get_provider_instance(name, mngr_ctx))
        except ProviderEmptyError as e:
            logger.debug("Skipping provider {} (empty -- nothing to list): {}", name, e)
            skipped.append(
                SkippedProviderConstruction(
                    provider_name=name,
                    error_type_name=type(e).__name__,
                    error_message=str(e),
                    user_help_text=e.user_help_text,
                    is_empty=True,
                )
            )
            continue
        except ProviderUnavailableError as e:
            logger.debug("Skipping provider {} (unavailable): {}", name, e)
            skipped.append(
                SkippedProviderConstruction(
                    provider_name=name,
                    error_type_name=type(e).__name__,
                    error_message=str(e),
                    user_help_text=e.user_help_text,
                    is_empty=False,
                )
            )
            continue

    if reset_caches:
        for provider in providers:
            provider.reset_caches()

    logger.trace("Loaded {} total provider instances ({} skipped)", len(providers), len(skipped))
    return providers, skipped


def get_all_provider_instances(
    mngr_ctx: MngrContext,
    provider_names: tuple[str, ...] | None = None,
    reset_caches: bool = False,
) -> list[BaseProviderInstance]:
    """Get all available provider instances, dropping the skipped-provider details.

    See :func:`get_all_provider_instances_and_skipped` for the full contract.
    """
    providers, _skipped = get_all_provider_instances_and_skipped(
        mngr_ctx,
        provider_names=provider_names,
        reset_caches=reset_caches,
    )
    return providers
