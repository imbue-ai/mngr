"""Address-aware orchestration: discovery + lookup driven by typed addresses.

This module sits one layer above :mod:`imbue.mngr.api.find`: it accepts already-
parsed :class:`AgentAddress` values, runs discovery (optionally narrowed to a
single provider when the address pins one), and resolves the result down to a
concrete agent + host pair.

CLI commands should never construct an :class:`AgentAddress` from a raw string
themselves -- use the Click ParamTypes in :mod:`imbue.mngr.cli.address_params`,
which feeds typed addresses straight into command bodies.
"""

from collections.abc import Mapping
from collections.abc import Sequence

from imbue.imbue_common.pure import pure
from imbue.mngr.api.addresses import AgentAddress
from imbue.mngr.api.addresses import HostAddress
from imbue.mngr.api.addresses import collect_required_provider_names
from imbue.mngr.api.addresses import host_addresses_match
from imbue.mngr.api.discover import discover_hosts_and_agents
from imbue.mngr.api.find import AgentMatch
from imbue.mngr.api.find import find_agents_by_identifiers_or_state
from imbue.mngr.api.find import find_and_maybe_start_agent
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import AgentNotFoundError
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.providers.base_provider import BaseProviderInstance


@pure
def _address_matches_host(address: AgentAddress, host_ref: DiscoveredHost) -> bool:
    """Check if a discovered host satisfies the host/provider constraints of an address."""
    if address.host is None:
        return True
    other = HostAddress(host=host_ref.host_name, provider=host_ref.provider_name)
    return host_addresses_match(address.host, other)


@pure
def _address_matches_agent_match(address: AgentAddress, match: AgentMatch) -> bool:
    """Check if an :class:`AgentMatch` satisfies the host/provider constraints of an address."""
    if address.host is None:
        return True
    other = HostAddress(host=match.host_name, provider=match.provider_name)
    return host_addresses_match(address.host, other)


@pure
def filter_agents_by_host_constraint(
    agents_by_host: Mapping[DiscoveredHost, Sequence[DiscoveredAgent]],
    address: AgentAddress,
) -> dict[DiscoveredHost, Sequence[DiscoveredAgent]]:
    """Filter agents_by_host to only include hosts matching the address's host constraint.

    If the address has no host component, returns the original mapping unchanged.
    """
    if address.host is None:
        return dict(agents_by_host)

    return {
        host_ref: agent_refs
        for host_ref, agent_refs in agents_by_host.items()
        if _address_matches_host(address, host_ref)
    }


def discover_by_address(
    address: AgentAddress,
    mngr_ctx: MngrContext,
    include_destroyed: bool = False,
    reset_caches: bool = False,
) -> tuple[dict[DiscoveredHost, Sequence[DiscoveredAgent]], list[BaseProviderInstance]]:
    """Discover hosts and agents scoped by a single :class:`AgentAddress`.

    The address's provider (if any) narrows discovery so we skip irrelevant
    providers; the agent name/ID feeds the discovery event-stream
    optimization. After discovery, results are filtered by the address's full
    host/provider constraint.
    """
    provider_names: tuple[str, ...] | None = None
    if address.host is not None and address.host.provider is not None:
        provider_names = (str(address.host.provider),)

    agents_by_host, providers = discover_hosts_and_agents(
        mngr_ctx,
        provider_names=provider_names,
        agent_identifiers=(str(address.agent),),
        include_destroyed=include_destroyed,
        reset_caches=reset_caches,
    )

    filtered = filter_agents_by_host_constraint(agents_by_host, address)
    return filtered, providers


def find_agents_by_addresses(
    addresses: Sequence[AgentAddress],
    filter_all: bool,
    target_state: AgentLifecycleState | None,
    mngr_ctx: MngrContext,
    include_destroyed: bool = False,
) -> list[AgentMatch]:
    """Find agents matching a sequence of :class:`AgentAddress` constraints.

    When all addresses pin a provider, only those providers are queried during
    discovery. Identifiers without host/provider components match by name/ID
    alone; identifiers with host/provider components are post-filtered to
    keep only matches on a satisfying host.
    """
    agent_identifiers = [addr.agent for addr in addresses]
    provider_filter = collect_required_provider_names(addresses)
    provider_names = tuple(str(p) for p in provider_filter) if provider_filter is not None else None

    matches = find_agents_by_identifiers_or_state(
        agent_identifiers=agent_identifiers,
        filter_all=filter_all,
        target_state=target_state,
        mngr_ctx=mngr_ctx,
        include_destroyed=include_destroyed,
        provider_names=provider_names,
    )

    return _post_filter_matches_by_addresses(addresses, matches)


@pure
def _post_filter_matches_by_addresses(
    addresses: Sequence[AgentAddress],
    matches: Sequence[AgentMatch],
) -> list[AgentMatch]:
    """Post-filter agent matches by the host/provider constraints of each address.

    For addresses without host/provider components, matches pass through
    unchanged. For constrained addresses, only matches on a satisfying host are
    kept. Raises :class:`AgentNotFoundError` if a constrained address has no
    matching agents after filtering.
    """
    has_host_constraints = any(addr.host is not None for addr in addresses)
    if not has_host_constraints:
        return list(matches)

    # Group host-constrained addresses by their agent (str) for matching.
    addresses_by_agent: dict[str, list[AgentAddress]] = {}
    for addr in addresses:
        if addr.host is not None:
            addresses_by_agent.setdefault(str(addr.agent), []).append(addr)

    filtered: list[AgentMatch] = []
    for match in matches:
        agent_name_str = str(match.agent_name)
        agent_id_str = str(match.agent_id)

        # Address agents may be either AgentName or AgentId; check both.
        constraints = addresses_by_agent.get(agent_name_str) or addresses_by_agent.get(agent_id_str)
        if constraints is None or any(_address_matches_agent_match(addr, match) for addr in constraints):
            filtered.append(match)

    for addr in addresses:
        if addr.host is None:
            continue
        agent_str = str(addr.agent)
        has_match = any(str(m.agent_name) == agent_str or str(m.agent_id) == agent_str for m in filtered)
        if not has_match:
            raise AgentNotFoundError(f"No agent found matching address: {addr}")

    return filtered


def find_agent_by_address(
    address: AgentAddress,
    mngr_ctx: MngrContext,
    command_name: str,
    is_start_desired: bool = False,
    skip_agent_state_check: bool = False,
) -> tuple[AgentInterface, OnlineHostInterface]:
    """Find an agent by :class:`AgentAddress`, supporting host/provider disambiguation.

    Handles the full flow: runs discovery (skipping irrelevant providers),
    filters by the address's host constraint, and resolves to an agent+host
    pair. Raises :class:`UserInputError` if the host constraint matches no
    hosts.
    """
    agents_by_host, _providers = discover_by_address(address, mngr_ctx, include_destroyed=False)

    if not agents_by_host and address.host is not None:
        raise UserInputError(f"No hosts found matching {address.host}")

    return find_and_maybe_start_agent(
        address.agent,
        agents_by_host,
        mngr_ctx,
        command_name,
        is_start_desired=is_start_desired,
        skip_agent_state_check=skip_agent_state_check,
    )
