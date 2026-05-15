from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from typing import assert_never

from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.mngr.api.find import find_one_agent
from imbue.mngr.api.list import list_agents
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.cli.agent_selector import select_agent_interactively
from imbue.mngr.cli.output_helpers import emit_info
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostAddress
from imbue.mngr.primitives import OutputFormat


@pure
def filter_agents_by_host(
    agents_by_host: Mapping[DiscoveredHost, Sequence[DiscoveredAgent]],
    host_filter: HostAddress,
) -> dict[DiscoveredHost, Sequence[DiscoveredAgent]]:
    """Filter agents_by_host to only include hosts matching the given :class:`HostAddress`.

    Raises :class:`UserInputError` if no hosts match the filter.
    """
    filtered = {
        host_ref: agent_refs
        for host_ref, agent_refs in agents_by_host.items()
        if host_filter.matches(HostAddress(host=host_ref.host_name, provider=host_ref.provider_name))
    }
    if not filtered:
        raise UserInputError(f"No host found matching: {host_filter}")
    return filtered


def _ensure_host_online(
    host_ref: DiscoveredHost,
    allow_auto_start: bool,
    mngr_ctx: MngrContext,
) -> OnlineHostInterface:
    """Resolve a :class:`DiscoveredHost` to an :class:`OnlineHostInterface`.

    Starts the host if offline and ``allow_auto_start`` is True; otherwise
    raises :class:`UserInputError` when the host is offline.
    """
    provider = get_provider_instance(host_ref.provider_name, mngr_ctx)
    host = provider.get_host(host_ref.host_id)
    match host:
        case OnlineHostInterface() as online_host:
            return online_host
        case HostInterface() as offline_host:
            if not allow_auto_start:
                raise UserInputError(
                    f"Host '{offline_host.id}' is offline and automatic starting is disabled. "
                    "Pass --start to start the host automatically."
                )
            logger.info("Host is offline, starting it...", host_id=offline_host.id, provider=provider.name)
            return provider.start_host(offline_host)
        case _ as unreachable:
            assert_never(unreachable)


def _resolve_agent_on_host(
    host_ref: DiscoveredHost,
    agent_ref: DiscoveredAgent,
    online_host: OnlineHostInterface,
) -> AgentInterface:
    """Look up the agent that ``agent_ref`` points to on a live host.

    Raises :class:`RuntimeError` if the agent was found during discovery
    but is no longer present on the live host (a stale-cache / host state
    inconsistency case).
    """
    for live_agent in online_host.get_agents():
        if live_agent.id == agent_ref.agent_id:
            return live_agent
    raise RuntimeError(
        f"Agent '{agent_ref.agent_name}' (ID: {agent_ref.agent_id}) was found during discovery but is "
        f"no longer present on host {host_ref.host_name}.{host_ref.provider_name}. "
        "This indicates a stale discovery cache or host state inconsistency."
    )


def ensure_host_started_and_resolve_agent(
    host_ref: DiscoveredHost,
    agent_ref: DiscoveredAgent,
    allow_auto_start: bool,
    mngr_ctx: MngrContext,
) -> tuple[AgentInterface, OnlineHostInterface]:
    """Bring the host online and resolve the agent ref to an :class:`AgentInterface`.

    The agent process's lifecycle state is *not* checked: the returned
    ``AgentInterface`` may represent a stopped agent. Callers that need
    the agent process to be running should use
    :func:`ensure_host_and_agent_started` instead.

    When ``allow_auto_start`` is True, an offline host is started
    automatically. When False, an offline host raises
    :class:`UserInputError`.

    Raises :class:`RuntimeError` if the agent was found during discovery
    but is missing on the live host.
    """
    online_host = _ensure_host_online(host_ref, allow_auto_start, mngr_ctx)
    agent = _resolve_agent_on_host(host_ref, agent_ref, online_host)
    return agent, online_host


