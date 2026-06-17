from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import assert_never

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_call
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.pure import pure
from imbue.mngr.api.discover import discover_by_address
from imbue.mngr.api.discover import discover_hosts_and_agents
from imbue.mngr.api.providers import get_local_host
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import AgentNotFoundError
from imbue.mngr.errors import AgentStateInconsistencyError
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.host import Host
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import HostLocation
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentNameOrId
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostAddress
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostLocationAddress
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostNameOrId
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.base_provider import BaseProviderInstance
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME


@pure
def determine_resolved_path(
    parsed_path: Path | None,
    resolved_agent: DiscoveredAgent | None,
    agent_work_dir_if_available: Path | None,
) -> Path:
    """Determine the final path from parsed components.

    Pure function that determines which path to use based on what's available.
    Raises UserInputError if path cannot be determined.

    A *relative* path supplied alongside an agent reference is resolved against
    that agent's work_dir: ``agent:foo/bar`` means ``foo/bar`` inside the
    agent's worktree, not ``foo/bar`` relative to wherever the command happens
    to be running. An *absolute* path is honored verbatim. When no path is given
    at all, the agent's work_dir itself is used.
    """
    if parsed_path is not None:
        if not parsed_path.is_absolute() and resolved_agent is not None and agent_work_dir_if_available is not None:
            return agent_work_dir_if_available / parsed_path
        return parsed_path
    if resolved_agent is not None and agent_work_dir_if_available is not None:
        return agent_work_dir_if_available
    if resolved_agent is not None:
        raise UserInputError(f"Could not find agent {resolved_agent.agent_id} on host")
    raise UserInputError("Must specify a path if no agent is specified")


@pure
def _find_matching_hosts(
    host: HostNameOrId | None,
    provider: ProviderInstanceName | None,
    all_hosts: Sequence[DiscoveredHost],
) -> list[DiscoveredHost]:
    """Find hosts whose name/ID and provider match the given filter components.

    Either component may be ``None`` to skip that filter; if both are ``None``,
    returns ``all_hosts`` unchanged. Matching by ``host`` dispatches on its
    runtime type: a :class:`HostId` matches by ID, a :class:`HostName` by name.
    """
    if host is None and provider is None:
        return list(all_hosts)

    matches = list(all_hosts)
    if host is not None:
        if isinstance(host, HostId):
            matches = [h for h in matches if h.host_id == host]
        else:
            matches = [h for h in matches if h.host_name == host]
    if provider is not None:
        matches = [h for h in matches if h.provider_name == provider]
    return matches


@pure
def filter_all_hosts(
    address: HostAddress,
    all_hosts: Sequence[DiscoveredHost],
) -> list[DiscoveredHost]:
    """Find all hosts matching a :class:`HostAddress` filter."""
    return _find_matching_hosts(address.host, address.provider, all_hosts)


@pure
def filter_one_host(
    address: HostAddress,
    all_hosts: Sequence[DiscoveredHost],
) -> DiscoveredHost:
    """Find the single host matching a :class:`HostAddress` filter.

    Raises :class:`UserInputError` when no host matches or when more than one
    matches.
    """
    matches = filter_all_hosts(address, all_hosts)
    if len(matches) == 0:
        raise UserInputError(f"Could not find host with ID or name: {address}")
    if len(matches) > 1:
        raise UserInputError(f"Multiple hosts found with name: {address}")
    return matches[0]


@pure
def _filter_all_agents(
    agent: AgentNameOrId,
    agents_by_host: Mapping[DiscoveredHost, Sequence[DiscoveredAgent]],
    resolved_host: DiscoveredHost | None = None,
) -> list[tuple[DiscoveredHost, DiscoveredAgent]]:
    """Find all agents matching the given identifier (by ID or name)."""
    matches: list[tuple[DiscoveredHost, DiscoveredAgent]] = []
    for host_ref, agent_refs in agents_by_host.items():
        if resolved_host is not None and host_ref.host_id != resolved_host.host_id:
            continue
        for agent_ref in agent_refs:
            is_match = agent_ref.agent_id == agent if isinstance(agent, AgentId) else agent_ref.agent_name == agent
            if is_match:
                matches.append((host_ref, agent_ref))
    return matches


