from collections.abc import Callable

from imbue.mngr.api.find import find_one_agent
from imbue.mngr.api.list import list_agents
from imbue.mngr.cli.connect import select_agent_interactively
from imbue.mngr.cli.output_helpers import emit_info
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import OutputFormat


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

    return find_one_agent(
        AgentAddress(agent=selected.id),
        mngr_ctx,
        is_start_desired=is_start_desired,
        skip_agent_state_check=skip_agent_state_check,
    )


def find_agent_for_command(
    mngr_ctx: MngrContext,
    address: AgentAddress | None,
    is_start_desired: bool = False,
    skip_agent_state_check: bool = False,
    agent_filter: Callable[[AgentDetails], bool] | None = None,
    no_agents_message: str = "No agents found",
) -> tuple[AgentInterface, OnlineHostInterface] | None:
    """Find an agent by address, or interactively if no address is given.

    Returns ``(agent, host)``, or ``None`` if the user cancelled interactive
    selection. Raises :class:`UserInputError` if no address is given and the
    session is not interactive.
    """
    if address is not None:
        return find_one_agent(
            address,
            mngr_ctx,
            is_start_desired=is_start_desired,
            skip_agent_state_check=skip_agent_state_check,
        )

    if not mngr_ctx.is_interactive:
        raise UserInputError("No agent specified and not running in interactive mode (specify an agent name or ID)")

    return select_agent_interactively_with_host(
        mngr_ctx,
        is_start_desired=is_start_desired,
        skip_agent_state_check=skip_agent_state_check,
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
