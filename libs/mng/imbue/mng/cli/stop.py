from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import assert_never

import click
from click_option_group import optgroup

from imbue.mng.api.discovery_events import emit_discovery_events_for_host
from imbue.mng.api.find import AgentMatch
from imbue.mng.api.find import group_agents_by_host
from imbue.mng.api.providers import get_provider_instance
from imbue.mng.cli.agent_addr import find_agents_by_addresses
from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng.cli.destroy import get_agent_name_from_session
from imbue.mng.cli.help_formatter import CommandHelpMetadata
from imbue.mng.cli.help_formatter import add_pager_help_option
from imbue.mng.cli.label import apply_labels
from imbue.mng.cli.output_helpers import emit_event
from imbue.mng.cli.output_helpers import emit_final_json
from imbue.mng.cli.output_helpers import emit_format_template_lines
from imbue.mng.cli.output_helpers import write_human_line
from imbue.mng.cli.stdin_utils import expand_stdin_placeholder
from imbue.mng.config.data_types import CommonCliOptions
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.errors import HostOfflineError
from imbue.mng.errors import UserInputError
from imbue.mng.interfaces.host import HostInterface
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import HostId
from imbue.mng.primitives import OutputFormat


class StopCliOptions(CommonCliOptions):
    """Options passed from the CLI to the stop command."""

    agents: tuple[str, ...]
    agent_list: tuple[str, ...]
    stop_all: bool
    dry_run: bool
    archive: bool
    sessions: tuple[str, ...]
    # Planned features (not yet implemented)
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    snapshot_mode: str | None
    graceful: bool
    graceful_timeout: str | None


def _output(message: str, output_opts: OutputOptions) -> None:
    """Output a message according to the format."""
    if output_opts.output_format == OutputFormat.HUMAN:
        write_human_line(message)


