from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import assert_never

import click
from click_option_group import optgroup

from imbue.mngr.api.discovery_events import emit_discovery_events_for_host
from imbue.mngr.api.find import AgentMatch
from imbue.mngr.api.find import find_all_agents
from imbue.mngr.api.find import group_agents_by_host
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.cli.address_params import AGENT_ADDRESS
from imbue.mngr.cli.address_params import parse_agent_addresses_or_raise
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.destroy import get_agent_name_from_session
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.label import apply_labels
from imbue.mngr.cli.output_helpers import emit_event
from imbue.mngr.cli.output_helpers import emit_final_json
from imbue.mngr.cli.output_helpers import emit_format_template_lines
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.stdin_utils import STDIN_PLACEHOLDER
from imbue.mngr.cli.stdin_utils import expand_stdin_placeholder
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.errors import HostOfflineError
from imbue.mngr.errors import HostShutdownNotSupportedError
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.providers.base_provider import BaseProviderInstance


class StopCliOptions(CommonCliOptions):
    """Options passed from the CLI to the stop command."""

    agents: tuple[str, ...]
    agent_list: tuple[AgentAddress, ...]
    archive: bool
    sessions: tuple[str, ...]
    stop_host: bool
    # Planned features (not yet implemented)
    snapshot_mode: str | None
    graceful: bool
    graceful_timeout: str | None


