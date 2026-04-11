from collections.abc import Sequence
from concurrent.futures import Future
from threading import Lock

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.executor import ConcurrencyGroupExecutor
from imbue.imbue_common.logging import log_call
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.pure import pure
from imbue.mngr.api.discovery_events import resolve_provider_names_for_identifiers
from imbue.mngr.api.providers import get_all_provider_instances
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.base_provider import BaseProviderInstance


def warn_on_duplicate_host_names(
    agents_by_host: dict[DiscoveredHost, list[DiscoveredAgent]],
) -> None:
    """Emit a warning if any host names are duplicated within the same provider.

    This should never happen in normal operation -- it indicates a bug or race condition
    in host creation.

    Only considers hosts that have at least one agent reference, since destroyed
    hosts (which typically have no agents) may legitimately share a name with a
    newly created host.
    """
    # Group host names by provider, tracking which host IDs share each name
    host_ids_by_provider_and_name: dict[tuple[ProviderInstanceName, HostName], list[HostId]] = {}
    for host_ref, agent_refs in agents_by_host.items():
        if not agent_refs:
            continue
        key = (host_ref.provider_name, host_ref.host_name)
        host_ids_by_provider_and_name.setdefault(key, []).append(host_ref.host_id)

    for (provider_name, host_name), host_ids in host_ids_by_provider_and_name.items():
        if len(host_ids) > 1:
            logger.warning(
                "Duplicate host name '{}' found on provider '{}' (host IDs: {}). "
                "This should never happen -- it may indicate a bug or a race condition during host creation.",
                host_name,
                provider_name,
                ", ".join(str(host_id) for host_id in host_ids),
            )


def _discover_provider_hosts_and_agents(
    provider: BaseProviderInstance,
    agents_by_host: dict[DiscoveredHost, list[DiscoveredAgent]],
    include_destroyed: bool,
    results_lock: Lock,
    cg: ConcurrencyGroup,
) -> None:
    """Discover hosts and agents from a single provider.

    This function is run in a thread by discover_hosts_and_agents.
    Results are merged into the shared agents_by_host dict under the results_lock.
    """
    provider_results = provider.discover_hosts_and_agents(cg=cg, include_destroyed=include_destroyed)

    # Merge results into the main dict under lock
    with results_lock:
        agents_by_host.update(provider_results)


def _run_discovery(
    mngr_ctx: MngrContext,
    provider_names: tuple[str, ...] | None,
    include_destroyed: bool,
    reset_caches: bool,
    existing_providers: list[BaseProviderInstance] | None = None,
) -> tuple[dict[DiscoveredHost, list[DiscoveredAgent]], list[BaseProviderInstance]]:
    """Run the actual discovery against providers. Shared implementation for discover_hosts_and_agents."""
    agents_by_host: dict[DiscoveredHost, list[DiscoveredAgent]] = {}
    results_lock = Lock()

    providers = (
        existing_providers if existing_providers is not None else get_all_provider_instances(mngr_ctx, provider_names)
    )
    logger.trace("Found {} provider instances", len(providers))

    if reset_caches:
        logger.debug("Resetting provider caches before discovery")
        for provider in providers:
            provider.reset_caches()

    # Process all providers in parallel using ConcurrencyGroupExecutor
    futures: list[Future[None]] = []
    with ConcurrencyGroupExecutor(
        parent_cg=mngr_ctx.concurrency_group, name="discover_hosts_and_agents", max_workers=32
    ) as executor:
        for provider in providers:
            futures.append(
                executor.submit(
                    _discover_provider_hosts_and_agents,
                    provider,
                    agents_by_host,
                    include_destroyed,
                    results_lock,
                    mngr_ctx.concurrency_group,
                )
            )

    # Re-raise any thread exceptions
    for future in futures:
        future.result()

    # Warn if any host names are duplicated within the same provider
    warn_on_duplicate_host_names(agents_by_host)

    return (agents_by_host, providers)


@pure
def _all_identifiers_found(
    identifiers: Sequence[str],
    agents_by_host: dict[DiscoveredHost, list[DiscoveredAgent]],
) -> bool:
    """Check whether all requested agent identifiers appear in the discovery results."""
    remaining = set(identifiers)
    for agent_refs in agents_by_host.values():
        for agent_ref in agent_refs:
            remaining.discard(str(agent_ref.agent_id))
            remaining.discard(str(agent_ref.agent_name))
            if not remaining:
                return True
    return not remaining


@log_call
def discover_hosts_and_agents(
    mngr_ctx: MngrContext,
    provider_names: tuple[str, ...] | None,
    agent_identifiers: Sequence[str] | None,
    include_destroyed: bool,
    reset_caches: bool,
) -> tuple[dict[DiscoveredHost, list[DiscoveredAgent]], list[BaseProviderInstance]]:
    """Discover hosts and agents from providers.

    Uses ConcurrencyGroup to query providers in parallel for better performance.
    Returns lightweight DiscoveredHost/DiscoveredAgent data without connecting to hosts.

    When agent_identifiers is provided and provider_names is None, uses the discovery
    event stream to resolve identifiers to provider names and queries only those providers.
    Falls back to a full scan if the event stream is stale or missing.

    When provider_names is explicitly provided, agent_identifiers is ignored (the caller
    already knows which providers to query).
    """
    with log_span("Discovering hosts and agents from providers"):
        # When the caller already specified providers, or no identifiers given,
        # or safe mode is enabled, skip the optimization
        if provider_names is not None or agent_identifiers is None or mngr_ctx.is_full_discovery:
            return _run_discovery(mngr_ctx, provider_names, include_destroyed, reset_caches)

        # Try to resolve identifiers to provider names from the event stream
        resolved_providers = resolve_provider_names_for_identifiers(mngr_ctx.config, agent_identifiers)
        if resolved_providers is None:
            logger.trace("Could not resolve agent identifiers from event stream, doing full scan")
            return _run_discovery(mngr_ctx, None, include_destroyed, reset_caches)

        logger.trace(
            "Resolved agent identifiers to providers: {}",
            resolved_providers,
        )

        # Run discovery with only the resolved providers
        agents_by_host, providers = _run_discovery(mngr_ctx, resolved_providers, include_destroyed, reset_caches)

        # Verify all identifiers were found; if not, the event stream was stale
        if _all_identifiers_found(agent_identifiers, agents_by_host):
            return agents_by_host, providers

        # Fall back to a full scan, reusing the already-created provider instances
        # to avoid leaking resources (each new provider instance holds gRPC connections).
        logger.debug("Event stream was stale (not all identifiers found), falling back to full scan")
        all_providers = get_all_provider_instances(mngr_ctx, None)
        return _run_discovery(mngr_ctx, None, include_destroyed, reset_caches, existing_providers=all_providers)
