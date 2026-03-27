import sys
from collections.abc import Sequence

import click

from imbue.imbue_common.pure import pure
from imbue.mngr.cli.create import create as create_cmd
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option


@pure
def _build_create_args(
    source_agent: str,
    remaining: list[str],
    original_argv: list[str],
) -> list[str]:
    """Build the argument list for the create command, re-inserting ``--`` if needed.

    Click's ``UNPROCESSED`` type silently strips the ``--`` end-of-options
    separator before the args reach the command function.  We inspect
    *original_argv* (typically ``sys.argv``) to detect whether the user
    supplied ``--`` and, if so, re-insert it at the correct position so that
    downstream commands (e.g. ``create``) see it.
    """
    prefix = ["--from-agent", source_agent]

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


def _args_before_dd_count(remaining: list[str], original_argv: list[str]) -> int | None:
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
) -> str:
    """Validate args, reject conflicting options, and delegate to the create command.

    Returns the source agent name so callers (e.g. migrate) can use it for
    follow-up steps.

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

    before_dd = _args_before_dd_count(remaining, original_argv)
    _reject_source_agent_options(remaining, ctx, before_dd)

    create_args = _build_create_args(source_agent, remaining, original_argv)

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


CommandHelpMetadata(
    key="clone",
    one_line_description="Create a new agent by cloning an existing one [experimental]",
    synopsis="mngr clone <SOURCE_AGENT> [<AGENT_NAME>] [create-options...]",
    description="""This is a convenience wrapper around `mngr create --from-agent <source>`.
The first argument is the source agent to clone from. An optional second
positional argument sets the new agent's name. All remaining arguments are
passed through to the create command.""",
    examples=(
        ("Clone an agent with auto-generated name", "mngr clone my-agent"),
        ("Clone with a specific name", "mngr clone my-agent new-agent"),
        ("Clone into a Docker container", "mngr clone my-agent --provider docker"),
        ("Clone and pass args to the agent", "mngr clone my-agent -- --model opus"),
    ),
    see_also=(
        ("create", "Create an agent (full option set)"),
        ("list", "List existing agents"),
    ),
).register()
add_pager_help_option(clone)