@pure
def filter_one_agent(
    agent: AgentNameOrId,
    resolved_host: DiscoveredHost | None,
    agents_by_host: Mapping[DiscoveredHost, Sequence[DiscoveredAgent]],
) -> tuple[DiscoveredHost, DiscoveredAgent]:
    """Find the single agent matching the given identifier (by ID or name).

    Raises :class:`AgentNotFoundError` when ``agent`` is an :class:`AgentId`
    and no agent has that ID (the ID was supposed to identify a specific
    agent uniquely). Raises :class:`UserInputError` when an :class:`AgentName`
    has no match, or when more than one agent matches. If ``resolved_host``
    is given, only agents on that host are considered.

    The multi-match error lists each matching agent in ``NAME@HOST.PROVIDER``
    form so the user can disambiguate.
    """
    matches = _filter_all_agents(agent, agents_by_host, resolved_host)
    if len(matches) == 0:
        if isinstance(agent, AgentId):
            raise AgentNotFoundError(str(agent))
        raise UserInputError(f"Could not find agent with ID or name: {agent}")
    if len(matches) > 1:
        match_lines = "\n".join(
            f"  - {agent_ref.agent_name}@{host_ref.host_name}.{host_ref.provider_name} (ID: {agent_ref.agent_id})"
            for host_ref, agent_ref in matches
        )
        raise UserInputError(
            f"Multiple agents found with name '{agent}':\n{match_lines}\n\n"
            "Disambiguate using NAME@HOST.PROVIDER or use the agent ID directly."
        )
    return matches[0]


class ResolvedHostLocationAddress(FrozenModel):
    """Result of resolving a :class:`HostLocationAddress`, including the discovered agent when available."""

    model_config = {"arbitrary_types_allowed": True}

    location: HostLocation = Field(description="The resolved host and path")
    agent: DiscoveredAgent | None = Field(default=None, description="The resolved agent, if the location named one")


@log_call
def resolve_host_location_address(
    parsed: HostLocationAddress,
    agents_by_host: Mapping[DiscoveredHost, Sequence[DiscoveredAgent]],
    mngr_ctx: MngrContext,
    *,
    is_start_desired: bool = True,
) -> ResolvedHostLocationAddress:
    """Resolve a :class:`HostLocationAddress` to a concrete host, path, and optional agent.

    Resolves agent/host references against the discovered hosts and agents.
    If the resolved host is offline, it will be started if ``is_start_desired``
    is True (the default); otherwise raises :class:`UserInputError`.
    """
    logger.trace(
        "Resolving hosted location: agent={} host={} path={}",
        parsed.agent,
        parsed.host,
        parsed.path,
    )

    all_hosts = list(agents_by_host.keys())
    resolved_host: DiscoveredHost | None
    if parsed.host is None:
        resolved_host = None
    else:
        with log_span("Resolving host reference"):
            resolved_host = filter_one_host(parsed.host, all_hosts)

    resolved_agent: DiscoveredAgent | None = None
    if parsed.agent is not None:
        with log_span("Resolving agent reference"):
            resolved_host, resolved_agent = filter_one_agent(parsed.agent, resolved_host, agents_by_host)

    with log_span("Getting host interface from provider"):
        if resolved_host is None:
            provider = get_provider_instance(ProviderInstanceName(LOCAL_PROVIDER_NAME), mngr_ctx)
            host_interface = provider.get_host(HostName(LOCAL_HOST_NAME))
        else:
            provider = get_provider_instance(resolved_host.provider_name, mngr_ctx)
            host_interface = provider.get_host(resolved_host.host_id)

    if not isinstance(host_interface, OnlineHostInterface):
        online_host, _was_started = ensure_host_started(
            host_interface, is_start_desired=is_start_desired, provider=provider
        )
    else:
        online_host = host_interface

    agent_work_dir: Path | None = None
    if resolved_agent is not None:
        for agent_ref in online_host.discover_agents():
            if agent_ref.agent_id == resolved_agent.agent_id:
                agent_work_dir = agent_ref.work_dir
                break

    resolved_path = determine_resolved_path(
        parsed_path=parsed.path,
        resolved_agent=resolved_agent,
        agent_work_dir_if_available=agent_work_dir,
    )

    return ResolvedHostLocationAddress(
        location=HostLocation(host=online_host, path=resolved_path),
        agent=resolved_agent,
    )


