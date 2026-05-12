"""Implementation of ``mngr restart-host`` for tiered workspace recovery.

This command stops and starts the host(s) hosting the named agent(s).
For providers that support full container/VM lifecycle (docker, imbue_cloud),
this performs a real container/VM bounce via ``provider.stop_host`` +
``provider.start_host``. For providers that cannot stop their host (e.g.
the local provider, where the "host" is the user's machine), the command
falls back to stopping and restarting just the agent processes on the host
(``online_host.stop_agents`` + ``start_agents``), which restarts the
bootstrap manager and all services that live inside the agent's tmux
session.

Used by minds' tiered recovery flow (L2 / container restart) when L1
surgical restart of the workspace-server tmux window is not sufficient
because the container or bootstrap manager itself is wedged.

Snapshots are deliberately not created during this restart: this command
is recovery-oriented, not state-preservation-oriented, and snapshotting
a wedged container can be slow or fail.
"""

from collections.abc import Sequence
from typing import Any
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger

from imbue.imbue_common.logging import log_span
from imbue.mngr.api.agent_addr import find_agents_by_addresses
from imbue.mngr.api.discovery_events import emit_discovery_events_for_host
from imbue.mngr.api.find import group_agents_by_host
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import emit_event
from imbue.mngr.cli.output_helpers import emit_final_json
from imbue.mngr.cli.output_helpers import emit_format_template_lines
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.stdin_utils import STDIN_PLACEHOLDER
from imbue.mngr.cli.stdin_utils import expand_stdin_placeholder
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.errors import HostOfflineError
from imbue.mngr.errors import LocalHostNotStoppableError
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import OutputFormat


class RestartHostCliOptions(CommonCliOptions):
    """Options passed from the CLI to the restart-host command."""

    agents: tuple[str, ...]
    agent_list: tuple[str, ...]


def _output(message: str, output_opts: OutputOptions) -> None:
    if output_opts.output_format == OutputFormat.HUMAN:
        write_human_line(message)


def _output_result(restarted_agents: Sequence[str], output_opts: OutputOptions) -> None:
    if output_opts.format_template is not None:
        items = [{"name": name} for name in restarted_agents]
        emit_format_template_lines(output_opts.format_template, items)
        return
    result_data = {"restarted_agents": restarted_agents, "count": len(restarted_agents)}
    match output_opts.output_format:
        case OutputFormat.JSON:
            emit_final_json(result_data)
        case OutputFormat.JSONL:
            emit_event("restart_host_result", result_data, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            if restarted_agents:
                write_human_line("Successfully restarted host(s) for {} agent(s)", len(restarted_agents))
        case _ as unreachable:
            assert_never(unreachable)


@click.command(name="restart-host")
@click.argument("agents", nargs=-1, required=False)
@optgroup.group("Target Selection")
@optgroup.option(
    "--agent",
    "agent_list",
    multiple=True,
    help="Agent name or ID whose host to restart (can be specified multiple times)",
)
@add_common_options
@click.pass_context
def restart_host(ctx: click.Context, **kwargs: Any) -> None:
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="restart-host",
        command_class=RestartHostCliOptions,
        is_format_template_supported=True,
    )

    agent_identifiers = expand_stdin_placeholder(opts.agents) + list(opts.agent_list)

    if not agent_identifiers:
        if STDIN_PLACEHOLDER not in opts.agents:
            raise click.UsageError("Must specify at least one agent (use '-' to read from stdin)")
        return

    # Match agents in any lifecycle state: this command is called from recovery
    # paths where the agent's reported state may be stale (e.g. tmux session
    # exists but bootstrap is wedged, or container died but mngr hasn't noticed).
    agents_to_restart = find_agents_by_addresses(
        raw_identifiers=agent_identifiers,
        filter_all=False,
        target_state=None,
        mngr_ctx=mngr_ctx,
    )

    if not agents_to_restart:
        _output("No agents found matching the given identifiers", output_opts)
        return

    restarted_agents: list[str] = []
    agents_by_host = group_agents_by_host(agents_to_restart)

    for host_key, agent_list in agents_by_host.items():
        host_id_str, _ = host_key.split(":", 1)
        provider_name = agent_list[0].provider_name
        provider = get_provider_instance(provider_name, mngr_ctx)
        host = provider.get_host(HostId(host_id_str))
        agent_ids = [m.agent_id for m in agent_list]

        # Full host bounce: stop the container/VM and start it back up.
        # Skip snapshotting -- this is recovery, not preservation, and a
        # wedged container may not be snapshottable anyway.
        #
        # The local provider advertises supports_shutdown_hosts=True but
        # raises LocalHostNotStoppableError from stop_host (the "host" is
        # the user's machine, which we obviously can't shut down). Catch
        # that and fall back to bouncing just the agent's tmux session,
        # which restarts the bootstrap manager and all services inside it
        # -- the closest analogue to a container restart available on the
        # local provider.
        try:
            with log_span("Restarting host", host_id=host_id_str, provider=provider_name):
                provider.stop_host(host, create_snapshot=False)
                online_host = provider.start_host(host)
            online_host.start_agents(agent_ids)
        except LocalHostNotStoppableError:
            match host:
                case OnlineHostInterface() as online_host:
                    with log_span(
                        "Restarting agents on un-stoppable host",
                        host_id=host_id_str,
                        provider=provider_name,
                    ):
                        online_host.stop_agents(agent_ids)
                        online_host.start_agents(agent_ids)
                case HostInterface():
                    raise HostOfflineError(
                        f"Host '{host_id_str}' is offline and provider '{provider_name}' "
                        "does not support starting hosts."
                    )
                case _ as unreachable:
                    assert_never(unreachable)

        emit_discovery_events_for_host(mngr_ctx.config, online_host)

        for m in agent_list:
            restarted_agents.append(str(m.agent_name))
            _output(f"Restarted host for agent: {m.agent_name}", output_opts)
        logger.info(
            "Restarted host {} on provider {} for {} agent(s)",
            host_id_str,
            provider_name,
            len(agent_list),
        )

    _output_result(restarted_agents, output_opts)


CommandHelpMetadata(
    key="restart-host",
    one_line_description="Restart the host(s) for the given agent(s)",
    synopsis="mngr restart-host [AGENTS...|-] [--agent <AGENT>]",
    description="""Stop and start the host backing each named agent.

For providers that support host shutdown (Docker, imbue_cloud), this
performs a full container/VM bounce. For providers that cannot stop
their host (e.g. the local provider, where the host is your own
machine), the command falls back to restarting just the agent's
tmux session on the existing host -- which restarts the bootstrap
manager and all services inside it.

Snapshots are not created before the stop (this is a recovery
command, not a state-preservation command).

Use '-' in place of agent names to read them from stdin, one per line.

Supports custom format templates via --format. Available fields: name.""",
    aliases=(),
    examples=(
        ("Restart the host backing an agent", "mngr restart-host my-agent"),
        ("Restart hosts for multiple agents", "mngr restart-host agent1 agent2"),
    ),
    see_also=(
        ("stop", "Stop running agents (without restarting the host)"),
        ("start", "Start stopped agents"),
    ),
).register()

add_pager_help_option(restart_host)
