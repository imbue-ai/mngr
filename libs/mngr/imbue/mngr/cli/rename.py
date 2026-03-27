from typing import Any
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger

from imbue.mngr.api.discover import discover_hosts_and_agents
from imbue.mngr.api.discovery_events import emit_discovery_events_for_host
from imbue.mngr.cli.agent_addr import find_agent_by_address
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import emit_event
from imbue.mngr.cli.output_helpers import emit_final_json
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.errors import UserInputError
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import OutputFormat


class RenameCliOptions(CommonCliOptions):
    """Options passed from the CLI to the rename command."""

    current: str
    new_name: str
    dry_run: bool
    # Planned features (not yet implemented)
    host: bool


def _output(message: str, output_opts: OutputOptions) -> None:
    """Output a message according to the format."""
    if output_opts.output_format == OutputFormat.HUMAN:
        write_human_line(message)


def _output_result(
    old_name: str,
    new_name: str,
    agent_id: str,
    output_opts: OutputOptions,
) -> None:
    """Output the final result."""
    result_data = {
        "old_name": old_name,
        "new_name": new_name,
        "agent_id": agent_id,
    }
    match output_opts.output_format:
        case OutputFormat.JSON:
            emit_final_json(result_data)
        case OutputFormat.JSONL:
            emit_event("rename_result", result_data, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            write_human_line("Renamed agent: {} -> {}", old_name, new_name)
        case _ as unreachable:
            assert_never(unreachable)


@click.command(name="rename")
@click.argument("current")
@click.argument("new_name", metavar="NEW-NAME")
@optgroup.group("Behavior")
@optgroup.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be renamed without actually renaming",
)
@optgroup.option(
    "--host",
    is_flag=True,
    help="Rename a host instead of an agent [future]",
)
@add_common_options
@click.pass_context
def rename(ctx: click.Context, **kwargs: Any) -> None:
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="rename",
        command_class=RenameCliOptions,
    )
    logger.debug("Started rename command")

    # Check for unsupported [future] options
    if opts.host:
        raise NotImplementedError("--host is not implemented yet. Currently only agent renaming is supported.")

    # Validate new name
    try:
        new_agent_name = AgentName(opts.new_name)
    except ValueError as e:
        raise UserInputError(f"Invalid new name: {e}") from None

    # Resolve the agent (without requiring the agent process to be running)
    agents_by_host, _ = discover_hosts_and_agents(
        mngr_ctx,
        provider_names=None,
        agent_identifiers=(opts.current,),
        include_destroyed=False,
        reset_caches=False,
    )
    agent, host = find_agent_by_address(
        opts.current,
        agents_by_host,
        mngr_ctx,
        "rename",
        skip_agent_state_check=True,
    )

    old_name = str(agent.name)

    # Check if the name is actually changing
    if agent.name == new_agent_name:
        _output(f"Agent already named: {new_agent_name}", output_opts)
        return

    # Check for name conflicts using the already-loaded agent references
    for agent_refs in agents_by_host.values():
        for agent_ref in agent_refs:
            if agent_ref.agent_name == new_agent_name and agent_ref.agent_id != agent.id:
                raise UserInputError(f"An agent named '{new_agent_name}' already exists (ID: {agent_ref.agent_id})")

    # Handle dry-run mode
    if opts.dry_run:
        _output(f"Would rename agent: {old_name} -> {new_agent_name}", output_opts)
        return

    # Perform the rename
    updated_agent = host.rename_agent(agent, new_agent_name)

    # Emit discovery events for renamed agent and host
    emit_discovery_events_for_host(mngr_ctx.config, host)

    # Warn that the git branch was not renamed (only in human output mode)
    if output_opts.output_format == OutputFormat.HUMAN:
        logger.warning("Note: the git branch name was not changed. You may want to rename it manually.")

    # Output the result
    _output_result(
        old_name=old_name,
        new_name=str(updated_agent.name),
        agent_id=str(updated_agent.id),
        output_opts=output_opts,
    )


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="rename",
    one_line_description="Rename an agent or host [experimental]",
    synopsis="mngr [rename|mv] <CURRENT> <NEW-NAME> [--dry-run] [--host]",
    arguments_description="- `CURRENT`: Current name or ID of the agent to rename\n- `NEW-NAME`: New name for the agent",
    description="""Updates the agent's name in its data.json and renames the tmux session
if the agent is currently running. Git branch names are not renamed.

If a previous rename was interrupted (e.g., the tmux session was renamed
but data.json was not updated), re-running the command will attempt
to complete it.""",
    aliases=("mv",),
    examples=(
        ("Rename an agent", "mngr rename my-agent new-name"),
        ("Preview what would be renamed", "mngr rename my-agent new-name --dry-run"),
        ("Use the alias", "mngr mv my-agent new-name"),
    ),
    see_also=(
        ("list", "List existing agents"),
        ("create", "Create a new agent"),
        ("destroy", "Destroy an agent"),
    ),
).register()

# Add pager-enabled help option to the rename command
add_pager_help_option(rename)