def resolve_host_location(
    parsed: HostLocationAddress,
    mngr_ctx: MngrContext,
    *,
    is_start_desired: bool = True,
) -> ResolvedHostLocationAddress:
    """Resolve a :class:`HostLocationAddress` to a host, path, and optional agent.

    Convenience wrapper that drives discovery on the caller's behalf:

    - If ``parsed`` has no agent and no host, the path is returned with the
      local host (no discovery is performed, so unrelated providers like
      Docker or Modal are not touched).
    - Otherwise, performs discovery narrowed to the address's provider (when
      pinned) and delegates to :func:`resolve_host_location_address`.

    Callers that need to drive discovery themselves (e.g. ``mngr create``,
    which caches a single discovery result across multiple address
    resolutions) should call :func:`resolve_host_location_address` directly.

    Raises :class:`UserInputError` if ``parsed`` has no path, no agent, and
    no host.
    """
    if parsed.agent is None and parsed.host is None:
        if parsed.path is None:
            raise UserInputError("Location must include an agent, a host, or a path")
        return ResolvedHostLocationAddress(location=HostLocation(host=get_local_host(mngr_ctx), path=parsed.path))

    # Narrow discovery to the address's provider (when pinned) and feed the
    # event-stream optimization with the agent name/ID (when present).
    provider_names: tuple[str, ...] | None = None
    if parsed.host is not None and parsed.host.provider is not None:
        provider_names = (str(parsed.host.provider),)
    agent_identifiers: tuple[str, ...] | None = None
    if parsed.agent is not None:
        agent_identifiers = (str(parsed.agent),)
    agents_by_host, _providers = discover_hosts_and_agents(
        mngr_ctx,
        provider_names=provider_names,
        agent_identifiers=agent_identifiers,
        include_destroyed=False,
        reset_caches=False,
    )
    return resolve_host_location_address(
        parsed,
        agents_by_host,
        mngr_ctx,
        is_start_desired=is_start_desired,
    )


@pure
def get_host_from_list_by_id(host_id: HostId, all_hosts: Sequence[DiscoveredHost]) -> DiscoveredHost | None:
    for host in all_hosts:
        if host.host_id == host_id:
            return host
    return None


@pure
def get_unique_host_from_list_by_name(
    host_name: HostName, all_hosts: Sequence[DiscoveredHost]
) -> DiscoveredHost | None:
    matching_hosts = [host for host in all_hosts if host.host_name == host_name]
    if len(matching_hosts) == 1:
        return matching_hosts[0]
    elif len(matching_hosts) > 1:
        raise UserInputError(f"Multiple hosts found with name: {host_name}")
    else:
        return None


def ensure_host_started(
    host: HostInterface, is_start_desired: bool, provider: BaseProviderInstance
) -> tuple[Host, bool]:
    """Ensure the host is online and started.

    If the host is already online, returns it directly.
    If offline and start is desired, starts the host and returns the online host.
    If offline and start is not desired, raises UserInputError.

    Also returns a boolean indicating whether the host was started.
    """
    match host:
        case Host() as online_host:
            return online_host, False
        case HostInterface() as offline_host:
            if is_start_desired:
                logger.info("Host is offline, starting it...", host_id=offline_host.id, provider=provider.name)
                started_host = provider.start_host(offline_host)
                return started_host, True
            else:
                raise UserInputError(
                    f"Host '{offline_host.id}' is offline and automatic starting is disabled. "
                    "Enable automatic host starting to proceed."
                )
        case _ as unreachable:
            assert_never(unreachable)


def ensure_agent_started(agent: AgentInterface, host: OnlineHostInterface, is_start_desired: bool) -> None:
    """Ensure an agent is started, starting it if needed and desired.

    If the agent is stopped and is_start_desired is True, starts the agent.
    If the agent is stopped and is_start_desired is False, raises UserInputError.
    """
    lifecycle_state = agent.get_lifecycle_state()
    if lifecycle_state not in (
        AgentLifecycleState.RUNNING,
        AgentLifecycleState.REPLACED,
        AgentLifecycleState.RUNNING_UNKNOWN_AGENT_TYPE,
        AgentLifecycleState.WAITING,
    ):
        if is_start_desired:
            logger.info("Agent {} is stopped, starting it", agent.name)
            agent.wait_for_ready_signal(
                is_creating=False,
                start_action=lambda: host.start_agents([agent.id]),
                timeout=agent.get_ready_timeout_seconds(),
            )
        else:
            raise UserInputError(
                f"Agent '{agent.name}' is stopped and automatic starting is disabled. "
                "Enable automatic agent starting to proceed."
            )


class AgentMatch(FrozenModel):
    """Information about an agent that matched a search query."""

    agent_id: AgentId = Field(description="Unique identifier for the matched agent")
    agent_name: AgentName = Field(description="Human-readable name of the matched agent")
    host_id: HostId = Field(description="Unique identifier for the host the agent runs on")
    host_name: HostName = Field(description="Human-readable name of the host the agent runs on")
    provider_name: ProviderInstanceName = Field(description="Name of the provider instance that owns the host")


