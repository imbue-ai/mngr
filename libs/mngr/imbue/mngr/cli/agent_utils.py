from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence

from imbue.imbue_common.pure import pure
from imbue.mngr.api.discover import discover_by_address
from imbue.mngr.api.discover import discover_hosts_and_agents
from imbue.mngr.api.find import find_agent_by_address
from imbue.mngr.api.find import find_and_maybe_start_agent
from imbue.mngr.api.list import list_agents
from imbue.mngr.cli.connect import select_agent_interactively
from imbue.mngr.cli.output_helpers import emit_info
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentAddress
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


def select_agent_interactively_with_host(
    mngr_ctx: MngrContext,
    is_start_desired: bool = False,
    skip_agent_state_check: bool = False,
    agent_filter: Callable[[AgentDetails], bool] | None = None,
    no_agents_message: str = "No agents found",
) -> tuple[AgentInterface, OnlineHostInterface] | None:
    """Show interactive UI to select an agent.

    When agent_filter is provided, only agents matching the predicate are shown
    in the interactive selector.

    Returns tuple of (agent, host) or None if user quit without selecting.
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

    agents_by_host, _ = discover_hosts_and_agents(
        mngr_ctx,
        provider_names=None,
        agent_identifiers=(str(selected.id),),
        include_destroyed=False,
        reset_caches=False,
    )
    return find_and_maybe_start_agent(
        selected.id,
        agents_by_host,
        mngr_ctx,
        "select",
        is_start_desired=is_start_desired,
        skip_agent_state_check=skip_agent_state_check,
    )


def find_agent_for_command(
    mngr_ctx: MngrContext,
    address: AgentAddress | None,
    command_usage: str,
    host_filter: HostAddress | None,
    is_start_desired: bool = False,
    skip_agent_state_check: bool = False,
    agent_filter: Callable[[AgentDetails], bool] | None = None,
    no_agents_message: str = "No agents found",
) -> tuple[AgentInterface, OnlineHostInterface] | None:
    """Find an agent by address, or interactively if no address is given.

    The optional ``host_filter`` is an additional :class:`HostAddress`
    constraint applied on top of the address (e.g. from a ``--host`` flag).

    Returns ``(agent, host)``, or ``None`` if the user cancelled interactive
    selection. Raises :class:`UserInputError` if no address is given and the
    session is not interactive.
    """
    if address is not None:
        if host_filter is not None:
            agents_by_host, _ = discover_by_address(address, mngr_ctx, include_destroyed=False)
            agents_by_host = filter_agents_by_host(agents_by_host, host_filter)
            return find_and_maybe_start_agent(
                address.agent,
                agents_by_host,
                mngr_ctx,
                command_usage,
                is_start_desired=is_start_desired,
                skip_agent_state_check=skip_agent_state_check,
            )
        return find_agent_by_address(
            address,
            mngr_ctx,
            command_usage,
            is_start_desired=is_start_desired,
            skip_agent_state_check=skip_agent_state_check,
        )

    if not mngr_ctx.is_interactive:
        raise UserInputError("No agent specified and not running in interactive mode (specify an agent name or ID)")

    result = select_agent_interactively_with_host(
        mngr_ctx,
        is_start_desired=is_start_desired,
        skip_agent_state_check=skip_agent_state_check,
        agent_filter=agent_filter,
        no_agents_message=no_agents_message,
    )
    if result is None:
        return None
    return result


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
