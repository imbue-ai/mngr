import sys
from typing import Any

import click
from loguru import logger

from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.errors import MngrError
from imbue.mngr_robinhood.arg_partition import partition_args
from imbue.mngr_robinhood.errors import UnsupportedClaudeFlagError
from imbue.mngr_robinhood.orchestrator import EXIT_MNGR_ERROR
from imbue.mngr_robinhood.orchestrator import run as orchestrator_run


class RobinhoodCliOptions(CommonCliOptions):
    """CLI options for the robinhood command.

    Only captures the trailing argv tuple: every meaningful flag is parsed
    by :func:`partition_args` rather than by click, so the user sees the
    same flag surface as the real ``claude`` CLI.
    """

    argv: tuple[str, ...]


@click.command(
    name="robinhood",
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
    },
)
@click.argument("argv", nargs=-1, type=click.UNPROCESSED)
@add_common_options
@click.pass_context
def robinhood(ctx: click.Context, **_kwargs: Any) -> None:
    # Force ``--quiet`` and ``--headless`` regardless of what the user passed:
    # this command pretends to be ``claude -p``, whose stdout/stderr contract is
    # "only the model's response shows up, nothing else." mngr's own loguru
    # chatter ("Creating agent state...", "Starting agent ...", "Sending
    # initial message...") and any TUI/interactive behavior would otherwise
    # leak into the captured output that callers consume. Both flags are still
    # accepted on the CLI for compatibility -- this just makes them no-ops when
    # absent rather than required.
    ctx.params["quiet"] = True
    ctx.params["headless"] = True
    mngr_ctx, _output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="robinhood",
        command_class=RobinhoodCliOptions,
    )

    try:
        partition = partition_args(opts.argv)
    except UnsupportedClaudeFlagError as exc:
        logger.error("{}", exc)
        ctx.exit(EXIT_MNGR_ERROR)

    try:
        exit_code = orchestrator_run(
            mngr_ctx=mngr_ctx,
            partition=partition,
            stdin=sys.stdin,
            stdout=sys.stdout,
            is_stdin_a_tty=sys.stdin.isatty(),
        )
    except MngrError as exc:
        logger.error("{}", exc)
        ctx.exit(EXIT_MNGR_ERROR)

    ctx.exit(exit_code)


CommandHelpMetadata(
    key="robinhood",
    one_line_description="Drop-in `claude -p` replacement backed by `mngr create` / `message` / `transcript`",
    synopsis="mngr robinhood [CLAUDE_FLAGS...] [PROMPT]",
    description="""Run a single `claude -p`-style invocation by spawning a fresh, ephemeral
`mngr` claude agent in the current directory. The agent receives the prompt,
runs to end-of-turn, the response is collected from the agent's transcript,
and the agent is destroyed.

Almost every flag accepted by the regular `claude` CLI is forwarded verbatim
to the spawned agent. The `-p`/`--print` flag is implied (always on); the
`--input-format`, `--output-format`, `--replay-user-messages`,
`--include-partial-messages`, and `--stream-plain-text` flags are consumed by
the wrapper to shape stdin/stdout.

Streaming (approximate, reverse-mapped from the agent's tmux pane):
- `--include-partial-messages` (with `--output-format stream-json`) emits
  claude-native `text_delta` partial events as the response is produced.
- `--stream-plain-text` (with the default text output) streams the response
  text to stdout incrementally.
Both default the agent to sonnet (a user-passed `--model` still wins).

The following flags are explicitly NOT supported in v1 and will cause the
command to exit with code 2:

- --fallback-model
- --max-budget-usd
- --no-session-persistence
- --include-hook-events
- -c / --continue
- -r / --resume
- --session-id

Exit codes:
  0 - Successful turn (agent reached WAITING with a reply)
  1 - The spawned claude agent exited before completing the turn
  2 - mngr-side failure (bad flags, missing prompt, agent failed to start, etc.)""",
    examples=(
        ("Single prompt", 'mngr robinhood "summarize this repo"'),
        ("JSON output", 'mngr robinhood "summarize" --output-format json'),
        ("Stream output", 'mngr robinhood "explain recursion" --output-format stream-json --verbose'),
        ("Pipe stdin", 'cat error.log | mngr robinhood "explain this"'),
        (
            "Multi-turn via stream-json",
            'printf \'%s\\n\' \'{"type":"user","message":{"role":"user","content":"hi"}}\''
            " | mngr robinhood --input-format stream-json --output-format stream-json",
        ),
    ),
    see_also=(
        ("create", "Create a long-lived mngr agent (this command's underlying primitive)"),
        ("message", "Send a follow-up message to a running agent"),
        ("transcript", "Read the message transcript for an agent"),
    ),
).register()

add_pager_help_option(robinhood)
