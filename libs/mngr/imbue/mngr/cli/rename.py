from typing import Any
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger

from imbue.mngr.api.discovery_events import emit_agent_discovered
from imbue.mngr.api.find import ensure_host_started
from imbue.mngr.api.find import find_one_agent_and_agents_by_host
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.cli.address_params import AGENT_ADDRESS
from imbue.mngr.cli.address_params import AGENT_NAME
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.label import parse_label_string
from imbue.mngr.cli.output_helpers import emit_event
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.output_helpers import write_json_line
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import OutputFormat


class RenameCliOptions(CommonCliOptions):
    """Options passed from the CLI to the rename command."""

    current: AgentAddress
    new_name: AgentName
    dry_run: bool
    start: bool
    label: tuple[str, ...] = ()
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
            write_json_line(result_data)
        case OutputFormat.JSONL:
            emit_event("rename_result", result_data, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            write_human_line("Renamed agent: {} -> {}", old_name, new_name)
        case _ as unreachable:
            assert_never(unreachable)


@click.command(name="rename")
@click.argument("current", type=AGENT_ADDRESS)
@click.argument("new_name", type=AGENT_NAME, metavar="NEW-NAME")
@optgroup.group("Behavior")
@optgroup.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be renamed without actually renaming",
)
@optgroup.option(
    "--start/--no-start",
    default=False,
    show_default=True,
    help=(
        "If the host is offline, start it before renaming so the tmux "
        "session and on-host env file are updated alongside data.json. "
        "Default: do not start; rename only edits the provider's persisted "
        "agent data."
    ),
)
@optgroup.option(
    "--host",
    is_flag=True,
    help="Rename a host instead of an agent [future]",
)
@optgroup.group("Labels")
@optgroup.option(
    "-l",
    "--label",
    multiple=True,
    help=(
        "Apply a KEY=VALUE label in the same atomic write as the rename "
        "(repeatable). Avoids the race where an external observer sees the "
        "renamed agent before separate `mngr label` calls have applied "
        "labels."
    ),
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

    new_agent_name = opts.new_name

    # Parse any --label KEY=VALUE pairs to merge in the same write as the rename.
    labels_to_merge: dict[str, str] = {}
    for label_str in opts.label:
        key, value = parse_label_string(label_str)
        labels_to_merge[key] = value

    # Resolve the agent's metadata. Discovery covers both online and offline
    # hosts (the latter via the provider's persisted agent data), so we never
    # need to start the host just to rename.
    host_ref, agent_ref, agents_by_host = find_one_agent_and_agents_by_host(opts.current, mngr_ctx)

    old_name = str(agent_ref.agent_name)

    # Check if the name is actually changing
    if agent_ref.agent_name == new_agent_name:
        _output(f"Agent already named: {new_agent_name}", output_opts)
        return

    # Check for name conflicts using the already-loaded agent references
    for other_refs in agents_by_host.values():
        for other_ref in other_refs:
            if other_ref.agent_name == new_agent_name and other_ref.agent_id != agent_ref.agent_id:
                raise UserInputError(f"An agent named '{new_agent_name}' already exists (ID: {other_ref.agent_id})")

    # Handle dry-run mode
    if opts.dry_run:
        _output(f"Would rename agent: {old_name} -> {new_agent_name}", output_opts)
        if labels_to_merge:
            _output(f"Would merge labels: {labels_to_merge}", output_opts)
        return

    # Online and offline hosts both implement rename_agent; the offline
    # variant only edits the provider's persisted data (no tmux/env updates).
    # With --start, force the host online first so tmux and env files are
    # updated too; otherwise rename whichever host kind we end up with.
    provider = get_provider_instance(host_ref.provider_name, mngr_ctx)
    host = provider.get_host(host_ref.host_id)
    if opts.start and not isinstance(host, OnlineHostInterface):
        host, _ = ensure_host_started(host, is_start_desired=True, provider=provider)
    updated_ref = host.rename_agent(agent_ref, new_agent_name, labels_to_merge=labels_to_merge or None)

    # Only the renamed agent's metadata changed; the host and other agents
    # on it are untouched, so a single agent_discovered event suffices.
    emit_agent_discovered(mngr_ctx.config, updated_ref)

    # Warn that the git branch was not renamed (only in human output mode)
    if output_opts.output_format == OutputFormat.HUMAN:
        logger.warning("Note: the git branch name was not changed. You may want to rename it manually.")

    # Output the result
    _output_result(
        old_name=old_name,
        new_name=str(updated_ref.agent_name),
        agent_id=str(updated_ref.agent_id),
        output_opts=output_opts,
    )


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="rename",
    one_line_description="Rename an agent or host [experimental]",
    synopsis="mngr [rename|mv] <CURRENT> <NEW-NAME> [--dry-run] [--start/--no-start] [--host] [-l KEY=VALUE ...]",
    arguments_description="- `CURRENT`: Current name or ID of the agent to rename\n- `NEW-NAME`: New name for the agent",
    description="""Updates the agent's name in its data.json and renames the tmux session
if the agent is currently running. Git branch names are not renamed.

If the host is offline, the rename is applied to the provider's
persisted agent data without starting the host; tmux and env-file
updates are skipped (data.json remains the source of truth for the
agent's name). Pass --start to force the host online first so tmux
and the env file are updated alongside data.json.

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