def _output_result(stopped_agents: Sequence[str], output_opts: OutputOptions) -> None:
    """Output the final result."""
    if output_opts.format_template is not None:
        items = [{"name": name} for name in stopped_agents]
        emit_format_template_lines(output_opts.format_template, items)
        return
    result_data = {"stopped_agents": stopped_agents, "count": len(stopped_agents)}
    match output_opts.output_format:
        case OutputFormat.JSON:
            emit_final_json(result_data)
        case OutputFormat.JSONL:
            emit_event("stop_result", result_data, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            if stopped_agents:
                write_human_line("Successfully stopped {} agent(s)", len(stopped_agents))
        case _ as unreachable:
            assert_never(unreachable)


@click.command(name="stop")
@click.argument("agents", nargs=-1, required=False)
@optgroup.group("Target Selection")
@optgroup.option(
    "--agent",
    "agent_list",
    multiple=True,
    help="Agent name or ID to stop (can be specified multiple times)",
)
@optgroup.option(
    "-a",
    "--all",
    "--all-agents",
    "stop_all",
    is_flag=True,
    help="Stop all running agents",
)
@optgroup.option(
    "--session",
    "sessions",
    multiple=True,
    help="Tmux session name to stop (can be specified multiple times). The agent name is extracted by "
    "stripping the configured prefix from the session name.",
)
@optgroup.option(
    "--include",
    multiple=True,
    help="Filter agents to stop by CEL expression (repeatable) [future]",
)
@optgroup.option(
    "--exclude",
    multiple=True,
    help="Exclude agents matching CEL expression (repeatable) [future]",
)
@optgroup.group("Behavior")
@optgroup.option(
    "--archive",
    is_flag=True,
    help="Set an 'archived_at' label on each stopped agent (marks it as archived)",
)
@optgroup.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be stopped without actually stopping",
)
@optgroup.option(
    "--snapshot-mode",
    type=click.Choice(["auto", "always", "never"], case_sensitive=False),
    default=None,
    help="Control snapshot creation when stopping: auto (snapshot if needed), always, or never [future]",
)
@optgroup.option(
    "--graceful/--no-graceful",
    default=True,
    help="Wait for agent to reach a clean state before stopping [future]",
)
@optgroup.option(
    "--graceful-timeout",
    type=str,
    default=None,
    help="Timeout for graceful stop (e.g., 30s, 5m) [future]",
)
@add_common_options
@click.pass_context
def stop(ctx: click.Context, **kwargs: Any) -> None:
    mng_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="stop",
        command_class=StopCliOptions,
        is_format_template_supported=True,
    )

    # Check for unsupported [future] options
    if opts.include:
        raise NotImplementedError("--include is not implemented yet")
    if opts.exclude:
        raise NotImplementedError("--exclude is not implemented yet")
    if opts.snapshot_mode is not None:
        raise NotImplementedError("--snapshot-mode is not implemented yet")
    if not opts.graceful:
        raise NotImplementedError("--no-graceful is not implemented yet")
    if opts.graceful_timeout is not None:
        raise NotImplementedError("--graceful-timeout is not implemented yet")

    # Validate input
    agent_identifiers = expand_stdin_placeholder(opts.agents) + list(opts.agent_list)

    # Handle --session option by extracting agent names from session names
    if opts.sessions:
        if agent_identifiers or opts.stop_all:
            raise UserInputError("Cannot specify --session with agent names or --all")
        for session_name in opts.sessions:
            agent_name = get_agent_name_from_session(session_name, mng_ctx.config.prefix)
            if agent_name is None:
                raise UserInputError(
                    f"Session '{session_name}' does not match the expected format. "
                    f"Session names should start with the configured prefix '{mng_ctx.config.prefix}'."
                )
            agent_identifiers.append(agent_name)

    if not agent_identifiers and not opts.stop_all:
        raise click.UsageError("Must specify at least one agent or use --all")

    if agent_identifiers and opts.stop_all:
        raise click.UsageError("Cannot specify both agent names and --all")

    # Find agents to stop (RUNNING agents when using --all)
    agents_to_stop = find_agents_by_addresses(
        raw_identifiers=agent_identifiers,
        filter_all=opts.stop_all,
        target_state=AgentLifecycleState.RUNNING,
        mng_ctx=mng_ctx,
    )

    if not agents_to_stop:
        _output("No running agents found to stop", output_opts)
        return

    # Handle dry-run mode
    if opts.dry_run:
        _output("Would stop:", output_opts)
        for match in agents_to_stop:
            _output(f"  - {match.agent_name} (on host {match.host_id})", output_opts)
        return

    # Stop each agent
    stopped_agents: list[str] = []
    stopped_matches: list[AgentMatch] = []

    # Group agents by host to stop them together
    agents_by_host = group_agents_by_host(agents_to_stop)

    for host_key, agent_list in agents_by_host.items():
        host_id_str, _ = host_key.split(":", 1)
        # Get provider from first agent (all agents in list have same provider)
        provider_name = agent_list[0].provider_name

        provider = get_provider_instance(provider_name, mng_ctx)
        host = provider.get_host(HostId(host_id_str))

        # Ensure host is online (can't stop agents on offline hosts)
        match host:
            case OnlineHostInterface() as online_host:
                # Stop each agent on this host
                agent_ids_to_stop = [m.agent_id for m in agent_list]
                online_host.stop_agents(agent_ids_to_stop)

                for m in agent_list:
                    stopped_agents.append(str(m.agent_name))
                    stopped_matches.append(m)
                    _output(f"Stopped agent: {m.agent_name}", output_opts)

                # Emit discovery events for stopped agents and host
                emit_discovery_events_for_host(mng_ctx.config, online_host)
            case HostInterface():
                raise HostOfflineError(f"Host '{host_id_str}' is offline. Cannot stop agents on offline hosts.")
            case _ as unreachable:
                assert_never(unreachable)

    # Archive stopped agents if requested
    if opts.archive and stopped_matches:
        now = datetime.now(timezone.utc).isoformat()
        apply_labels(stopped_matches, {"archived_at": now}, mng_ctx, output_opts)

    # Output final result
    _output_result(stopped_agents, output_opts)


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="stop",
    one_line_description="Stop running agent(s)",
    synopsis="mng [stop|s] [AGENTS...|-] [--agent <AGENT>] [--all] [--session <SESSION>] [--archive] [--dry-run] [--snapshot-mode <MODE>] [--graceful/--no-graceful]",
    description="""For remote hosts, this stops the agent's tmux session. The host remains
running unless idle detection stops it automatically.

For local agents, this stops the agent's tmux session. The local host
itself cannot be stopped (if you want that, shut down your computer).

Use --archive to also set an 'archived_at' label on each stopped agent.
This marks the agent as archived without destroying it, allowing it to
be filtered out of listings while preserving its state. The 'mng archive'
command is a shorthand for 'mng stop --archive'.

Supports custom format templates via --format. Available fields: name.""",
    aliases=(),
    examples=(
        ("Stop an agent by name", "mng stop my-agent"),
        ("Stop multiple agents", "mng stop agent1 agent2"),
        ("Stop all running agents", "mng stop --all"),
        ("Stop and archive an agent", "mng stop my-agent --archive"),
        ("Stop by tmux session name", "mng stop --session mng-my-agent"),
        ("Preview what would be stopped", "mng stop --all --dry-run"),
        ("Custom format template output", "mng stop --all --format '{name}'"),
    ),
    see_also=(
        ("start", "Start stopped agents"),
        ("connect", "Connect to an agent"),
        ("list", "List existing agents"),
        ("archive", "Stop and archive agents (shorthand for stop --archive)"),
    ),
).register()

# Add pager-enabled help option to the stop command
add_pager_help_option(stop)
