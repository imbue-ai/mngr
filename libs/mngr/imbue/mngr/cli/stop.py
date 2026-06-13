from collections.abc import Sequence
from concurrent.futures import Future
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger

from imbue.mngr.api.discovery_events import ResolvedAgentHost
from imbue.mngr.api.discovery_events import emit_discovery_events_for_host
from imbue.mngr.api.discovery_events import resolve_hosts_for_identifiers
from imbue.mngr.api.find import AgentMatch
from imbue.mngr.api.find import find_all_agents
from imbue.mngr.api.find import group_agents_by_host
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.cli.address_params import AGENT_ADDRESS
from imbue.mngr.cli.address_params import parse_agent_addresses_or_raise
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.destroy import get_agent_name_from_session
from imbue.mngr.cli.exit_codes import exit_code_for_failures
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.label import apply_labels
from imbue.mngr.cli.output_helpers import emit_event
from imbue.mngr.cli.output_helpers import emit_format_template_lines
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.output_helpers import write_json_line
from imbue.mngr.cli.stdin_utils import STDIN_PLACEHOLDER
from imbue.mngr.cli.stdin_utils import expand_stdin_placeholder
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.errors import AgentNotFoundError
from imbue.mngr.errors import HostOfflineError
from imbue.mngr.errors import HostShutdownNotSupportedError
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.cleanup_failures import CleanupFailedGroup
from imbue.mngr.interfaces.data_types import CleanupFailure
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import HostAddress
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.providers.base_provider import BaseProviderInstance
from imbue.mngr.utils.thread_cleanup import mngr_executor


class StopCliOptions(CommonCliOptions):
    """Options passed from the CLI to the stop command."""

    agents: tuple[str, ...]
    agent_list: tuple[AgentAddress, ...]
    archive: bool
    sessions: tuple[str, ...]
    stop_host: bool
    dry_run: bool
    # Planned features (not yet implemented)
    snapshot_mode: str | None
    graceful: bool
    graceful_timeout: str | None


def _ensure_providers_support_host_shutdown(providers: Sequence[BaseProviderInstance]) -> None:
    """Raise HostShutdownNotSupportedError if any provider cannot stop hosts."""
    for provider in providers:
        if not provider.supports_shutdown_hosts:
            raise HostShutdownNotSupportedError(provider.name)


def _stop_hosts_for_addresses(
    agent_addresses: Sequence[AgentAddress],
    mngr_ctx: MngrContext,
    output_opts: OutputOptions,
) -> list[str]:
    """Stop the entire host of each agent address, resolving the host without SSH.

    ``mngr stop --stop-host`` is a daemon-level operation: stopping a host does
    not require enumerating the agents on it (which would SSH into the host).
    This resolves each agent identifier to its ``host_id`` via the SSH-free
    discovery event stream, then fetches the host through the provider's own
    (also SSH-free) ``get_host`` -- which validates that the host still exists
    and supplies its name -- so it works even when the container is running but
    its sshd is unreachable.

    Returns the list of agent identifiers whose host was stopped (or was
    already stopped).
    """
    resolved_by_identifier = resolve_hosts_for_identifiers(mngr_ctx, [str(addr.agent) for addr in agent_addresses])

    # Fetch each distinct host once (SSH-free) -- this is also what validates
    # the resolved host still exists. Honor any explicit @HOST[.PROVIDER]
    # qualifier against the fetched host's name, mirroring the non-stop-host
    # path.
    hosts_to_stop: dict[HostId, tuple[ResolvedAgentHost, HostInterface]] = {}
    for address in agent_addresses:
        resolved = resolved_by_identifier[str(address.agent)]
        if resolved.host_id in hosts_to_stop:
            host = hosts_to_stop[resolved.host_id][1]
        else:
            host = get_provider_instance(resolved.provider_name, mngr_ctx).get_host(resolved.host_id)
        if address.host is not None:
            concrete = HostAddress(host=host.get_name(), provider=resolved.provider_name)
            if not address.host.matches(concrete):
                raise AgentNotFoundError(f"No agent found matching address: {address}")
        hosts_to_stop[resolved.host_id] = (resolved, host)

    providers = [get_provider_instance(resolved.provider_name, mngr_ctx) for resolved, _ in hosts_to_stop.values()]
    _ensure_providers_support_host_shutdown(providers)

    # Each stop_host is an independent, network-bound daemon operation, so run
    # them concurrently rather than serializing on the slowest host. Futures are
    # iterated in submission order so output (and any re-raised exception)
    # remains deterministic regardless of completion order.
    #
    # Note this changes partial-failure behavior versus the old sequential loop:
    # the executor's context manager joins every submitted task before exit, so
    # *all* targeted hosts are stopped even if one raises -- only the output (and
    # the first re-raised error) stops at the failing future. The old loop
    # aborted on the first failure, leaving later hosts running. Stopping every
    # targeted host is the desired end state here, so this is an improvement.
    futures: list[Future[str]] = []
    with mngr_executor(parent_cg=mngr_ctx.concurrency_group, name="stop_hosts", max_workers=32) as executor:
        for resolved, host in hosts_to_stop.values():
            futures.append(executor.submit(_stop_single_host, resolved, host, mngr_ctx))

    for future in futures:
        _output(future.result(), output_opts)

    return [str(address.agent) for address in agent_addresses]


