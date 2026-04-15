import importlib.metadata
from pathlib import Path
from typing import Final

import click
from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.cli.create import create as create_cmd
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr_diagnose.clone import ensure_mngr_clone
from imbue.mngr_diagnose.context_file import read_diagnose_context
from imbue.mngr_diagnose.prompt import build_diagnose_initial_message

DIAGNOSE_CLONE_DIR: Final[Path] = Path("/tmp/mngr-diagnose")


def _get_mngr_version() -> str:
    """Get the installed mngr version, falling back to 'unknown'."""
    try:
        return importlib.metadata.version("imbue-mngr")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


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
@click.pass_context
def diagnose(
    ctx: click.Context,
    description: str | None,
    clone_dir: str | None,
    context_file: str | None,
    agent_type: str | None,
) -> None:
    """Launch an agent to diagnose a bug and prepare a GitHub issue.

    Clones the mngr repo (or reuses an existing clone) and creates an agent
    in a worktree to investigate the problem.
    """
    resolved_clone_dir = Path(clone_dir) if clone_dir is not None else DIAGNOSE_CLONE_DIR

    # Read context file if provided
    traceback_str: str | None = None
    mngr_version = _get_mngr_version()

    if context_file is not None:
        context = read_diagnose_context(Path(context_file))
        traceback_str = context.traceback_str
        mngr_version = context.mngr_version
        # Use error info as description if no explicit description given
        if description is None and context.error_message is not None:
            description = f"{context.error_type}: {context.error_message}"

    # Clone or update the repo
    with ConcurrencyGroup(name="diagnose-clone") as cg:
        ensure_mngr_clone(resolved_clone_dir, cg)

    # Build the diagnostic message
    message = build_diagnose_initial_message(
        description=description,
        traceback_str=traceback_str,
        mngr_version=mngr_version,
    )

    logger.info("Launching diagnostic agent...")

    # Build create command args
    create_args: list[str] = [
        "--from", f":{resolved_clone_dir}",
        "--transfer", "git-worktree",
        "--branch", "main:",
        "--message", message,
        "--no-ensure-clean",
    ]
    if agent_type is not None:
        create_args.extend(["--type", agent_type])

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
    see_also=(
        ("create", "Create an agent (full option set)"),
    ),
).register()
add_pager_help_option(diagnose)
