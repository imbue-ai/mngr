import sys

import click
from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.pure import pure
from imbue.mngr.api.list import list_agents
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.cli.clone import args_before_dd_count
from imbue.mngr.cli.clone import has_name_in_remaining_args
from imbue.mngr.cli.clone import parse_source_and_invoke_create
from imbue.mngr.cli.connect import connect as connect_cmd
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.help_formatter import register_help_metadata
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.loader import load_config
from imbue.mngr.errors import AgentNotFoundError
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId


@pure
def _user_specified_no_connect(
    remaining: list[str],
    original_argv: list[str],
) -> bool:
    """Check if the user explicitly passed --no-connect before ``--``.

    Only examines args before the ``--`` separator so that agent args
    (e.g. ``-- --no-connect``) are not misidentified as create options.
    """
    before_dd = args_before_dd_count(remaining, original_argv)
    check = remaining if before_dd is None else remaining[:before_dd]
    return "--no-connect" in check


@pure
def _user_specified_quiet(
    remaining: list[str],
    original_argv: list[str],
) -> bool:
    """Check if the user explicitly passed --quiet or -q before ``--``."""
    before_dd = args_before_dd_count(remaining, original_argv)
    check = remaining if before_dd is None else remaining[:before_dd]
    return "--quiet" in check or "-q" in check


def _resolve_and_stop_source_agent(mngr_ctx: MngrContext, source_agent: str) -> AgentId:
    """Resolve the source agent name or ID to a unique AgentId, then stop it.

    Stopping the source is necessary because tmux session names are derived
    from agent names (not IDs). If the clone inherits the same name, it
    needs the session name to be free so it can create its own session.

    The source's data/config remains intact for the clone to copy from.
    """
    result = list_agents(mngr_ctx, is_streaming=False)

    for agent_info in result.agents:
        if str(agent_info.name) == source_agent or str(agent_info.id) == source_agent:
            provider = get_provider_instance(agent_info.host.provider_name, mngr_ctx)
            host = provider.get_host(agent_info.host.id)
            if isinstance(host, OnlineHostInterface):
                host.stop_agents([agent_info.id])
            return agent_info.id

    raise UserInputError(f"Source agent '{source_agent}' not found")


def _destroy_source_agent_data(mngr_ctx: MngrContext, source_agent_id: AgentId) -> None:
    """Destroy the source agent's data without stopping its tmux session.

    Uses skip_stop=True on destroy_agent because the source was already
    stopped before cloning and the clone now owns the tmux session with
    the same name.
    """
    result = list_agents(mngr_ctx, is_streaming=False)

    for agent_info in result.agents:
        if agent_info.id == source_agent_id:
            provider = get_provider_instance(agent_info.host.provider_name, mngr_ctx)
            host = provider.get_host(agent_info.host.id)
            if isinstance(host, OnlineHostInterface):
                for agent in host.get_agents():
                    if agent.id == source_agent_id:
                        host.destroy_agent(agent, skip_stop=True)
                        return
            raise AgentNotFoundError(str(source_agent_id))

    raise AgentNotFoundError(str(source_agent_id))


@pure
def _determine_new_agent_name(
    source_agent: str,
    remaining: list[str],
    original_argv: list[str],
) -> str:
    """Determine the new agent's name from the args.

    Uses the same logic as _build_create_args: if a name is present in
    remaining args (positional or --name), the create command will use it.
    Otherwise, the source agent name is forwarded.
    """
    before_dd_count = args_before_dd_count(remaining, original_argv)
    has_name = has_name_in_remaining_args(remaining, before_dd_count)

    if not has_name:
        return source_agent

    check = remaining if before_dd_count is None else remaining[:before_dd_count]

    # Check for --name or -n flag
    for i, arg in enumerate(check):
        if arg in ("--name", "-n") and i + 1 < len(check):
            return check[i + 1]
        if arg.startswith("--name="):
            return arg.split("=", 1)[1]
        if arg.startswith("-n="):
            return arg.split("=", 1)[1]

    # First positional arg (not starting with -)
    if check and not check[0].startswith("-"):
        return check[0]

    return source_agent


