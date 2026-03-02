import sys
from collections.abc import Sequence

import click

from imbue.imbue_common.pure import pure
from imbue.mngr.cli.create import create as create_cmd
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.help_formatter import register_help_metadata


@pure
def has_name_in_remaining_args(
    remaining: list[str],
    before_dd_count: int | None,
) -> bool:
    """Detect whether *remaining* already contains a name for the new agent.

    A name is present when any of these conditions hold (looking only at args
    before the ``--`` separator):

    * The ``--name`` or ``-n`` flag appears.
    * The first element is a positional argument (does not start with ``-``),
      which the ``create`` command would consume as ``positional_name``.
    """
    check = remaining if before_dd_count is None else remaining[:before_dd_count]

    for arg in check:
        if arg in ("--name", "-n") or arg.startswith(("--name=", "-n=")):
            return True

    if check and not check[0].startswith("-"):
        return True

    return False


@pure
def _build_create_args(
    source_agent: str,
    remaining: list[str],
    original_argv: list[str],
    *,
    preserve_name: bool = False,
) -> list[str]:
    """Build the argument list for the create command, re-inserting ``--`` if needed.

    Click's ``UNPROCESSED`` type silently strips the ``--`` end-of-options
    separator before the args reach the command function.  We inspect
    *original_argv* (typically ``sys.argv``) to detect whether the user
    supplied ``--`` and, if so, re-insert it at the correct position so that
    downstream commands (e.g. ``create``) see it.

    When *preserve_name* is True and no explicit agent name is present in
    *remaining*, the source agent's name is forwarded via ``--name``.  This
    is used by ``migrate`` (where the source is stopped first, freeing the
    tmux session name).  ``clone`` leaves *preserve_name* as False so the
    clone gets an auto-generated name, avoiding tmux session name collisions
    with the still-running source.
    """
    prefix = ["--from-agent", source_agent]
    if preserve_name:
        before_dd_count = args_before_dd_count(remaining, original_argv)
        has_name = has_name_in_remaining_args(remaining, before_dd_count)
        if not has_name:
            prefix = prefix + ["--name", source_agent]

    if "--" not in original_argv:
        return prefix + remaining

    dd_index = original_argv.index("--")
    args_after_dd = len(original_argv) - dd_index - 1

    if args_after_dd > 0 and args_after_dd <= len(remaining):
        before_dd = remaining[: len(remaining) - args_after_dd]
        after_dd = remaining[len(remaining) - args_after_dd :]
        return prefix + before_dd + ["--"] + after_dd

    # -- was present but nothing came after it
    return prefix + remaining + ["--"]


def args_before_dd_count(remaining: list[str], original_argv: list[str]) -> int | None:
    """Return the number of items in *remaining* that came before ``--``.

    Returns ``None`` when ``--`` was not present in *original_argv*.
    """
    if "--" not in original_argv:
        return None

    dd_index = original_argv.index("--")
    args_after_dd = len(original_argv) - dd_index - 1

    if args_after_dd > 0 and args_after_dd <= len(remaining):
        return len(remaining) - args_after_dd
    return len(remaining)


def parse_source_and_invoke_create(
    ctx: click.Context,
    args: tuple[str, ...],
    command_name: str,
    original_argv: list[str] | None = None,
    *,
    preserve_name: bool = False,
) -> str:
    """Validate args, reject conflicting options, and delegate to the create command.

    Returns the source agent name so callers (e.g. migrate) can use it for
    follow-up steps.

    When *preserve_name* is True, the source agent's name is forwarded to the
    create command (unless the user already specified a name).

    *original_argv* defaults to ``sys.argv`` when ``None``.  Passing an
    explicit value allows tests (where ``sys.argv`` is not updated by Click's
    ``CliRunner``) to exercise the ``--`` re-insertion logic.
    """
    if len(args) == 0:
        raise click.UsageError("Missing required argument: SOURCE_AGENT", ctx=ctx)

    source_agent = args[0]
    remaining = list(args[1:])

    if original_argv is None:
        original_argv = sys.argv

    before_dd = args_before_dd_count(remaining, original_argv)
    _reject_source_agent_options(remaining, ctx, before_dd)

    create_args = _build_create_args(source_agent, remaining, original_argv, preserve_name=preserve_name)

    create_ctx = create_cmd.make_context(command_name, create_args, parent=ctx)
    with create_ctx:
        create_cmd.invoke(create_ctx)

    return source_agent


@click.command(
    context_settings={"ignore_unknown_options": True},
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def clone(ctx: click.Context, args: tuple[str, ...]) -> None:
    """Create a new agent by cloning an existing one.

    \b
    This is a convenience wrapper around `mngr create --from-agent <source>`.
    All create options are supported.
    """
    parse_source_and_invoke_create(ctx, args, command_name="clone")


def _reject_source_agent_options(
    args: Sequence[str],
    ctx: click.Context,
    before_dd: int | None = None,
) -> None:
    """Raise an error if --from-agent or --source-agent appears before ``--``.

    *before_dd* is the number of items in *args* that precede the ``--``
    separator.  When ``None`` (no ``--`` was present), all items are checked.
    """
    check = args if before_dd is None else args[:before_dd]
    for arg in check:
        # Check exact match and --opt=value forms
        if arg in ("--from-agent", "--source-agent") or arg.startswith(("--from-agent=", "--source-agent=")):
            raise click.UsageError(
                f"Cannot use {arg.split('=')[0]} with {ctx.info_name}. "
                "The source agent is specified as the first positional argument.",
                ctx=ctx,
            )


_CLONE_HELP_METADATA = CommandHelpMetadata(
    name="mngr-clone",
    one_line_description="Create a new agent by cloning an existing one",
    synopsis="mngr clone <SOURCE_AGENT> [<AGENT_NAME>] [create-options...]",
    description="""Create a new agent by cloning an existing one.

This is a convenience wrapper around `mngr create --from-agent <source>`.
The first argument is the source agent to clone from. An optional second
positional argument sets the new agent's name. All remaining arguments are
passed through to the create command.""",
    examples=(
        ("Clone an agent with auto-generated name", "mngr clone my-agent"),
        ("Clone with a specific name", "mngr clone my-agent new-agent"),
        ("Clone into a Docker container", "mngr clone my-agent --in docker"),
        ("Clone and pass args to the agent", "mngr clone my-agent -- --model opus"),
    ),
    see_also=(
        ("create", "Create an agent (full option set)"),
        ("list", "List existing agents"),
    ),
)

register_help_metadata("clone", _CLONE_HELP_METADATA)
add_pager_help_option(clone)
