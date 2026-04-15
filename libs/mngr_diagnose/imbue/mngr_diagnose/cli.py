from pathlib import Path
from typing import Final

import click
from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.create import create as create_cmd
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.issue_reporting import get_mngr_version
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr_diagnose.clone import ensure_mngr_clone
from imbue.mngr_diagnose.context_file import read_diagnose_context
from imbue.mngr_diagnose.prompt import build_diagnose_initial_message

DIAGNOSE_CLONE_DIR: Final[Path] = Path("/tmp/mngr-diagnose")


class DiagnoseCliOptions(CommonCliOptions):
    """CLI options for the diagnose command."""

    description: str | None
    clone_dir: str | None
    context_file: str | None
    agent_type: str | None


@pure
def _build_description_from_context(error_type: str | None, error_message: str | None) -> str | None:
    """Build a fallback description from context error fields.

    Returns None if neither field is present.
    """
    if error_type is not None and error_message is not None:
        return f"{error_type}: {error_message}"
    return error_type or error_message


@click.command()
@click.argument("description", required=False, default=None)
@click.option(
    "--clone-dir",
    type=click.Path(),
    default=None,
    help=f"Clone location [default: {DIAGNOSE_CLONE_DIR}]",
)
@click.option(
    "--context-file",
    type=click.Path(exists=True),
    default=None,
    help="JSON file with error context (written by error handler)",
)
@click.option(
    "--type",
    "agent_type",
    default=None,
    help="Agent type [default: from config]",
)
@add_common_options
@click.pass_context
def diagnose(ctx: click.Context, **kwargs: object) -> None:
    """Launch an agent to diagnose a bug and prepare a GitHub issue.

    Clones the mngr repo (or reuses an existing clone) and creates an agent
    in a worktree to investigate the problem.
    """
    mngr_ctx, _output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="diagnose",
        command_class=DiagnoseCliOptions,
    )

    resolved_clone_dir = Path(opts.clone_dir) if opts.clone_dir is not None else DIAGNOSE_CLONE_DIR

    # Read context file if provided
    traceback_str: str | None = None
    mngr_version = get_mngr_version()

    if opts.context_file is not None:
        context = read_diagnose_context(Path(opts.context_file))
        traceback_str = context.traceback_str
        mngr_version = context.mngr_version
        # Use error info as description if no explicit description given
        if opts.description is None:
            description = _build_description_from_context(context.error_type, context.error_message)
        else:
            description = opts.description
    else:
        description = opts.description

    # Clone or update the repo
    ensure_mngr_clone(resolved_clone_dir, mngr_ctx.concurrency_group)

    # Build the diagnostic message
    message = build_diagnose_initial_message(
        description=description,
        traceback_str=traceback_str,
        mngr_version=mngr_version,
    )

    logger.info("Launching diagnostic agent...")

    # Build create command args -- pass -y to auto-trust the clone directory
    create_args: list[str] = [
        "--from",
        f":{resolved_clone_dir}",
        "--transfer",
        "git-worktree",
        "--branch",
        "main:",
        "--message",
        message,
        "--no-ensure-clean",
    ]
    if opts.agent_type is not None:
        create_args.extend(["--type", opts.agent_type])

    create_ctx = create_cmd.make_context("diagnose", create_args, parent=ctx)
    with create_ctx:
        create_cmd.invoke(create_ctx)


CommandHelpMetadata(
    key="diagnose",
    one_line_description="Launch an agent to diagnose a bug and prepare a GitHub issue",
    synopsis="mngr diagnose [DESCRIPTION] [--context-file PATH] [--clone-dir PATH] [--type TYPE]",
    description="""Launch a diagnostic agent that investigates a bug in the mngr codebase.

The agent works in a worktree of a local clone of the mngr repository
(cloned to --clone-dir, default /tmp/mngr-diagnose). It analyzes the
error, finds the root cause, and prepares a GitHub issue for user review.

Provide a description as a positional argument, a --context-file written
by the error handler, or both. If neither is provided, the agent will
ask the user for details interactively.""",
    examples=(
        ("Diagnose a described problem", 'mngr diagnose "create fails with spaces in path"'),
        ("Diagnose from error context", "mngr diagnose --context-file /tmp/mngr-diagnose-context-abc123.json"),
        ("Both description and context", 'mngr diagnose "spaces bug" --context-file /tmp/ctx.json'),
    ),
    see_also=(("create", "Create an agent (full option set)"),),
).register()
add_pager_help_option(diagnose)
