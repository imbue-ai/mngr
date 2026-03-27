import click
from loguru import logger

from imbue.mngr.cli.clone import parse_source_and_invoke_create
from imbue.mngr.cli.destroy import destroy as destroy_cmd
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option


@click.command(
    context_settings={"ignore_unknown_options": True},
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def migrate(ctx: click.Context, args: tuple[str, ...]) -> None:
    source_agent = parse_source_and_invoke_create(ctx, args, command_name="migrate")

    # Destroy the source agent with --force
    destroy_args = [source_agent, "--force"]
    try:
        destroy_ctx = destroy_cmd.make_context("migrate-destroy", destroy_args, parent=ctx)
        with destroy_ctx:
            destroy_cmd.invoke(destroy_ctx)
    except (click.Abort, click.ClickException):
        logger.error(
            "Clone succeeded but destroy of '{}' failed. "
            "Please manually destroy the source agent:\n"
            "  mngr destroy --force {}",
            source_agent,
            source_agent,
        )
        raise


CommandHelpMetadata(
    key="migrate",
    one_line_description="Move an agent to a different host by cloning and destroying the original [experimental]",
    synopsis="mngr migrate <SOURCE_AGENT> [<AGENT_NAME>] [create-options...]",
    description="""This is equivalent to running `mngr clone <source>` followed by
`mngr destroy --force <source>`. The first argument is the source agent to
migrate. An optional second positional argument sets the new agent's name.
All remaining arguments are passed through to the create command.

The source agent is always force-destroyed after a successful clone. If the
clone step fails, the source agent is left untouched. If the destroy step
fails after a successful clone, the error is reported and the user can
manually clean up.""",
    examples=(
        ("Migrate an agent to a Docker container", "mngr migrate my-agent --provider docker"),
        ("Migrate with a new name", "mngr migrate my-agent new-agent --provider modal"),
        ("Migrate and pass args to the agent", "mngr migrate my-agent -- --model opus"),
    ),
    see_also=(
        ("clone", "Clone an agent (without destroying the original)"),
        ("create", "Create an agent (full option set)"),
        ("destroy", "Destroy an agent"),
    ),
).register()
add_pager_help_option(migrate)
