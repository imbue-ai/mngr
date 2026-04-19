from collections.abc import Sequence
from pathlib import Path
from typing import Final

import click
from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.pure import pure
from imbue.mngr.cli.create import create as create_cmd
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.issue_reporting import DIAGNOSE_FLOW_META_KEY
from imbue.mngr.cli.issue_reporting import get_mngr_version
from imbue.mngr_diagnose.clone import ensure_mngr_clone
from imbue.mngr_diagnose.context_file import read_diagnose_context
from imbue.mngr_diagnose.prompt import build_diagnose_initial_message

DIAGNOSE_CLONE_DIR: Final[Path] = Path("/tmp/mngr-diagnose")

# Create options that diagnose owns and so the user must not pass via pass-through.
# These are the flags diagnose hardcodes when invoking create.
_RESERVED_CREATE_OPTIONS: Final[frozenset[str]] = frozenset(
    {
        "--from",
        "--source",
        "--transfer",
        "--branch",
        "--message",
        "--message-file",
        "--edit-message",
    }
)


@pure
def _build_description_from_context(error_type: str | None, error_message: str | None) -> str | None:
    """Build a fallback description from context error fields.

    Returns None if neither field is present.
    """
    if error_type is not None and error_message is not None:
        return f"{error_type}: {error_message}"
    return error_type or error_message


def _reject_reserved_options(args: Sequence[str], ctx: click.Context) -> None:
    """Raise a UsageError if any reserved create option appears in args."""
    for arg in args:
        flag_name = arg.split("=", 1)[0]
        if flag_name in _RESERVED_CREATE_OPTIONS:
            raise click.UsageError(
                f"Cannot pass {flag_name} to diagnose: this flag is set automatically. "
                f"Reserved flags: {', '.join(sorted(_RESERVED_CREATE_OPTIONS))}",
                ctx=ctx,
            )


@click.command(context_settings={"ignore_unknown_options": True})
@click.option(
    "--description",
    default=None,
    help="Free-text description of the problem.",
)
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
@click.argument("create_args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def diagnose(
    ctx: click.Context,
    description: str | None,
    clone_dir: str | None,
    context_file: str | None,
    create_args: tuple[str, ...],
) -> None:
    """Launch an agent to diagnose a bug and prepare a GitHub issue.

    Clones the mngr repo (or reuses an existing clone) and creates an agent
    in a worktree to investigate the problem.

    Any options not listed below are forwarded to the underlying `mngr create`
    command, so you can use any create option (e.g. --provider, --type,
    --idle-timeout, --env). Options that diagnose sets automatically (--from,
    --source, --transfer, --branch, --message, --message-file, --edit-message)
    cannot be overridden.
    """
    _reject_reserved_options(create_args, ctx)

    resolved_clone_dir = Path(clone_dir) if clone_dir is not None else DIAGNOSE_CLONE_DIR

    # Read context file if provided
    traceback_str: str | None = None
    mngr_version = get_mngr_version()

    if context_file is not None:
        context = read_diagnose_context(Path(context_file))
        traceback_str = context.traceback_str
        mngr_version = context.mngr_version
        # Use error info as description if no explicit description given
        if description is None:
            description = _build_description_from_context(context.error_type, context.error_message)

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

    # Diagnose-owned create args, then user-provided pass-through args.
    # Click's last-wins semantics let pass-through args override defaults.
    full_create_args: list[str] = [
        "--from",
        f":{resolved_clone_dir}",
        "--transfer",
        "git-worktree",
        "--branch",
        "main:",
        "--message",
        message,
        *create_args,
    ]

    # Mark the shared meta dict so that AliasAwareGroup's error handler can
    # detect a crash inside a diagnose flow and skip offering diagnose again
    # (which would recurse). create's setup_command_context overwrites
    # hook_command_name, so we use a dedicated flag.
    ctx.meta[DIAGNOSE_FLOW_META_KEY] = True

    create_ctx = create_cmd.make_context("diagnose", full_create_args, parent=ctx)
    with create_ctx:
        create_cmd.invoke(create_ctx)


CommandHelpMetadata(
    key="diagnose",
    one_line_description="Launch an agent to diagnose a bug and prepare a GitHub issue",
    synopsis="mngr diagnose [--description TEXT] [--context-file PATH] [--clone-dir PATH] [CREATE_OPTIONS...]",
    description="""Launch a diagnostic agent that investigates a bug in the mngr codebase.

The agent works in a worktree of a local clone of the mngr repository
(cloned to --clone-dir, default /tmp/mngr-diagnose). It analyzes the
error, finds the root cause, and prepares a GitHub issue for user review.

Provide a description via --description, a --context-file written by the
error handler, or both. If neither is provided, the agent will ask the
user for details interactively.

Any options not recognized by diagnose are forwarded to `mngr create`, so
you can use any create option (e.g. --provider, --type, --idle-timeout).
The following flags are reserved by diagnose and cannot be passed through:
--from, --source, --transfer, --branch, --message, --message-file,
--edit-message.""",
    examples=(
        ("Diagnose a described problem", 'mngr diagnose --description "create fails with spaces in path"'),
        ("Diagnose from error context", "mngr diagnose --context-file /tmp/mngr-diagnose-context-abc123.json"),
        (
            "Diagnose on a different provider",
            'mngr diagnose --description "modal-only bug" --provider modal',
        ),
        (
            "Diagnose with a specific agent type",
            'mngr diagnose --description "error" --type opencode',
        ),
    ),
    see_also=(("create", "Create an agent (full option set)"),),
).register()
add_pager_help_option(diagnose)
