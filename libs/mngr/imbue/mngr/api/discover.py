from concurrent.futures import Future
from threading import Lock

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.executor import ConcurrencyGroupExecutor
from imbue.imbue_common.logging import log_call
from imbue.imbue_common.logging import log_span
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

    This function is run in a thread by discover_all_hosts_and_agents.
    Results are merged into the shared agents_by_host dict under the results_lock.
    """
    provider_results = provider.discover_hosts_and_agents(cg=cg, include_destroyed=include_destroyed)

    # Merge results into the main dict under lock
    with results_lock:
        agents_by_host.update(provider_results)


@log_call
def discover_all_hosts_and_agents(
    mngr_ctx: MngrContext,
    provider_names: tuple[str, ...] | None = None,
    include_destroyed: bool = False,
    reset_caches: bool = False,
) -> tuple[dict[DiscoveredHost, list[DiscoveredAgent]], list[BaseProviderInstance]]:
    """Discover all hosts and agents from all providers.

    Uses ConcurrencyGroup to query providers in parallel for better performance.
    Returns lightweight DiscoveredHost/DiscoveredAgent data without connecting to hosts.
    """
    agents_by_host: dict[DiscoveredHost, list[DiscoveredAgent]] = {}
    results_lock = Lock()

    with log_span("Discovering all hosts and agents from all providers"):
        providers = get_all_provider_instances(mngr_ctx, provider_names)
        logger.trace("Found {} provider instances", len(providers))

        if reset_caches:
            logger.debug("Resetting provider caches before discovery")
            for provider in providers:
                provider.reset_caches()

        # Process all providers in parallel using ConcurrencyGroupExecutor
        futures: list[Future[None]] = []
        with ConcurrencyGroupExecutor(
            parent_cg=mngr_ctx.concurrency_group, name="discover_all_hosts_and_agents", max_workers=32
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
