import concurrent.futures
import time
from collections.abc import Mapping
from collections.abc import Sequence
from concurrent.futures import Future
from threading import Lock
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_call
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from imbue.mngr.api.discovery_events import resolve_provider_names_for_identifiers
from imbue.mngr.api.providers import get_all_provider_instances_and_skipped
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import ProviderDiscoveryError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostAddress
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.base_provider import BaseProviderInstance
from imbue.mngr.utils.thread_cleanup import mngr_executor

# A full discovery snapshot waits for every provider in parallel, so a single
# provider whose call blocks freezes the whole snapshot. These thresholds make
# that diagnosable: warn (by default, at WARNING) when one provider's discovery
# is unusually slow, and -- while still pending -- name the provider(s) we are
# blocked on so a hang is visible by name instead of only inferable from silence.
_SLOW_PROVIDER_DISCOVERY_WARN_SECONDS: Final[float] = 10.0
_PENDING_PROVIDER_WARN_INTERVAL_SECONDS: Final[float] = 15.0


class Unreachable(FrozenModel):
    """Why a provider could not be reached: it failed to construct, so it was never queried.

    Carries the provider's own error and curated remediation, which is what a
    caller that turns this back into a user-facing failure needs (see
    :func:`_raise_for_unmatched_identifiers`).
    """

    provider_name: ProviderInstanceName = Field(description="Name of the provider that could not be reached")
    error_type_name: str = Field(description="The type name of the construction exception")
    error_message: str = Field(description="The construction exception's message")
    user_help_text: str | None = Field(default=None, description="The exception's curated remediation, or None")


