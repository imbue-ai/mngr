from collections.abc import Sequence
from typing import Any
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger

from imbue.imbue_common.logging import log_span
from imbue.mngr.api.connect import connect_to_agent
from imbue.mngr.api.connect import resolve_connect_command
from imbue.mngr.api.connect import run_connect_command
from imbue.mngr.api.data_types import ConnectionOptions
from imbue.mngr.api.discovery_events import emit_discovery_events_for_host
from imbue.mngr.api.find import ensure_host_started
from imbue.mngr.api.find import find_all_agents
from imbue.mngr.api.find import group_agents_by_host
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.cli.address_params import AGENT_ADDRESS
from imbue.mngr.cli.address_params import HOST_ADDRESS
from imbue.mngr.cli.address_params import parse_agent_addresses_or_raise
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import emit_event
from imbue.mngr.cli.output_helpers import emit_format_template_lines
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.output_helpers import write_json_line
from imbue.mngr.cli.stdin_utils import STDIN_PLACEHOLDER
from imbue.mngr.cli.stdin_utils import expand_stdin_placeholder
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.errors import AgentNotFoundOnHostError
from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import require_interactive_agent
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import HostAddress
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.utils.polling import poll_until


class StartCliOptions(CommonCliOptions):
    """Options passed from the CLI to the start command."""

    agents: tuple[str, ...]
    agent_list: tuple[AgentAddress, ...]
    connect: bool
    connect_command: str | None
    restart: bool
    no_resume: bool
    dry_run: bool
    # Planned features (not yet implemented)
    host: tuple[HostAddress, ...]


def _output(message: str, output_opts: OutputOptions) -> None:
    """Output a message according to the format."""
    if output_opts.output_format == OutputFormat.HUMAN:
        write_human_line(message)


def _output_result(started_agents: Sequence[str], output_opts: OutputOptions, *, is_restart: bool = False) -> None:
    """Output the final result."""
    if output_opts.format_template is not None:
        items = [{"name": name} for name in started_agents]
        emit_format_template_lines(output_opts.format_template, items)
        return
    result_data = {"started_agents": started_agents, "count": len(started_agents)}
    match output_opts.output_format:
        case OutputFormat.JSON:
            write_json_line(result_data)
        case OutputFormat.JSONL:
            emit_event("start_result", result_data, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            if started_agents:
                verb = "restarted" if is_restart else "started"
                write_human_line("Successfully {} {} agent(s)", verb, len(started_agents))
        case _ as unreachable:
            assert_never(unreachable)


def _send_resume_message_if_configured(agent: AgentInterface, output_opts: OutputOptions) -> None:
    """Send the resume message to an agent if one is configured."""
    resume_message = agent.get_resume_message()
    if resume_message is None:
        return

    _output(f"Sending resume message to {agent.name}...", output_opts)
    # Wait for the agent to signal readiness via the WAITING lifecycle state.
    # Agents like Claude configure hooks that remove the 'active' file when idle.
    # If the timeout expires (agent doesn't support hooks or is slow), proceed anyway.
    timeout = agent.get_ready_timeout_seconds()
    with log_span("Waiting for agent to become ready before sending resume message"):
        is_ready = poll_until(
            lambda: agent.get_lifecycle_state() == AgentLifecycleState.WAITING,
            timeout=timeout,
            poll_interval=0.2,
        )
    if is_ready:
        logger.debug("Signaled agent readiness via WAITING state")
    else:
        logger.debug(
            "Failed to reach WAITING state within {}s, proceeding anyway",
            timeout,
        )
    require_interactive_agent(agent).send_message(resume_message)
    logger.debug("Sent resume message to agent {}", agent.name)


@click.command(name="start")
@click.argument("agents", nargs=-1, required=False)
@optgroup.group("Target Selection")
@optgroup.option(
    "--agent",
    "agent_list",
    type=AGENT_ADDRESS,
    multiple=True,
    help="Agent address (NAME[@HOST[.PROVIDER]]) to start (can be specified multiple times)",
)
@optgroup.option(
    "--host",
    type=HOST_ADDRESS,
    multiple=True,
    help="Host(s) to start all stopped agents on [repeatable] [future]",
)
@optgroup.group("Behavior")
@optgroup.option(
    "--restart",
    is_flag=True,
    help="Stop the agent first if it is already running, ensuring a clean start.",
)
@optgroup.option(
    "--no-resume",
    is_flag=True,
    help="Skip sending the resume message after starting.",
)
@optgroup.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be started without actually starting anything",
)
@optgroup.option(
    "--connect/--no-connect",
    default=False,
    help="Connect to the agent after starting (only valid for single agent)",
)
@optgroup.option(
    "--connect-command",
    help="Command to run instead of the builtin connect. MNGR_AGENT_NAME and MNGR_SESSION_NAME env vars are set.",
)
@add_common_options
@click.pass_context
def start(ctx: click.Context, **kwargs: Any) -> None:
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="start",
        command_class=StartCliOptions,
        is_format_template_supported=True,
    )

    # Check for unsupported [future] options
    if opts.host:
        raise NotImplementedError("--host is not implemented yet")

    # Validate input
    agent_addresses: list[AgentAddress] = parse_agent_addresses_or_raise(expand_stdin_placeholder(opts.agents)) + list(
        opts.agent_list
    )

    if not agent_addresses:
        if STDIN_PLACEHOLDER not in opts.agents:
            raise click.UsageError("Must specify at least one agent (use '-' to read from stdin)")
        return

    if opts.connect and len(agent_addresses) > 1:
        raise click.UsageError("--connect can only be used with a single agent")

    _start_agents(agent_addresses, mngr_ctx, output_opts, opts)