@click.command(
    context_settings={"ignore_unknown_options": True},
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def migrate(ctx: click.Context, args: tuple[str, ...]) -> None:
    """Move an agent to a different host by cloning it and destroying the original.

    \b
    This is equivalent to running `mngr clone` followed by `mngr destroy --force`.
    All create options are supported. The source agent is force-destroyed after
    a successful clone (including running agents).

    When connecting (the default), the source agent is destroyed as soon as
    the clone is ready, before attaching to the new agent's session.
    """
    if len(args) == 0:
        raise click.UsageError("Missing required argument: SOURCE_AGENT", ctx=ctx)

    source_agent = args[0]
    remaining = list(args[1:])
    original_argv = sys.argv

    # Determine if the user wants to connect (the default).
    # If they do, we inject --no-connect so that create returns after the
    # agent is ready, then we destroy the source, then connect manually.
    wants_connect = not _user_specified_no_connect(remaining, original_argv)
    is_quiet = _user_specified_quiet(remaining, original_argv)

    pm = ctx.obj

    # Resolve the source agent's unique ID and stop its tmux session
    # before cloning. This ensures: (1) destroy targets only this
    # specific agent, not a newly cloned agent with the same name,
    # and (2) the clone can create its own tmux session (session
    # names are name-based, not ID-based).
    #
    # We use __enter__/__exit__(None) instead of a `with` block so that
    # exceptions propagate directly rather than being wrapped in a
    # ConcurrencyExceptionGroup (same pattern as setup_command_context).
    cg = ConcurrencyGroup(name="mngr-migrate")
    cg.__enter__()
    try:
        mngr_ctx = load_config(pm, cg)
        if not is_quiet:
            logger.info("Stopping source agent...")
        source_agent_id = _resolve_and_stop_source_agent(mngr_ctx, source_agent)
    finally:
        cg.__exit__(None, None, None)

    if wants_connect:
        create_args = args + ("--no-connect", "--await-ready")
    else:
        create_args = args

    parse_source_and_invoke_create(ctx, create_args, command_name="migrate", preserve_name=True)

    # Destroy the source agent's data. We use skip_stop=True because
    # the source was already stopped before cloning and the clone now
    # owns the tmux session with the same name.
    cg = ConcurrencyGroup(name="mngr-migrate-destroy")
    cg.__enter__()
    try:
        mngr_ctx = load_config(pm, cg)
        if not is_quiet:
            logger.info("Cleaning up source agent...")
        try:
            _destroy_source_agent_data(mngr_ctx, source_agent_id)
        except click.ClickException:
            logger.error(
                "Clone succeeded but destroy of '{}' failed. "
                "Please manually destroy the source agent:\n"
                "  mngr destroy --force {}",
                source_agent,
                source_agent,
            )
            raise
    finally:
        cg.__exit__(None, None, None)

    # Connect to the new agent if the user wanted to connect
    if wants_connect:
        new_agent_name = _determine_new_agent_name(source_agent, remaining, original_argv)
        connect_ctx = connect_cmd.make_context("migrate-connect", [new_agent_name], parent=ctx)
        with connect_ctx:
            connect_cmd.invoke(connect_ctx)


_MIGRATE_HELP_METADATA = CommandHelpMetadata(
    name="mngr-migrate",
    one_line_description="Move an agent to a different host",
    synopsis="mngr migrate <SOURCE_AGENT> [<AGENT_NAME>] [create-options...]",
    description="""Move an agent to a different host by cloning it and destroying the original.

This is equivalent to running `mngr clone <source>` followed by
`mngr destroy --force <source>`. The first argument is the source agent to
migrate. An optional second positional argument sets the new agent's name.
All remaining arguments are passed through to the create command.

The source agent is always force-destroyed after a successful clone. If the
clone step fails, the source agent is left untouched. If the destroy step
fails after a successful clone, the error is reported and the user can
manually clean up.""",
    examples=(
        ("Migrate an agent to a Docker container", "mngr migrate my-agent --in docker"),
        ("Migrate with a new name", "mngr migrate my-agent new-agent --in modal"),
        ("Migrate and pass args to the agent", "mngr migrate my-agent -- --model opus"),
    ),
    see_also=(
        ("clone", "Clone an agent (without destroying the original)"),
        ("create", "Create an agent (full option set)"),
        ("destroy", "Destroy an agent"),
    ),
)

register_help_metadata("migrate", _MIGRATE_HELP_METADATA)
add_pager_help_option(migrate)