def _find_agents_by_identifiers_or_state(
    agent_identifiers: Sequence[AgentNameOrId],
    filter_all: bool,
    target_state: AgentLifecycleState | None,
    mngr_ctx: MngrContext,
    include_destroyed: bool = False,
    provider_names: tuple[str, ...] | None = None,
) -> list[AgentMatch]:
    """Find agents matching identifiers or a target lifecycle state.

    When filter_all is True, returns all agents in the target_state
    (or all agents if target_state is None).
    When filter_all is False, returns agents matching the given identifiers.

    When provider_names is set, only those providers are queried during discovery.

    Raises AgentNotFoundError if any identifier does not match an agent.
    """
    agents_by_host, _ = discover_hosts_and_agents(
        mngr_ctx,
        provider_names=provider_names,
        agent_identifiers=tuple(str(i) for i in agent_identifiers) if not filter_all and agent_identifiers else None,
        include_destroyed=include_destroyed,
        reset_caches=False,
    )

    candidates: list[AgentMatch] = []
    matched_identifiers: set[AgentNameOrId] = set()

    for host_ref, agent_refs in agents_by_host.items():
        for agent_ref in agent_refs:
            should_include: bool
            if filter_all:
                should_include = True
            elif agent_identifiers:
                should_include = False
                for identifier in agent_identifiers:
                    is_match = (
                        agent_ref.agent_id == identifier
                        if isinstance(identifier, AgentId)
                        else agent_ref.agent_name == identifier
                    )
                    if is_match:
                        should_include = True
                        matched_identifiers.add(identifier)
            else:
                should_include = False

            if should_include:
                candidates.append(
                    AgentMatch(
                        agent_id=agent_ref.agent_id,
                        agent_name=agent_ref.agent_name,
                        host_id=host_ref.host_id,
                        host_name=host_ref.host_name,
                        provider_name=host_ref.provider_name,
                    )
                )

    if agent_identifiers:
        unmatched_identifiers = set(agent_identifiers) - matched_identifiers
        if unmatched_identifiers:
            unmatched_list = ", ".join(sorted(str(i) for i in unmatched_identifiers))
            raise AgentNotFoundError(f"No agent(s) found matching: {unmatched_list}")

    if not filter_all or target_state is None:
        return candidates

    matches: list[AgentMatch] = []
    candidates_by_host = group_agents_by_host(candidates)
    for host_key, agent_list in candidates_by_host.items():
        host_id_str, _ = host_key.split(":", 1)
        provider_name = agent_list[0].provider_name
        provider = get_provider_instance(provider_name, mngr_ctx)
        host = provider.get_host(HostId(host_id_str))
        if not isinstance(host, OnlineHostInterface):
            if target_state == AgentLifecycleState.STOPPED:
                matches.extend(agent_list)
            continue
        agents = host.get_agents()
        for candidate in agent_list:
            for agent in agents:
                if agent.id == candidate.agent_id and agent.get_lifecycle_state() == target_state:
                    matches.append(candidate)
                    break

    return matches


@pure
def group_agents_by_host(agents: Sequence[AgentMatch]) -> dict[str, list[AgentMatch]]:
    """Group a list of AgentMatch objects by their host.

    Returns a dictionary where keys are "{host_id}:{provider_name}" and
    values are lists of AgentMatch objects on that host.
    """
    agents_by_host: dict[str, list[AgentMatch]] = {}
    for match in agents:
        key = f"{match.host_id}:{match.provider_name}"
        if key not in agents_by_host:
            agents_by_host[key] = []
        agents_by_host[key].append(match)
    return agents_by_host


# === Address-driven find ===


@pure
def _address_matches_agent_match(address: AgentAddress, match: AgentMatch) -> bool:
    """Check if an :class:`AgentMatch` satisfies the host/provider constraints of an address."""
    if address.host is None:
        return True
    other = HostAddress(host=match.host_name, provider=match.provider_name)
    return address.host.matches(other)


@pure
def _collect_required_provider_names(
    addresses: Sequence[AgentAddress],
) -> tuple[ProviderInstanceName, ...] | None:
    """Return the set of provider names a discovery call can be restricted to.

    If every address has a provider set, returns the deduped tuple. If any
    address omits the provider, returns ``None`` (meaning: all providers must
    be queried).
    """
    providers: set[ProviderInstanceName] = set()
    for addr in addresses:
        if addr.host is None or addr.host.provider is None:
            return None
        providers.add(addr.host.provider)
    if not providers:
        return None
    return tuple(sorted(providers))