def _stop_single_host(
    resolved: ResolvedAgentHost,
    host: HostInterface,
    mngr_ctx: MngrContext,
) -> str:
    """Stop a single resolved host, returning the human-readable status message.

    Online hosts are stopped via the provider; hosts that are already offline
    are treated as an idempotent no-op (the desired end state is reached).
    """
    provider = get_provider_instance(resolved.provider_name, mngr_ctx)
    match host:
        case OnlineHostInterface() as online_host:
            # No snapshot: native start_host preserves the container filesystem
            # anyway. No discovery-event emission: the host is offline
            # afterwards, so an online-host sweep would fail.
            provider.stop_host(online_host, create_snapshot=False)
            return f"Stopped host: {host.get_name()}"
        case HostInterface():
            # The host is already offline (stopped or destroyed) -- the desired
            # end state is already reached, so this is an idempotent no-op.
            return f"Host '{host.get_name()}' is already stopped"
        case _ as unreachable:
            assert_never(unreachable)


def _output(message: str, output_opts: OutputOptions) -> None:
    """Output a message according to the format."""
    if output_opts.output_format == OutputFormat.HUMAN:
        write_human_line(message)


def _output_result(
    stopped_agents: Sequence[str],
    failures: Sequence[CleanupFailure],
    output_opts: OutputOptions,
) -> None:
    """Output the final result, including any real cleanup failures.

    Mirrors ``destroy._output_result``: JSON/JSONL output carries the structured
    ``failures`` list and the cause-specific ``exit_code`` so non-interactive
    consumers can see which resources were left behind (see
    specs/cleanup-error-aggregation.md), not just the process exit code.
    """
    if output_opts.format_template is not None:
        items = [{"name": name} for name in stopped_agents]
        emit_format_template_lines(output_opts.format_template, items)
        return
    result_data = {
        "stopped_agents": list(stopped_agents),
        "count": len(stopped_agents),
        "failures": [failure.model_dump(mode="json") for failure in failures],
        "failure_count": len(failures),
        "exit_code": exit_code_for_failures(failures),
    }
    match output_opts.output_format:
        case OutputFormat.JSON:
            write_json_line(result_data)
        case OutputFormat.JSONL:
            emit_event("stop_result", result_data, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            if stopped_agents:
                write_human_line("Successfully stopped {} agent(s)", len(stopped_agents))
            if failures:
                logger.warning("{} cleanup failure(s) -- resources may remain:", len(failures))
                for failure in failures:
                    logger.warning("  - [{}] {}", failure.category.value, failure.message)
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
    "--dry-run",
    is_flag=True,
    help="Show what would be stopped without actually stopping anything",
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

    # --stop-host stops the agent's whole host directly, without the
    # agent-enumeration scan (see _stop_hosts_for_addresses).
    if opts.stop_host:
        if opts.dry_run:
            _output("Would stop the host(s) of:", output_opts)
            for address in agent_addresses:
                _output(f"  - {address.agent}", output_opts)
            return
        stopped_host_agents = _stop_hosts_for_addresses(agent_addresses, mngr_ctx, output_opts)
        # The --stop-host path raises on a real failure (it does not aggregate
        # CleanupFailures), so there are never any failures to report here.
        _output_result(stopped_host_agents, [], output_opts)
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

    # Dry-run: report what would be stopped without touching any agent.
    if opts.dry_run:
        _output("Would stop:", output_opts)
        for match in agents_to_stop:
            _output(f"  - {match.agent_name} (on host {match.host_id})", output_opts)
        return

    # Stop each agent
    stopped_agents: list[str] = []
    stopped_matches: list[AgentMatch] = []
    # Real cleanup failures (resources left behind); drives the process exit code.
    failures: list[CleanupFailure] = []

    # Group agents by host to stop them together
    agents_by_host = group_agents_by_host(agents_to_stop)

    for host_key, agent_list in agents_by_host.items():
        host_id_str, _ = host_key.split(":", 1)
        # Get provider from first agent (all agents in list have same provider)
        provider_name = agent_list[0].provider_name

        provider = get_provider_instance(provider_name, mngr_ctx)
        host = provider.get_host(HostId(host_id_str))

        # Ensure host is online (can't stop agents on offline hosts)
        match host:
            case OnlineHostInterface() as online_host:
                # Stop each named agent on this host. stop_agents is best-effort: it raises a
                # CleanupFailedGroup carrying the real failures (resources left behind) rather
                # than failing fast.
                agent_ids_to_stop = [m.agent_id for m in agent_list]
                try:
                    online_host.stop_agents(agent_ids_to_stop)
                except CleanupFailedGroup as group:
                    failures.extend(group.failures)

                for m in agent_list:
                    stopped_agents.append(str(m.agent_name))
                    stopped_matches.append(m)
                    _output(f"Stopped agent: {m.agent_name}", output_opts)

                # Emit discovery events for stopped agents and host.
                emit_discovery_events_for_host(mngr_ctx.config, online_host)
            case HostInterface():
                raise HostOfflineError(f"Host '{host_id_str}' is offline. Cannot stop agents on offline hosts.")
            case _ as unreachable:
                assert_never(unreachable)

    # Archive stopped agents if requested
    if opts.archive and stopped_matches:
        now = datetime.now(timezone.utc).isoformat()
        apply_labels(stopped_matches, {"archived_at": now}, mngr_ctx, output_opts)

    # Output final result (including any real cleanup failures), then exit with a
    # cause-specific code (see specs/cleanup-error-aggregation.md).
    _output_result(stopped_agents, failures, output_opts)
    ctx.exit(exit_code_for_failures(failures))


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="stop",
    one_line_description="Stop running agent(s)",
    synopsis="mngr [stop|s] [AGENTS...|-] [--agent <AGENT>] [--session <SESSION>] [--archive] [--stop-host] [--dry-run] [--snapshot-mode <MODE>] [--graceful/--no-graceful]",
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

Use --dry-run to preview which agents (or hosts, with --stop-host) would be
stopped without actually stopping anything.

Use '-' in place of agent names to read them from stdin, one per line.

Supports custom format templates via --format. Available fields: name.""",
    aliases=(),
    examples=(
        ("Stop an agent by name", "mngr stop my-agent"),
        ("Stop multiple agents", "mngr stop agent1 agent2"),
        ("Stop all running agents", "mngr list --ids | mngr stop -"),
        ("Stop and archive an agent", "mngr stop my-agent --archive"),
        ("Stop the agent's whole host", "mngr stop my-agent --stop-host"),
        ("Preview what would be stopped", "mngr list --ids | mngr stop - --dry-run"),
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
