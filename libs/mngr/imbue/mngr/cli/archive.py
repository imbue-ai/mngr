from datetime import datetime
from datetime import timezone
from typing import Any
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger

from imbue.mngr.api.discovery_events import emit_discovery_events_for_host
from imbue.mngr.api.find import AgentMatch
from imbue.mngr.api.find import group_agents_by_host
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.cli.agent_addr import find_agents_by_addresses
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.label import apply_labels
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.stdin_utils import expand_stdin_placeholder
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.errors import HostOfflineError
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import OutputFormat


class ArchiveCliOptions(CommonCliOptions):
    """Options passed from the CLI to the archive command."""

    agents: tuple[str, ...]
    agent_list: tuple[str, ...]
    archive_all: bool
    force: bool
    dry_run: bool


def _output(message: str, output_opts: OutputOptions) -> None:
    """Output a message in human format."""
    if output_opts.output_format == OutputFormat.HUMAN:
        write_human_line(message)


@click.command(name="archive")
@click.argument("agents", nargs=-1, required=False)
@optgroup.group("Target Selection")
@optgroup.option(
    "--agent",
    "agent_list",
    multiple=True,
    help="Agent name or ID to archive (can be specified multiple times)",
)
@optgroup.option(
    "-a",
    "--all",
    "--all-agents",
    "archive_all",
    is_flag=True,
    help="Archive all agents",
)
@optgroup.group("Behavior")
@optgroup.option(
    "-f",
    "--force",
    is_flag=True,
    help="Stop running agents before archiving (without this, running agents are skipped)",
)
@optgroup.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be archived without actually archiving",
)
@add_common_options
@click.pass_context
def archive(ctx: click.Context, **kwargs: Any) -> None:
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="archive",
        command_class=ArchiveCliOptions,
    )

    # Collect agent identifiers from positional args and --agent flag
    agent_identifiers = expand_stdin_placeholder(opts.agents) + list(opts.agent_list)

    if not agent_identifiers and not opts.archive_all:
        raise UserInputError("Must specify at least one agent or use --all")

    if agent_identifiers and opts.archive_all:
        raise UserInputError("Cannot specify both agent names and --all")

    # Find agents in any state (archive applies to stopped agents)
    target_agents = find_agents_by_addresses(
        raw_identifiers=agent_identifiers,
        filter_all=opts.archive_all,
        target_state=None,
        mngr_ctx=mngr_ctx,
    )

    if not target_agents:
        _output("No agents found to archive", output_opts)
        return

    # Handle dry-run mode
    if opts.dry_run:
        _output("Would archive:", output_opts)
        for match in target_agents:
            _output(f"  - {match.agent_name} (on host {match.host_id})", output_opts)
        return

    # Separate running agents that need to be stopped first
    agents_to_archive, running_agents = _partition_running_agents(target_agents, mngr_ctx)

    if running_agents:
        if opts.force:
            _stop_running_agents(running_agents, mngr_ctx, output_opts)
            agents_to_archive.extend(running_agents)
        else:
            for match in running_agents:
                logger.warning("Skipping running agent {} (use --force to stop it first)", match.agent_name)

    if not agents_to_archive:
        _output("No agents to archive (all targeted agents are running; use --force to stop them first)", output_opts)
        return

    # Apply the archived_at label
    now = datetime.now(timezone.utc).isoformat()
    apply_labels(agents_to_archive, {"archived_at": now}, mngr_ctx, output_opts)

    _output(f"Archived {len(agents_to_archive)} agent(s)", output_opts)


def _partition_running_agents(
    agents: list[AgentMatch],
    mngr_ctx: MngrContext,
) -> tuple[list[AgentMatch], list[AgentMatch]]:
    """Partition agents into non-running (archivable) and running lists.

    Agents on offline hosts are always considered non-running (archivable).
    """
    non_running: list[AgentMatch] = []
    running: list[AgentMatch] = []

    agents_by_host = group_agents_by_host(agents)

    for host_key, agent_list in agents_by_host.items():
        host_id_str, _ = host_key.split(":", 1)
        provider_name = agent_list[0].provider_name

        provider = get_provider_instance(provider_name, mngr_ctx)
        host = provider.get_host(HostId(host_id_str))

        match host:
            case OnlineHostInterface() as online_host:
                host_agents = online_host.get_agents()
                running_agent_ids = {
                    agent.id for agent in host_agents if agent.is_running()
                }
                for agent_match in agent_list:
                    if agent_match.agent_id in running_agent_ids:
                        running.append(agent_match)
                    else:
                        non_running.append(agent_match)
            case HostInterface():
                # Offline hosts: all agents are considered stopped
                non_running.extend(agent_list)
            case _ as unreachable:
                assert_never(unreachable)

    return non_running, running


def _stop_running_agents(
    running_agents: list[AgentMatch],
    mngr_ctx: MngrContext,
    output_opts: OutputOptions,
) -> None:
    """Stop the given running agents."""
    agents_by_host = group_agents_by_host(running_agents)

    for host_key, agent_list in agents_by_host.items():
        host_id_str, _ = host_key.split(":", 1)
        provider_name = agent_list[0].provider_name

        provider = get_provider_instance(provider_name, mngr_ctx)
        host = provider.get_host(HostId(host_id_str))

        match host:
            case OnlineHostInterface() as online_host:
                agent_ids_to_stop = [m.agent_id for m in agent_list]
                online_host.stop_agents(agent_ids_to_stop)

                for m in agent_list:
                    _output(f"Stopped agent: {m.agent_name}", output_opts)

                emit_discovery_events_for_host(mngr_ctx.config, online_host)
            case HostInterface():
                raise HostOfflineError(f"Host '{host_id_str}' is offline. Cannot stop agents on offline hosts.")
            case _ as unreachable:
                assert_never(unreachable)


CommandHelpMetadata(
    key="archive",
    one_line_description="Archive agents (set the 'archived_at' label)",
    synopsis="mngr archive [AGENTS...] [--agent <AGENT>] [--all] [-f|--force] [--dry-run]",
    arguments_description="- `AGENTS`: Agent name(s) or ID(s) to archive.",
    description="""Sets an 'archived_at' label with the current UTC timestamp on each
targeted agent. By default, only non-running agents are archived; running
agents are skipped with a warning.

Use --force to stop running agents before archiving them.

Archived agents remain in 'mngr list' output but can be filtered out
using label-based filtering. Their state is preserved (not destroyed),
so they can be restarted later if needed.""",
    examples=(
        ("Archive a stopped agent", "mngr archive my-agent"),
        ("Archive multiple agents", "mngr archive agent1 agent2"),
        ("Force-stop and archive a running agent", "mngr archive my-agent --force"),
        ("Archive all non-running agents", "mngr archive --all"),
        ("Force-stop and archive all agents", "mngr archive --all --force"),
        ("Preview what would be archived", "mngr archive --all --dry-run"),
    ),
    see_also=(
        ("stop", "Stop agents without archiving"),
        ("label", "Set arbitrary labels on agents"),
        ("list", "List agents (use labels to filter archived agents)"),
        ("start", "Restart archived agents"),
    ),
).register()

add_pager_help_option(archive)