def _ensure_providers_support_host_shutdown(providers: Sequence[BaseProviderInstance]) -> None:
    """Raise HostShutdownNotSupportedError if any provider cannot stop hosts."""
    for provider in providers:
        if not provider.supports_shutdown_hosts:
            raise HostShutdownNotSupportedError(provider.name)


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
    type=AGENT_ADDRESS,
    multiple=True,
    help="Agent address (NAME[@HOST[.PROVIDER]]) to stop (can be specified multiple times)",
)
@optgroup.option(
    "--session",
    "sessions",
    multiple=True,
    help="Tmux session name to stop (can be specified multiple times). The agent name is extracted by "
    "stripping the configured prefix from the session name.",
)
@optgroup.group("Behavior")
@optgroup.option(
    "--archive",
    is_flag=True,
    help="Set an 'archived_at' label on each stopped agent (marks it as archived)",
)
@optgroup.option(
    "--stop-host",
    is_flag=True,
    help="Stop the agent's entire host (all agents on it) instead of just the named agent",
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
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="stop",
        command_class=StopCliOptions,
        is_format_template_supported=True,
    )

    # Check for unsupported [future] options
    if opts.snapshot_mode is not None:
        raise NotImplementedError("--snapshot-mode is not implemented yet")
    if not opts.graceful:
        raise NotImplementedError("--no-graceful is not implemented yet")
    if opts.graceful_timeout is not None:
        raise NotImplementedError("--graceful-timeout is not implemented yet")

    # --archive labels individual stopped agents, which is incompatible with
    # stopping the whole host (which takes down every agent on it, including
    # ones not named on the command line).
    if opts.stop_host and opts.archive:
        raise UserInputError("Cannot use --stop-host together with --archive")

    # Validate input. Variadic positional is parsed here (after stdin expansion);
    # --agent is already typed by Click.
    agent_addresses: list[AgentAddress] = parse_agent_addresses_or_raise(expand_stdin_placeholder(opts.agents)) + list(
        opts.agent_list
    )

    # Handle --session option by extracting agent names from session names
    if opts.sessions:
        if agent_addresses:
            raise UserInputError("Cannot specify --session with agent names")
        for session_name in opts.sessions:
            agent_name = get_agent_name_from_session(session_name, mngr_ctx.config.prefix)
            if agent_name is None:
                raise UserInputError(
                    f"Session '{session_name}' does not match the expected format. "
                    f"Session names should start with the configured prefix '{mngr_ctx.config.prefix}'."
                )
            agent_addresses.append(parse_agent_addresses_or_raise([agent_name])[0])

    if not agent_addresses:
        if STDIN_PLACEHOLDER not in opts.agents:
            raise click.UsageError("Must specify at least one agent (use '-' to read from stdin)")
        return

    # Find agents to stop (RUNNING agents)
    agents_to_stop = find_all_agents(
        addresses=agent_addresses,
        filter_all=False,
        target_state=AgentLifecycleState.RUNNING,
        mngr_ctx=mngr_ctx,
    )

    if not agents_to_stop:
        _output("No running agents found to stop", output_opts)
        return

    # Stop each agent
    stopped_agents: list[str] = []
    stopped_matches: list[AgentMatch] = []

    # Group agents by host to stop them together
    agents_by_host = group_agents_by_host(agents_to_stop)

    # When stopping whole hosts, verify every provider involved supports it
    # before stopping anything, so we don't stop one host and then fail.
    if opts.stop_host:
        _ensure_providers_support_host_shutdown(
            [get_provider_instance(agent_list[0].provider_name, mngr_ctx) for agent_list in agents_by_host.values()]
        )

    for host_key, agent_list in agents_by_host.items():
        host_id_str, _ = host_key.split(":", 1)
        # Get provider from first agent (all agents in list have same provider)
        provider_name = agent_list[0].provider_name

        provider = get_provider_instance(provider_name, mngr_ctx)
        host = provider.get_host(HostId(host_id_str))

        # Ensure host is online (can't stop agents on offline hosts)
        match host:
            case OnlineHostInterface() as online_host:
                if opts.stop_host:
                    # Stop the whole host. This takes down every agent on it,
                    # not just the ones named. No snapshot: native start_host
                    # preserves the container filesystem anyway.
                    provider.stop_host(online_host, create_snapshot=False)
                else:
                    # Stop each named agent on this host
                    agent_ids_to_stop = [m.agent_id for m in agent_list]
                    online_host.stop_agents(agent_ids_to_stop)

                for m in agent_list:
                    stopped_agents.append(str(m.agent_name))
                    stopped_matches.append(m)
                    _output(f"Stopped agent: {m.agent_name}", output_opts)

                # Emit discovery events for stopped agents and host. Skip when
                # the host itself was stopped: the host is offline now, so an
                # online-host discovery sweep would fail.
                if not opts.stop_host:
                    emit_discovery_events_for_host(mngr_ctx.config, online_host)
            case HostInterface():
                if opts.stop_host:
                    # The whole host is the stop target and it is already
                    # offline -- the desired end state (host stopped) is
                    # already reached, so this is an idempotent no-op rather
                    # than an error. A plain ``mngr stop`` of individual
                    # agents still cannot proceed on an offline host.
                    _output(f"Host '{host_id_str}' is already stopped", output_opts)
                else:
                    raise HostOfflineError(
                        f"Host '{host_id_str}' is offline. Cannot stop agents on offline hosts."
                    )
            case _ as unreachable:
                assert_never(unreachable)

    # Archive stopped agents if requested
    if opts.archive and stopped_matches:
        now = datetime.now(timezone.utc).isoformat()
        apply_labels(stopped_matches, {"archived_at": now}, mngr_ctx, output_opts)

    # Output final result
    _output_result(stopped_agents, output_opts)


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="stop",
    one_line_description="Stop running agent(s)",
    synopsis="mngr [stop|s] [AGENTS...|-] [--agent <AGENT>] [--session <SESSION>] [--archive] [--stop-host] [--snapshot-mode <MODE>] [--graceful/--no-graceful]",
    description="""For remote hosts, this stops the agent's tmux session. The host remains
running unless idle detection stops it automatically.

For local agents, this stops the agent's tmux session. The local host
itself cannot be stopped (if you want that, shut down your computer).

Use --stop-host to stop the agent's entire host instead of just the
agent. This takes down every agent on that host. For container-backed
providers it stops the container (the underlying machine keeps running);
it is rejected on providers that do not support stopping hosts.

Use --archive to also set an 'archived_at' label on each stopped agent.
This marks the agent as archived without destroying it, allowing it to
be filtered out of listings while preserving its state. The 'mngr archive'
command is a shorthand for 'mngr stop --archive'.

Use '-' in place of agent names to read them from stdin, one per line.

Supports custom format templates via --format. Available fields: name.""",
    aliases=(),
    examples=(
        ("Stop an agent by name", "mngr stop my-agent"),
        ("Stop multiple agents", "mngr stop agent1 agent2"),
        ("Stop all running agents", "mngr list --ids | mngr stop -"),
        ("Stop and archive an agent", "mngr stop my-agent --archive"),
        ("Stop the agent's whole host", "mngr stop my-agent --stop-host"),
        ("Stop by tmux session name", "mngr stop --session mngr-my-agent"),
        ("Custom format template output", "mngr stop agent1 agent2 --format '{name}'"),
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