def _start_agents(
    agent_addresses: list[AgentAddress],
    mngr_ctx: MngrContext,
    output_opts: OutputOptions,
    opts: StartCliOptions,
) -> None:
    is_restart = opts.restart
    send_resume = not opts.no_resume

    # Find agents: STOPPED only for normal start, any state for restart
    target_state = None if is_restart else AgentLifecycleState.STOPPED
    matched_agents = find_all_agents(
        addresses=agent_addresses,
        filter_all=False,
        target_state=target_state,
        mngr_ctx=mngr_ctx,
    )

    if not matched_agents:
        if is_restart:
            _output("No agents found matching the given addresses", output_opts)
        else:
            _output("No stopped agents found to start", output_opts)
        return

    # Handle dry-run mode: report what would be started without touching any hosts.
    if opts.dry_run:
        verb = "restarted" if is_restart else "started"
        _output(f"Would be {verb}:", output_opts)
        for match in matched_agents:
            _output(f"  - {match.agent_name} (on host {match.host_id})", output_opts)
        return

    started_agents: list[str] = []
    last_started_agent = None
    last_started_host = None

    # Group agents by host to avoid starting the same host multiple times
    agents_by_host = group_agents_by_host(matched_agents)

    for host_key, agent_list in agents_by_host.items():
        host_id_str, _ = host_key.split(":", 1)
        # Get provider from first agent (all agents in list have same provider)
        provider_name = agent_list[0].provider_name

        provider = get_provider_instance(provider_name, mngr_ctx)
        host = provider.get_host(HostId(host_id_str))

        # Ensure host is started (always start since this is the start command).
        # start_host is idempotent (returns early if the host is already running),
        # so concurrent starts do not need to coordinate around this step.
        online_host, _ = ensure_host_started(host, is_start_desired=True, provider=provider)

        agent_ids = [match.agent_id for match in agent_list]

        # Serialize agent (re)launch against any other operation on this host via
        # the shared host lock -- e.g. the minds desktop client (remote, over SSH)
        # racing a VM/container boot hook (local), or a concurrent `mngr gc`. The
        # lock blocks indefinitely; once it is held, an already-running agent's
        # launch is a no-op (the start command exits early when its tmux session
        # exists), so the loser cleanly does nothing.
        with online_host.lock_cooperatively(timeout_seconds=None):
            # gc shares this lock, so a gc that tore down the host/agent is now
            # serialized before us. Lightweight check (just the agent state dir,
            # not a full host revalidation) so a doomed start fails with a clear
            # error instead of trying to boot an agent gc already removed.
            for match in agent_list:
                agent_state_dir = get_agent_state_dir_path(online_host.host_dir, match.agent_id)
                if not online_host.path_exists(agent_state_dir):
                    raise AgentNotFoundOnHostError(match.agent_id, online_host.id)

            # Stop agents first when restarting
            if is_restart:
                with log_span("Stopping {} agent(s) for restart", len(agent_ids)):
                    online_host.stop_agents(agent_ids)

            # Start agents on this host
            with log_span("Starting {} agent(s)", len(agent_ids)):
                online_host.start_agents(agent_ids)

        # Emit discovery events for agents and host
        emit_discovery_events_for_host(mngr_ctx.config, online_host)

        for match in agent_list:
            verb = "Restarted" if is_restart else "Started"
            started_agents.append(str(match.agent_name))
            _output(f"{verb} agent: {match.agent_name}", output_opts)

            # Get the agent object for potential connect and resume message
            for agent in online_host.get_agents():
                if agent.id == match.agent_id:
                    if send_resume:
                        _send_resume_message_if_configured(agent, output_opts)

                    # Track for potential connect
                    if opts.connect:
                        last_started_agent = agent
                        last_started_host = online_host
                    break

    # Output final result
    _output_result(started_agents, output_opts, is_restart=is_restart)

    # Connect if requested and we started exactly one agent
    _maybe_connect(opts, last_started_agent, last_started_host, mngr_ctx)


