import click
from loguru import logger

from imbue.mngr.api.find import find_one_agent
from imbue.mngr.api.list import list_agents
from imbue.mngr.cli.agent_selector import select_agent_interactively
from imbue.mngr.cli.output_helpers import emit_info
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostAddress
from imbue.mngr.primitives import OutputFormat


def find_agent_by_address_or_interactively(
    mngr_ctx: MngrContext,
    address: AgentAddress | None,
    host_filter: HostAddress | None,
    include_filters: tuple[str, ...] = (),
    exclude_filters: tuple[str, ...] = (),
    no_agents_message: str = "No agents found",
) -> tuple[DiscoveredHost, DiscoveredAgent]:
    """Find an agent by address, or interactively if no address is given.

    The optional ``host_filter`` is an additional :class:`HostAddress`
    constraint applied on top of the address (e.g. from a ``--host`` flag).
    It is merged into the address; if the address already pins a different
    host, this raises :class:`UserInputError`.

    The optional ``include_filters`` / ``exclude_filters`` are CEL expressions
    that narrow the candidate pool of the interactive selector. They are
    ignored when ``address`` is given.

    Returns the chosen agent's discovery refs. Callers compose with
    :func:`imbue.mngr.api.find.resolve_to_started_host_and_agent` or
    :func:`imbue.mngr.api.find.resolve_to_started_host_and_running_agent`
    to bring the result live.

    Raises :class:`UserInputError` if no address is given and the session
    is not interactive, or if the interactive candidate pool is empty.
    Raises :class:`click.Abort` if the user quits the interactive selector
    without choosing an agent (which Click handles as a clean cancellation
    rather than printing a stack trace).
    """
    if address is not None:
        if host_filter is not None:
            if address.host is not None and address.host != host_filter:
                raise UserInputError(f"Address host ({address.host}) conflicts with --host filter ({host_filter}).")
            address = AgentAddress(agent=address.agent, host=host_filter)
        return find_one_agent(address, mngr_ctx)

    if not mngr_ctx.is_interactive:
        raise UserInputError("No agent specified and not running in interactive mode (specify an agent name or ID)")

    list_result = list_agents(
        mngr_ctx,
        is_streaming=False,
        include_filters=include_filters,
        exclude_filters=exclude_filters,
    )
    if not list_result.agents:
        raise UserInputError(no_agents_message)

    selected = select_agent_interactively(list_result.agents)
    if selected is None:
        logger.info("No agent selected")
        raise click.Abort()

    return find_one_agent(AgentAddress(agent=selected.id), mngr_ctx)


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