def ensure_host_and_agent_started(
    host_ref: DiscoveredHost,
    agent_ref: DiscoveredAgent,
    allow_auto_start: bool,
    mngr_ctx: MngrContext,
) -> tuple[AgentInterface, OnlineHostInterface]:
    """Bring the host online, resolve the agent, and ensure the agent is running.

    When ``allow_auto_start`` is True, both an offline host and a stopped
    agent are started automatically. When False, either condition raises
    :class:`UserInputError`.

    Raises :class:`RuntimeError` if the agent was found during discovery
    but is missing on the live host.
    """
    agent, online_host = ensure_host_started_and_resolve_agent(host_ref, agent_ref, allow_auto_start, mngr_ctx)
    lifecycle_state = agent.get_lifecycle_state()
    if lifecycle_state in (
        AgentLifecycleState.RUNNING,
        AgentLifecycleState.REPLACED,
        AgentLifecycleState.RUNNING_UNKNOWN_AGENT_TYPE,
        AgentLifecycleState.WAITING,
    ):
        return agent, online_host
    if not allow_auto_start:
        raise UserInputError(
            f"Agent '{agent.name}' is stopped and automatic starting is disabled. "
            "Pass --start to start the agent automatically."
        )
    logger.info("Agent {} is stopped, starting it", agent.name)
    agent.wait_for_ready_signal(
        is_creating=False,
        start_action=lambda: online_host.start_agents([agent.id]),
        timeout=agent.get_ready_timeout_seconds(),
    )
    return agent, online_host


def select_agent_interactively_with_host(
    mngr_ctx: MngrContext,
    agent_filter: Callable[[AgentDetails], bool] | None = None,
    no_agents_message: str = "No agents found",
) -> tuple[DiscoveredHost, DiscoveredAgent] | None:
    """Show interactive UI to select an agent.

    When ``agent_filter`` is provided, only agents matching the predicate
    are shown in the interactive selector. Returns the chosen agent's
    discovery refs, or ``None`` if the user quit without selecting.
    Callers compose with :func:`ensure_host_started_and_resolve_agent` or
    :func:`ensure_host_and_agent_started` to bring the result live.
    """
    list_result = list_agents(mngr_ctx, is_streaming=False)
    agents = list_result.agents
    if agent_filter is not None:
        agents = [a for a in agents if agent_filter(a)]
    if not agents:
        raise UserInputError(no_agents_message)

    selected = select_agent_interactively(agents)
    if selected is None:
        return None

    return find_one_agent(AgentAddress(agent=selected.id), mngr_ctx)


def find_agent_for_command(
    mngr_ctx: MngrContext,
    address: AgentAddress | None,
    host_filter: HostAddress | None,
    agent_filter: Callable[[AgentDetails], bool] | None = None,
    no_agents_message: str = "No agents found",
) -> tuple[DiscoveredHost, DiscoveredAgent] | None:
    """Find an agent by address, or interactively if no address is given.

    The optional ``host_filter`` is an additional :class:`HostAddress`
    constraint applied on top of the address (e.g. from a ``--host`` flag).
    It is merged into the address; if the address already pins a different
    host, this raises :class:`UserInputError`.

    Returns the agent's discovery refs, or ``None`` if the user cancelled
    interactive selection. Callers compose with
    :func:`ensure_host_started_and_resolve_agent` or
    :func:`ensure_host_and_agent_started` to bring the result live.

    Raises :class:`UserInputError` if no address is given and the session
    is not interactive.
    """
    if address is not None:
        if host_filter is not None:
            if address.host is not None and address.host != host_filter:
                raise UserInputError(f"Address host ({address.host}) conflicts with --host filter ({host_filter}).")
            address = AgentAddress(agent=address.agent, host=host_filter)
        return find_one_agent(address, mngr_ctx)

    if not mngr_ctx.is_interactive:
        raise UserInputError("No agent specified and not running in interactive mode (specify an agent name or ID)")

    return select_agent_interactively_with_host(
        mngr_ctx,
        agent_filter=agent_filter,
        no_agents_message=no_agents_message,
    )


def stop_agent_after_sync(
    agent: AgentInterface,
    host: OnlineHostInterface,
    is_dry_run: bool,
    output_format: OutputFormat,
) -> None:
    """Stop an agent after a sync operation, respecting dry-run mode."""
    if is_dry_run:
        emit_info("Dry run: would stop agent after sync", output_format)
    else:
        emit_info(f"Stopping agent: {agent.name}", output_format)
        host.stop_agents([agent.id])
        emit_info("Agent stopped", output_format)