def _maybe_connect(
    opts: StartCliOptions,
    last_started_agent: AgentInterface | None,
    last_started_host: OnlineHostInterface | None,
    mngr_ctx: MngrContext,
) -> None:
    if not opts.connect or last_started_agent is None or last_started_host is None:
        return

    resolved_command = resolve_connect_command(opts.connect_command, mngr_ctx)
    if resolved_command is not None:
        session_name = last_started_agent.session_name
        run_connect_command(
            resolved_command,
            str(last_started_agent.name),
            session_name,
            is_local=last_started_host.is_local,
        )
    else:
        connection_opts = ConnectionOptions(
            is_reconnect=True,
            retry_count=mngr_ctx.config.retry.connect_retry_times,
            retry_delay=mngr_ctx.config.retry.connect_retry_delay,
            session_command=None,
            is_unknown_host_allowed=False,
        )
        logger.info("Connecting to agent: {}", last_started_agent.name)
        connect_to_agent(last_started_agent, last_started_host, mngr_ctx, connection_opts)


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="start",
    one_line_description="Start stopped agent(s)",
    synopsis="mngr start [AGENTS...|-] [--agent <AGENT>] [--host <HOST>] [--restart] [--no-resume] [--connect] [--dry-run]",
    description="""For remote hosts, this restores from the most recent snapshot and starts
the container/instance. For local agents, this starts the agent's tmux
session.

If multiple agents share a host, they will all be started together when
the host starts.

Use --restart to stop any running agents first, ensuring a clean start.
Use --no-resume to skip sending the resume message after starting.

Use '-' in place of agent names to read them from stdin, one per line.

Supports custom format templates via --format. Available fields: name.""",
    aliases=(),
    examples=(
        ("Start an agent by name", "mngr start my-agent"),
        ("Start multiple agents", "mngr start agent1 agent2"),
        ("Restart a running agent cleanly", "mngr start my-agent --restart"),
        ("Start and connect", "mngr start my-agent --connect"),
        ("Start all stopped agents", "mngr list --ids | mngr start -"),
        ("Preview what would be started", "mngr list --ids | mngr start - --dry-run"),
        ("Custom format template output", "mngr start agent1 agent2 --format '{name}'"),
    ),
    see_also=(
        ("stop", "Stop running agents"),
        ("connect", "Connect to an agent"),
        ("list", "List existing agents"),
    ),
).register()

# Add pager-enabled help option to the start command
add_pager_help_option(start)