def find_all_agents(
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
    provider_filter = _collect_required_provider_names(addresses)
    provider_names = tuple(str(p) for p in provider_filter) if provider_filter is not None else None

    matches = _find_agents_by_identifiers_or_state(
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


def find_one_agent_and_agents_by_host(
    address: AgentAddress,
    mngr_ctx: MngrContext,
) -> tuple[DiscoveredHost, DiscoveredAgent, Mapping[DiscoveredHost, Sequence[DiscoveredAgent]]]:
    """Find an agent by :class:`AgentAddress` and return its refs plus the full discovery result.

    Performs discovery (skipping irrelevant providers) and matches the
    address's agent identifier against the discovered agents (filtered by
    the address's host constraint if any). Returns the matching refs and
    the unfiltered ``agents_by_host`` mapping so callers that need the
    whole discovery result (for example to check name conflicts across
    other agents) can reuse it instead of running discovery a second time.

    Returns only metadata. Callers that need a live ``AgentInterface`` or
    ``OnlineHostInterface`` should compose with
    :func:`resolve_to_started_host_and_agent` or
    :func:`resolve_to_started_host_and_running_agent`.

    Raises :class:`UserInputError` if the host constraint matches no hosts.
    Raises :class:`AgentNotFoundError` / :class:`UserInputError` if the
    agent cannot be resolved (see :func:`filter_one_agent`).
    """
    agents_by_host, _providers = discover_by_address(address, mngr_ctx, include_destroyed=False)
    if not agents_by_host and address.host is not None:
        raise UserInputError(f"No hosts found matching {address.host}")

    host_ref, agent_ref = filter_one_agent(address.agent, resolved_host=None, agents_by_host=agents_by_host)
    return host_ref, agent_ref, agents_by_host


def find_one_agent(
    address: AgentAddress,
    mngr_ctx: MngrContext,
) -> tuple[DiscoveredHost, DiscoveredAgent]:
    """Find an agent by :class:`AgentAddress` and return its discovery refs.

    Thin wrapper around :func:`find_one_agent_and_agents_by_host` that
    drops the full discovery mapping. See that function for the contract
    and error behaviour.
    """
    host_ref, agent_ref, _ = find_one_agent_and_agents_by_host(address, mngr_ctx)
    return host_ref, agent_ref


def resolve_to_started_host_and_agent(
    host_ref: DiscoveredHost,
    agent_ref: DiscoveredAgent,
    allow_auto_start: bool,
    mngr_ctx: MngrContext,
) -> tuple[AgentInterface, OnlineHostInterface]:
    """Resolve discovery refs to a live ``(AgentInterface, OnlineHostInterface)``.

    Composes :func:`ensure_host_started` with the metadata-to-live lookup
    step: brings the host online (auto-starting it iff ``allow_auto_start``
    is True), then locates ``agent_ref`` on the live host. The agent's
    lifecycle state is *not* checked -- the returned ``AgentInterface``
    may represent a stopped agent. Callers that need the agent process to
    be running should use :func:`resolve_to_started_host_and_running_agent`
    instead.

    Raises :class:`UserInputError` when the host is offline and
    ``allow_auto_start`` is False. Raises :class:`AgentStateInconsistencyError`
    if the agent was found during discovery but is missing on the live host (a
    stale-cache / host state inconsistency case).
    """
    provider = get_provider_instance(host_ref.provider_name, mngr_ctx)
    host = provider.get_host(host_ref.host_id)
    online_host, _was_started = ensure_host_started(host, is_start_desired=allow_auto_start, provider=provider)
    for live_agent in online_host.get_agents():
        if live_agent.id == agent_ref.agent_id:
            return live_agent, online_host
    raise AgentStateInconsistencyError(
        f"Agent '{agent_ref.agent_name}' (ID: {agent_ref.agent_id}) was found during discovery but is "
        f"no longer present on host {host_ref.host_name}.{host_ref.provider_name}. "
        "This indicates a stale discovery cache or host state inconsistency."
    )


def resolve_to_started_host_and_running_agent(
    host_ref: DiscoveredHost,
    agent_ref: DiscoveredAgent,
    allow_auto_start: bool,
    mngr_ctx: MngrContext,
) -> tuple[AgentInterface, OnlineHostInterface]:
    """Resolve discovery refs to live interfaces, requiring the agent to be running.

    Same as :func:`resolve_to_started_host_and_agent` but additionally
    ensures the agent process is running (auto-starting it iff
    ``allow_auto_start`` is True) via :func:`ensure_agent_started`.

    Raises :class:`UserInputError` when the host is offline or the agent
    is stopped and ``allow_auto_start`` is False. Raises
    :class:`RuntimeError` if the agent was found during discovery but is
    missing on the live host.
    """
    agent, online_host = resolve_to_started_host_and_agent(host_ref, agent_ref, allow_auto_start, mngr_ctx)
    ensure_agent_started(agent, online_host, is_start_desired=allow_auto_start)
    return agent, online_host
