"""Cheap, single-target lookups for one agent or host.

Where ``api.list`` enumerates every provider and then filters, the helpers here
resolve a single :class:`AgentAddress` / :class:`HostAddress` (provider-narrowed
discovery) and then fetch only that target. Two fetch tiers share one resolution:

- :func:`poll_combined_state` -- the cheap tier: just the agent/host lifecycle
  enums (no plugin field generators). Used by ``mngr wait``'s poll loop and
  ``mngr state --quick``.
- :func:`get_agent_details` / :func:`get_host_details` -- the rich tier: full
  :class:`AgentDetails` / :class:`HostDetails` with the same plugin fields
  ``mngr list`` produces, via ``provider.get_host_and_agent_details``.
"""

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.api.discover import discover_hosts_and_agents
from imbue.mngr.api.find import filter_one_host
from imbue.mngr.api.find import find_one_agent
from imbue.mngr.api.list import build_field_generators
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import AgentStateInconsistencyError
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.data_types import HostDetails
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentOrHostAddress
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostAddress
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.providers.base_provider import BaseProviderInstance


class CombinedState(FrozenModel):
    """The lifecycle state of a single target at a point in time."""

    host_state: HostState | None = Field(default=None, description="Current host state (None if host is unreachable)")
    agent_state: AgentLifecycleState | None = Field(
        default=None, description="Current agent lifecycle state (None if not an agent target or unreachable)"
    )


class ResolvedTarget(FrozenModel):
    """A single agent or host resolved to its provider and ids, ready to poll.

    Carries only what the cheap :func:`poll_combined_state` tier needs (provider
    + ids). The rich detail fetchers resolve their own discovery refs, so they do
    not take a ``ResolvedTarget``.
    """

    model_config = {"arbitrary_types_allowed": True}

    identifier: str = Field(description="The original identifier string (ID or name)")
    provider: BaseProviderInstance = Field(description="Provider instance that owns the host")
    host_id: HostId = Field(description="Host ID to poll")
    agent_id: AgentId | None = Field(
        default=None, description="Agent ID to poll, when this is an agent target (None for a host target)"
    )

    @property
    def is_agent_target(self) -> bool:
        return self.agent_id is not None


def resolve_target(address: AgentOrHostAddress, mngr_ctx: MngrContext) -> ResolvedTarget:
    """Resolve an :class:`AgentOrHostAddress` to a :class:`ResolvedTarget`.

    Agent vs host is decided by the address type (no state-based fallback).
    Raises :class:`UserInputError` if the target cannot be found.
    """
    if isinstance(address, AgentAddress):
        host_ref, agent_ref = find_one_agent(address, mngr_ctx)
        provider = get_provider_instance(host_ref.provider_name, mngr_ctx)
        return ResolvedTarget(
            identifier=str(address), provider=provider, host_id=host_ref.host_id, agent_id=agent_ref.agent_id
        )
    host_ref, _ = _discover_one_host(address, mngr_ctx)
    provider = get_provider_instance(host_ref.provider_name, mngr_ctx)
    return ResolvedTarget(identifier=str(address), provider=provider, host_id=host_ref.host_id, agent_id=None)


def _discover_one_host(
    address: HostAddress, mngr_ctx: MngrContext
) -> tuple[DiscoveredHost, dict[DiscoveredHost, list[DiscoveredAgent]]]:
    """Discover the single :class:`DiscoveredHost` matching a host address, with its agents.

    Discovery is narrowed to ``address.provider`` when the address pins one, so a
    fully-qualified ``host.provider`` only queries that provider. Returns the
    resolved host ref plus the full ``agents_by_host`` mapping, so callers that
    also need the host's agent refs avoid a second discovery pass.
    """
    provider_names = (str(address.provider),) if address.provider is not None else None
    agents_by_host, _ = discover_hosts_and_agents(
        mngr_ctx,
        provider_names=provider_names,
        agent_identifiers=None,
        include_destroyed=False,
        reset_caches=False,
    )
    host_ref = filter_one_host(address, list(agents_by_host.keys()))
    return host_ref, agents_by_host


