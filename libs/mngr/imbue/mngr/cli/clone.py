from collections.abc import Sequence

import click

from imbue.mngr.cli.create import create as create_cmd
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option


def parse_source_and_invoke_create(
    ctx: click.Context,
    args: tuple[str, ...],
    command_name: str,
) -> str:
    """Validate args, reject conflicting options, and delegate to the create command.

    Returns the source agent name so callers (e.g. migrate) can use it for
    follow-up steps. ``args`` is the tuple captured by click's
    ``nargs=-1, type=UNPROCESSED`` argument. With ``allow_interspersed_args``
    disabled, click preserves the ``--`` end-of-options separator in the tuple,
    so we forward it verbatim to ``create``.
    """
    if len(args) == 0:
        raise click.UsageError("Missing required argument: SOURCE_AGENT", ctx=ctx)

    source_agent = args[0]
    remaining = list(args[1:])

    before_dd = remaining.index("--") if "--" in remaining else None
    _reject_source_agent_options(remaining, ctx, before_dd)

    create_args = ["--from", source_agent, *remaining]

    create_ctx = create_cmd.make_context(command_name, create_args, parent=ctx)
    with create_ctx:
        create_cmd.invoke(create_ctx)

    return source_agent


@click.command(
    context_settings={"allow_interspersed_args": False, "ignore_unknown_options": True},
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
    """Raise an error if --from or --source appears before ``--``.

    *before_dd* is the number of items in *args* that precede the ``--``
    separator.  When ``None`` (no ``--`` was present), all items are checked.
    """
    check = args if before_dd is None else args[:before_dd]
    for arg in check:
        # Check exact match and --opt=value forms
        if arg in ("--from", "--source") or arg.startswith(("--from=", "--source=")):
            raise click.UsageError(
                f"Cannot use {arg.split('=')[0]} with {ctx.info_name}. "
                "The source agent is specified as the first positional argument.",
                ctx=ctx,
            )


CommandHelpMetadata(
    key="clone",
    one_line_description="Create a new agent by cloning an existing one [experimental]",
    synopsis="mngr clone <SOURCE_AGENT> [<AGENT_NAME>] [create-options...]",
    description="""This is a convenience wrapper around `mngr create --from <source>`.
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
