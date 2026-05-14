import sys
from typing import Any

import click
from loguru import logger

from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.errors import BaseMngrError
from imbue.mngr.errors import MngrError
from imbue.mngr_uncapped_claude.arg_partition import partition_args
from imbue.mngr_uncapped_claude.errors import UnsupportedClaudeFlagError
from imbue.mngr_uncapped_claude.orchestrator import EXIT_MNGR_ERROR
from imbue.mngr_uncapped_claude.orchestrator import run as orchestrator_run


class UncappedClaudeCliOptions(CommonCliOptions):
    """CLI options for the uncapped-claude command.

    Only captures the trailing argv tuple: every meaningful flag is parsed
    by :func:`partition_args` rather than by click, so the user sees the
    same flag surface as the real ``claude`` CLI.
    """

    argv: tuple[str, ...]


@click.command(
    name="uncapped-claude",
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
    },
)
@click.argument("argv", nargs=-1, type=click.UNPROCESSED)
@add_common_options
@click.pass_context
def uncapped_claude(ctx: click.Context, **_kwargs: Any) -> None:
    mngr_ctx, _output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="uncapped-claude",
        command_class=UncappedClaudeCliOptions,
    )

    try:
        partition = partition_args(opts.argv)
    except UnsupportedClaudeFlagError as exc:
        logger.error("{}", exc)
        ctx.exit(EXIT_MNGR_ERROR)
        return

    try:
        exit_code = orchestrator_run(
            mngr_ctx=mngr_ctx,
            partition=partition,
            stdin=sys.stdin,
            stdout=sys.stdout,
            is_stdin_a_tty=sys.stdin.isatty(),
        )
    except (MngrError, BaseMngrError) as exc:
        logger.error("{}", exc)
        ctx.exit(EXIT_MNGR_ERROR)
        return

    ctx.exit(exit_code)


CommandHelpMetadata(
    key="uncapped-claude",
    one_line_description="Drop-in `claude -p` replacement backed by `mngr create` / `message` / `transcript`",
    synopsis="mngr uncapped-claude [CLAUDE_FLAGS...] [PROMPT]",
    description="""Run a single `claude -p`-style invocation by spawning a fresh, ephemeral
`mngr` claude agent in the current directory. The agent receives the prompt,
runs to end-of-turn, the response is collected from the agent's transcript,
and the agent is destroyed.

Almost every flag accepted by the regular `claude` CLI is forwarded verbatim
to the spawned agent. The `-p`/`--print` flag is implied (always on); the
`--input-format`, `--output-format`, and `--replay-user-messages` flags are
consumed by the wrapper to shape stdin/stdout.

The following flags are explicitly NOT supported in v1 and will cause the
command to exit with code 2:

- --fallback-model
- --max-budget-usd
- --no-session-persistence
- --include-hook-events
- --include-partial-messages
- -c / --continue
- -r / --resume
- --session-id

Exit codes:
  0 - Successful turn (no claude or api error)
  1 - claude or api error reported in the transcript
  2 - mngr-side failure (bad flags, missing prompt, agent failed to start, etc.)""",
    examples=(
        ("Single prompt", 'mngr uncapped-claude "summarize this repo"'),
        ("JSON output", 'mngr uncapped-claude "summarize" --output-format json'),
        ("Stream output", 'mngr uncapped-claude "explain recursion" --output-format stream-json --verbose'),
        ("Pipe stdin", 'cat error.log | mngr uncapped-claude "explain this"'),
        (
            "Multi-turn via stream-json",
            'printf \'%s\\n\' \'{"type":"user","message":{"role":"user","content":"hi"}}\''
            " | mngr uncapped-claude --input-format stream-json --output-format stream-json",
        ),
    ),
    see_also=(
        ("create", "Create a long-lived mngr agent (this command's underlying primitive)"),
        ("message", "Send a follow-up message to a running agent"),
        ("transcript", "Read the message transcript for an agent"),
    ),
).register()

add_pager_help_option(uncapped_claude)