def poll_combined_state(resolved: ResolvedTarget) -> CombinedState:
    """Poll the current lifecycle state of the resolved target (the cheap tier).

    Gets a fresh host interface from the provider and queries state directly.
    Does NOT build full details or run plugin field generators.

    When any operation fails with a :class:`HostConnectionError` (e.g. SSH
    unreachable because the host was destroyed), falls back to the offline host
    representation to determine the provider-level state (DESTROYED, STOPPED, etc.).
    """
    try:
        host_interface = resolved.provider.get_host(resolved.host_id)
        host_state = host_interface.get_state()

        agent_state: AgentLifecycleState | None = None
        if resolved.agent_id is not None:
            if isinstance(host_interface, OnlineHostInterface):
                agent_state = _get_agent_lifecycle_state(host_interface, resolved.agent_id)
            else:
                agent_state = AgentLifecycleState.STOPPED

        return CombinedState(host_state=host_state, agent_state=agent_state)
    except HostConnectionError as exc:
        # Host is unreachable (e.g. destroyed, stopped) -- get state from provider metadata
        logger.debug("Host unreachable, falling back to offline state: {}", exc)
        offline_host = resolved.provider.to_offline_host(resolved.host_id)
        offline_agent_state = AgentLifecycleState.STOPPED if resolved.agent_id is not None else None
        return CombinedState(host_state=offline_host.get_state(), agent_state=offline_agent_state)


def _get_agent_lifecycle_state(
    host: OnlineHostInterface,
    agent_id: AgentId,
) -> AgentLifecycleState:
    """Get the lifecycle state of a specific agent on an online host."""
    for agent in host.get_agents():
        if agent.id == agent_id:
            return agent.get_lifecycle_state()
    # Agent not found on host -- treat as stopped
    logger.warning("Agent {} not found on host {}, treating as STOPPED", agent_id, host.id)
    return AgentLifecycleState.STOPPED


def get_agent_details(address: AgentAddress, mngr_ctx: MngrContext) -> AgentDetails:
    """Fetch full :class:`AgentDetails` for one agent (the rich tier).

    Resolves the address (provider-narrowed) and builds details via
    ``provider.get_host_and_agent_details`` -- the same path ``mngr list`` uses
    per host -- so plugin fields match list exactly. Raises the same errors as
    :func:`imbue.mngr.api.find.find_one_agent` when the agent cannot be resolved.
    """
    host_ref, agent_ref = find_one_agent(address, mngr_ctx)
    provider = get_provider_instance(host_ref.provider_name, mngr_ctx)
    return build_agent_details(provider, host_ref, agent_ref, mngr_ctx)


def build_agent_details(
    provider: BaseProviderInstance,
    host_ref: DiscoveredHost,
    agent_ref: DiscoveredAgent,
    mngr_ctx: MngrContext,
) -> AgentDetails:
    """Build full :class:`AgentDetails` for one already-resolved agent.

    The no-resolution counterpart to :func:`get_agent_details`, for callers that
    already hold the discovery refs (e.g. from a :class:`ResolvedTarget`).
    """
    field_generators, offline_field_generators = build_field_generators(mngr_ctx)
    _host_details, agent_details_list = provider.get_host_and_agent_details(
        host_ref,
        [agent_ref],
        field_generators=field_generators,
        offline_field_generators=offline_field_generators,
    )
    if not agent_details_list:
        raise AgentStateInconsistencyError(
            f"Agent '{agent_ref.agent_name}' (ID: {agent_ref.agent_id}) was resolved during discovery but "
            f"produced no details on host {host_ref.host_name}.{host_ref.provider_name}."
        )
    return agent_details_list[0]


def get_host_details(address: HostAddress, mngr_ctx: MngrContext) -> tuple[HostDetails, tuple[DiscoveredAgent, ...]]:
    """Fetch :class:`HostDetails` plus the discovery refs of the agents on that host.

    Only host-level details are built (cheap). The agents are returned as
    lightweight discovery refs (id/name), not full :class:`AgentDetails`, so a
    busy host does not trigger per-agent detail collection -- which would defeat
    the point of an addressed single-target lookup.
    """
    host_ref, agents_by_host = _discover_one_host(address, mngr_ctx)
    provider = get_provider_instance(host_ref.provider_name, mngr_ctx)
    return build_host_details(provider, host_ref, mngr_ctx), tuple(agents_by_host[host_ref])


def build_host_details(
    provider: BaseProviderInstance,
    host_ref: DiscoveredHost,
    mngr_ctx: MngrContext,
) -> HostDetails:
    """Build only the :class:`HostDetails` for one already-resolved host.

    The no-resolution counterpart to :func:`get_host_details`. Passes no agent
    refs to ``get_host_and_agent_details`` so no per-agent details are built.
    """
    field_generators, offline_field_generators = build_field_generators(mngr_ctx)
    host_details, _ = provider.get_host_and_agent_details(
        host_ref,
        [],
        field_generators=field_generators,
        offline_field_generators=offline_field_generators,
    )
    return host_details
