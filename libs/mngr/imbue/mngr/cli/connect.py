from typing import Any

import click
from click_option_group import optgroup
from loguru import logger

from imbue.mngr.api.connect import connect_to_agent
from imbue.mngr.api.connect import resolve_connect_command
from imbue.mngr.api.connect import run_connect_command
from imbue.mngr.api.data_types import ConnectionOptions
from imbue.mngr.api.find import resolve_to_started_host_and_running_agent
from imbue.mngr.cli.address_params import AGENT_ADDRESS
from imbue.mngr.cli.agent_utils import find_agent_by_address_or_interactively
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.filter_opts import AgentFilterCliOptions
from imbue.mngr.cli.filter_opts import add_agent_filter_options
from imbue.mngr.cli.filter_opts import build_agent_filter_cel
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.primitives import AgentAddress


class ConnectCliOptions(AgentFilterCliOptions, CommonCliOptions):
    """Options passed from the CLI to the connect command.

    Inherits common options from CommonCliOptions and the shared agent filter
    flags from AgentFilterCliOptions. Filter flags only narrow the candidate
    pool of the interactive selector; they are ignored when an explicit
    agent is given.
    """

    agent: AgentAddress | None
    start: bool
    reconnect: bool
    session_command: str | None
    connect_command: str | None
    allow_unknown_host: bool


def _check_connect_future_options(opts: ConnectCliOptions) -> None:
    """Raise NotImplementedError for unimplemented connect options.

    Mirrors the `[future]` suffix carried on each option's `--help` text;
    pinned by `test_future_flags_raise_not_implemented_error` so removing
    a raise (i.e. shipping the feature) forces a synopsis update.
    """
    # Run this command instead of the default tmux attach.
    if opts.session_command is not None:
        raise NotImplementedError("--session-command is not implemented yet")

    # Disable automatic reconnection if the connection is dropped.
    # Default behavior (--reconnect) should automatically reconnect.
    if not opts.reconnect:
        raise NotImplementedError("--no-reconnect is not implemented yet")


@click.command()
@click.argument("agent", type=AGENT_ADDRESS, default=None, required=False)
@optgroup.group("General")
@optgroup.option(
    "--agent", "agent", type=AGENT_ADDRESS, help="The agent to connect to (by name or ID, optionally @HOST[.PROVIDER])"
)
@optgroup.option(
    "--start/--no-start",
    default=True,
    show_default=True,
    help="Automatically start the host and agent if offline/stopped",
)
@optgroup.group("Options")
@optgroup.option(
    "--reconnect/--no-reconnect",
    default=True,
    show_default=True,
    help="Automatically reconnect if dropped [future]",
)
@optgroup.option("--session-command", help="Command to run instead of attaching to main session [future]")
@optgroup.option(
    "--connect-command",
    help="Command to run instead of the builtin connect. MNGR_AGENT_NAME and MNGR_SESSION_NAME env vars are set.",
)
@optgroup.option(
    "--allow-unknown-host/--no-allow-unknown-host",
    "allow_unknown_host",
    default=False,
    show_default=True,
    help="Allow connecting to hosts without a known_hosts file (disables SSH host key verification)",
)
@add_agent_filter_options
@add_common_options
@click.pass_context
def connect(ctx: click.Context, **kwargs: Any) -> None:
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="connect",
        command_class=ConnectCliOptions,
    )

    _check_connect_future_options(opts)

    logger.info("Finding agent...")

    include_filters, exclude_filters = build_agent_filter_cel(
        opts, mngr_ctx.concurrency_group, project_root=mngr_ctx.project_root
    )
    host_ref, agent_ref = find_agent_by_address_or_interactively(
        mngr_ctx=mngr_ctx,
        address=opts.agent,
        host_filter=None,
        include_filters=include_filters,
        exclude_filters=exclude_filters,
    )

    agent, host = resolve_to_started_host_and_running_agent(
        host_ref=host_ref,
        agent_ref=agent_ref,
        allow_auto_start=opts.start,
        mngr_ctx=mngr_ctx,
    )

    logger.info("Connecting to agent: {}", agent.name)

    # A custom connect command (from --connect-command or config) replaces the
    # builtin tmux attach, mirroring how create/start honor connect_command.
    resolved_connect_command = resolve_connect_command(opts.connect_command, mngr_ctx)
    if resolved_connect_command is not None:
        session_name = agent.session_name
        run_connect_command(
            resolved_connect_command,
            str(agent.name),
            session_name,
            is_local=host.is_local,
        )
        return

    # Build connection options
    connection_opts = ConnectionOptions(
        is_reconnect=opts.reconnect,
        retry_count=mngr_ctx.config.retry.connect_retry_times,
        retry_delay=mngr_ctx.config.retry.connect_retry_delay,
        session_command=opts.session_command,
        is_unknown_host_allowed=opts.allow_unknown_host,
    )

    connect_to_agent(agent, host, mngr_ctx, connection_opts)


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="connect",
    one_line_description="Connect to an existing agent via the terminal",
    synopsis="mngr [connect|conn] [AGENT] [--agent <AGENT>] [--[no-]start] [--connect-command <CMD>] [--[no-]allow-unknown-host]",
    description="""Attaches to the agent's tmux session, roughly equivalent to SSH'ing into
the agent's machine and attaching to the tmux session.

If no agent is specified, shows an interactive selector to choose from
available agents. The selector allows typeahead search to filter agents
by name.

The agent can be specified as a positional argument or via --agent:
  mngr connect my-agent
  mngr connect --agent my-agent

Filter flags (--include/--exclude plus aliases like --running, --project,
--label, ...) narrow the candidate pool of the interactive selector.
They are ignored when an explicit agent is given. See `mngr list --help`
for the full filter reference; the same flags work identically here.""",
    aliases=("conn",),
    examples=(
        ("Connect to an agent by name", "mngr connect my-agent"),
        ("Connect without auto-starting if stopped", "mngr connect my-agent --no-start"),
        ("Show interactive agent selector", "mngr connect"),
        ("Selector limited to running agents on a project", "mngr connect --running --project my-project"),
    ),
    see_also=(
        ("create", "Create and connect to a new agent"),
        ("list", "List agents (full filter flag reference lives here)"),
    ),
).register()

# Add pager-enabled help option to the connect command
add_pager_help_option(connect)
