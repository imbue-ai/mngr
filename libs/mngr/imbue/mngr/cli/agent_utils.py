from collections.abc import Callable

from imbue.imbue_common.pure import pure
from imbue.mngr.api.discover import discover_hosts_and_agents
from imbue.mngr.api.find import find_and_maybe_start_agent_by_name_or_id
from imbue.mngr.api.list import list_agents
from imbue.mngr.cli.agent_addr import find_agent_by_address
from imbue.mngr.cli.connect import select_agent_interactively
from imbue.mngr.cli.output_helpers import emit_info
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import OutputFormat


@pure
def _host_matches_filter(host_ref: DiscoveredHost, host_filter: str) -> bool:
    """Check if a host reference matches the given filter string.

    The filter can be either a HostId (UUID) or a HostName.
    """
    # Try matching as HostId first
    try:
        filter_as_id = HostId(host_filter)
        if host_ref.host_id == filter_as_id:
            return True
    except ValueError:
        pass

    # Try matching as HostName
    filter_as_name = HostName(host_filter)
    return host_ref.host_name == filter_as_name


@pure
def filter_agents_by_host(
    agents_by_host: dict[DiscoveredHost, list[DiscoveredAgent]],
    host_filter: str,
) -> dict[DiscoveredHost, list[DiscoveredAgent]]:
    """Filter the agents_by_host mapping to only include hosts matching the filter.

    Raises UserInputError if no hosts match the filter.
    """
    filtered = {
        host_ref: agent_refs
        for host_ref, agent_refs in agents_by_host.items()
        if _host_matches_filter(host_ref, host_filter)
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

    # Find the actual agent and host from the selection
    agents_by_host, _ = discover_hosts_and_agents(
        mngr_ctx,
        provider_names=None,
        agent_identifiers=(str(selected.id),),
        include_destroyed=False,
        reset_caches=False,
    )
    return find_and_maybe_start_agent_by_name_or_id(
        str(selected.id),
        agents_by_host,
        mngr_ctx,
        "select",
        is_start_desired=is_start_desired,
        skip_agent_state_check=skip_agent_state_check,
    )


@pure
def parse_agent_spec(
    spec: str | None,
    explicit_agent: str | None,
    # Used in error messages, e.g. "Target" or "Source"
    spec_name: str,
    default_subpath: str | None = None,
) -> tuple[str | None, str | None]:
    """Parse an AGENT, AGENT:PATH, or PATH specification string.

    Returns (agent_identifier, subpath).
    """
    agent_identifier: str | None = None
    subpath: str | None = default_subpath

    if spec is not None:
        if ":" in spec:
            agent_identifier, parsed_subpath = spec.split(":", 1)
            if default_subpath is not None and parsed_subpath != default_subpath:
                raise UserInputError(
                    f"Cannot specify both a subpath in {spec_name.lower()} "
                    f"('{parsed_subpath}') and --{spec_name.lower()}-path ('{default_subpath}')"
                )
            subpath = parsed_subpath
        elif spec.startswith(("/", "./", "~/", "../")):
            raise UserInputError(f"{spec_name} must include an agent specification")
        else:
            agent_identifier = spec

    if explicit_agent is not None:
        if agent_identifier is not None and agent_identifier != explicit_agent:
            raise UserInputError(
                f"Cannot specify both --{spec_name.lower()} and --{spec_name.lower()}-agent with different values"
            )
        agent_identifier = explicit_agent

    return agent_identifier, subpath


def find_agent_for_command(
    mngr_ctx: MngrContext,
    agent_identifier: str | None,
    command_usage: str,
    host_filter: str | None,
    is_start_desired: bool = False,
    skip_agent_state_check: bool = False,
    agent_filter: Callable[[AgentDetails], bool] | None = None,
    no_agents_message: str = "No agents found",
) -> tuple[AgentInterface, OnlineHostInterface] | None:
    """Find an agent by identifier, or interactively if no identifier given.

    When agent_filter is provided and selection is interactive, only agents
    matching the predicate are shown in the selector.

    Returns (agent, host) tuple, or None if the user cancelled interactive selection.
    Raises UserInputError if no agent specified and not running in interactive mode.
    """
    if agent_identifier is not None:
        agents_by_host, _ = discover_hosts_and_agents(
            mngr_ctx,
            provider_names=None,
            agent_identifiers=(agent_identifier,),
            include_destroyed=False,
            reset_caches=False,
        )
        if host_filter is not None:
            agents_by_host = filter_agents_by_host(agents_by_host, host_filter)
        return find_agent_by_address(
            agent_identifier,
            agents_by_host,
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