class DiscoveryOutcome(FrozenModel):
    """What one discovery pass saw, including the providers it could not reach.

    A provider that could not be reached is absent from ``agents_by_host`` in
    exactly the same way as a provider that genuinely holds no agents, so a
    caller that reads only the hosts cannot tell "this agent does not exist" from
    "the backend hosting it could not be reached". ``results_by_provider`` closes
    that gap.
    """

    agents_by_host: dict[DiscoveredHost, list[DiscoveredAgent]] = Field(
        description="Agents discovered per host, across every provider that answered"
    )
    results_by_provider: dict[ProviderInstanceName, BaseProviderInstance | Unreachable] = Field(
        description=(
            "Each provider mapped to the instance that answered, or to why it could not be reached. A "
            "provider that answered but holds nothing simply contributes no hosts -- that is the empty "
            "case, and it needs no entry of its own."
        )
    )

    @property
    def providers(self) -> list[BaseProviderInstance]:
        """The provider instances that answered (the reachable ones)."""
        return [result for result in self.results_by_provider.values() if not isinstance(result, Unreachable)]

    @property
    def unavailable_providers(self) -> list[Unreachable]:
        """The providers discovery could not reach, each leaving a gap in what it can claim.

        This is why "not found" is not always honest: an agent on an unreachable
        provider is absent from the snapshot exactly as a deleted one is.
        """
        return [result for result in self.results_by_provider.values() if isinstance(result, Unreachable)]


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

    Wraps any provider-side exception in ``ProviderDiscoveryError`` so the
    snapshot/poll error path can attribute the failure to the exact provider
    instance (e.g. ``imbue_cloud_alice@example.com``) without parsing
    messages -- minds surfaces this in its providers panel so the user can
    see which provider failed and Disable it themselves.
    """
    # Time + span each provider's discovery so per-provider latency is visible
    # (the span logs the duration at trace; the slow-warning below surfaces an
    # unusually-slow provider at WARNING by default).
    started_at = time.monotonic()
    with log_span("Discovering hosts and agents from provider {}", provider.name):
        try:
            provider_results = provider.discover_hosts_and_agents(cg=cg, include_destroyed=include_destroyed)
        except ProviderUnavailableError:
            # Re-raise as-is so the broad except below doesn't wrap it.
            raise
        except Exception as exc:
            raise ProviderDiscoveryError(provider.name, exc) from exc
    elapsed_seconds = time.monotonic() - started_at
    if elapsed_seconds > _SLOW_PROVIDER_DISCOVERY_WARN_SECONDS:
        logger.warning("Provider {} discovery was slow: took {:.1f}s", provider.name, elapsed_seconds)

    # Merge results into the main dict under lock
    with results_lock:
        agents_by_host.update(provider_results)


def _wait_for_provider_discovery(provider_name_by_future: Mapping["Future[None]", str]) -> None:
    """Wait for all provider-discovery futures, naming any that are still pending.

    A snapshot waits for every provider, so one provider whose call blocks freezes
    the whole snapshot. To make that diagnosable, this logs a warning naming the
    still-pending provider(s) every ``_PENDING_PROVIDER_WARN_INTERVAL_SECONDS``
    until they all finish. It does not abort -- bounding a hung provider's call is
    a separate concern; this only restores visibility into *which* provider is the
    one we are blocked on.
    """
    pending = set(provider_name_by_future)
    started_at = time.monotonic()
    while pending:
        _done, pending = concurrent.futures.wait(pending, timeout=_PENDING_PROVIDER_WARN_INTERVAL_SECONDS)
        if pending:
            pending_provider_names = sorted(provider_name_by_future[future] for future in pending)
            logger.warning(
                "Discovery still waiting on {} provider(s) after {:.0f}s: {}",
                len(pending),
                time.monotonic() - started_at,
                ", ".join(pending_provider_names),
            )


def _run_discovery(
    mngr_ctx: MngrContext,
    provider_names: tuple[str, ...] | None,
    include_destroyed: bool,
    reset_caches: bool,
) -> DiscoveryOutcome:
    """Run the actual discovery against providers. Shared implementation for discover_hosts_and_agents."""
    agents_by_host: dict[DiscoveredHost, list[DiscoveredAgent]] = {}
    results_lock = Lock()

    providers, skipped_providers = get_all_provider_instances_and_skipped(mngr_ctx, provider_names)
    logger.trace("Found {} provider instances ({} skipped)", len(providers), len(skipped_providers))
    # One entry per provider: the instance that answered, or why it could not be
    # reached. An unreachable provider is queried by nobody, so it contributes no
    # hosts and no agents -- indistinguishable, from the hosts alone, from a
    # provider that holds none. Recording why (and logging it) is what lets a
    # caller resolving an identifier tell the two apart. An empty provider
    # answered and holds nothing, so it is simply left out.
    results_by_provider: dict[ProviderInstanceName, BaseProviderInstance | Unreachable] = {
        provider.name: provider for provider in providers
    }
    for skipped in skipped_providers:
        if skipped.is_empty:
            continue
        results_by_provider[skipped.provider_name] = Unreachable(
            provider_name=skipped.provider_name,
            error_type_name=skipped.error_type_name,
            error_message=skipped.error_message,
            user_help_text=skipped.user_help_text,
        )
        logger.warning(
            "Discovery could not reach provider {}, so its hosts and agents are absent from this snapshot: {}",
            skipped.provider_name,
            skipped.error_message,
        )

    if reset_caches:
        logger.debug("Resetting provider caches before discovery")
        for provider in providers:
            provider.reset_caches()

    # Process all providers in parallel using mngr_executor. Track which provider
    # each future belongs to so a provider that hangs -- which freezes the whole
    # snapshot, since we wait for all of them -- can be named in the logs while it
    # is still pending, rather than only inferred later from its silence.
    provider_name_by_future: dict[Future[None], str] = {}
    with mngr_executor(
        parent_cg=mngr_ctx.concurrency_group, name="discover_hosts_and_agents", max_workers=32
    ) as executor:
        for provider in providers:
            future = executor.submit(
                _discover_provider_hosts_and_agents,
                provider,
                agents_by_host,
                include_destroyed,
                results_lock,
                mngr_ctx.concurrency_group,
            )
            provider_name_by_future[future] = provider.name
        _wait_for_provider_discovery(provider_name_by_future)

    # Re-raise any thread exceptions (the wait above only logs; results are read here).
    for future in provider_name_by_future:
        future.result()

    # Warn if any host names are duplicated within the same provider
    warn_on_duplicate_host_names(agents_by_host)

    return DiscoveryOutcome(agents_by_host=agents_by_host, results_by_provider=results_by_provider)


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
) -> DiscoveryOutcome:
    """Discover hosts and agents from providers.

    Uses ConcurrencyGroup to query providers in parallel for better performance.
    Returns lightweight DiscoveredHost/DiscoveredAgent data without connecting to hosts,
    alongside the providers that could not be constructed -- see
    :class:`DiscoveryOutcome` for why a caller resolving an identifier needs those.

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
        resolved_providers = resolve_provider_names_for_identifiers(mngr_ctx, agent_identifiers)
        if resolved_providers is None:
            logger.trace("Could not resolve agent identifiers from event stream, doing full scan")
            return _run_discovery(mngr_ctx, None, include_destroyed, reset_caches)

        logger.trace(
            "Resolved agent identifiers to providers: {}",
            resolved_providers,
        )

        # Run discovery with only the resolved providers
        outcome = _run_discovery(mngr_ctx, resolved_providers, include_destroyed, reset_caches)

        # Verify all identifiers were found; if not, the event stream was stale
        if _all_identifiers_found(agent_identifiers, outcome.agents_by_host):
            return outcome

        # Fall back to a full scan. Provider instances are cached so this
        # does not leak resources even though get_all_provider_instances is called again.
        logger.debug("Event stream was stale (not all identifiers found), falling back to full scan")
        return _run_discovery(mngr_ctx, None, include_destroyed, reset_caches)


def discover_by_address(
    address: AgentAddress,
    mngr_ctx: MngrContext,
    include_destroyed: bool = False,
    reset_caches: bool = False,
) -> DiscoveryOutcome:
    """Discover hosts and agents scoped by a single :class:`AgentAddress`.

    The address's provider (if any) narrows discovery so we skip irrelevant
    providers; the agent name/ID feeds the discovery event-stream
    optimization. After discovery, results are filtered by the address's full
    host/provider constraint.
    """
    provider_names: tuple[str, ...] | None = None
    if address.host is not None and address.host.provider is not None:
        provider_names = (str(address.host.provider),)

    outcome = discover_hosts_and_agents(
        mngr_ctx,
        provider_names=provider_names,
        agent_identifiers=(str(address.agent),),
        include_destroyed=include_destroyed,
        reset_caches=reset_caches,
    )

    if address.host is None:
        return outcome

    constraint: HostAddress = address.host
    filtered = {
        host_ref: agent_refs
        for host_ref, agent_refs in outcome.agents_by_host.items()
        if constraint.matches_host(host_ref.host_id, host_ref.host_name, host_ref.provider_name)
    }
    return outcome.model_copy_update(to_update(outcome.field_ref().agents_by_host, filtered))
