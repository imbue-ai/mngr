"""CLI command for test-mapreduce.

The framework (:mod:`imbue.mngr_mapreduce`) does the heavy lifting. This module
defines the click command, parses the TMR-specific options on top of the
framework's common options, and hands off to ``run_mapreduce``.
"""

import click

from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr_mapreduce.cli import MapReduceCliOptions
from imbue.mngr_mapreduce.cli import add_mapreduce_options
from imbue.mngr_mapreduce.cli import run_mapreduce
from imbue.mngr_tmr.recipe import TestMapReduceRecipe


class TmrCliOptions(MapReduceCliOptions):
    """Options for the ``mngr tmr`` command (TMR specifics on top of the framework's)."""

    pytest_args: tuple[str, ...]
    testing_flags: tuple[str, ...]


class _TmrCommand(click.Command):
    """Custom Command that handles -- separator for testing flags.

    Everything before -- is treated as positional args (test paths/patterns).
    Everything after -- is captured as testing_flags and shared between
    pytest discovery and individual test runs.

    This is the same trick used by _CreateCommand in the mngr create CLI.
    """

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if "--" in args:
            idx = args.index("--")
            after_dash = tuple(args[idx + 1 :])
            args = args[:idx]
        else:
            after_dash = ()
        result = super().parse_args(ctx, args)
        ctx.params["testing_flags"] = after_dash
        return result


@click.command("tmr", cls=_TmrCommand, context_settings={"ignore_unknown_options": True})
@click.argument("pytest_args", nargs=-1, type=click.UNPROCESSED)
@add_mapreduce_options
@add_common_options
@click.pass_context
def tmr(ctx: click.Context, **kwargs: object) -> None:
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="tmr",
        command_class=TmrCliOptions,
    )
    recipe = TestMapReduceRecipe(
        pytest_args=opts.pytest_args,
        testing_flags=opts.testing_flags,
    )
    run_mapreduce(recipe, opts, mngr_ctx, output_opts)


CommandHelpMetadata(
    key="tmr",
    one_line_description="Run and fix tests in parallel using agents (test map-reduce)",
    synopsis="mngr tmr [TEST_PATHS...] [-- TESTING_FLAGS...] [--provider <PROVIDER>] [--env KEY=VALUE] [--label KEY=VALUE] [--timeout <SECS>] [--agent-type <TYPE>]",
    description="""This command implements a map-reduce pattern for tests:

1. Collects tests using pytest --collect-only, passing through all arguments.
2. Launches one agent per test. Each agent runs the test and, if it fails,
   attempts to diagnose and fix either the test code or the implementation.
3. Polls agents until all finish or individually time out (per-agent timeout).
   An HTML report is updated continuously during polling.
4. For successful fixes, pulls the agent's code changes into branches
   named tmr/<run>/*.
5. If any fixes succeeded, launches a reducer agent to merge all fix
   branches into a single integrated branch (tmr/<run>/reducer).
6. Generates a final HTML report summarizing all outcomes with markdown
   summaries, including the integrated branch name if applicable.

Arguments before -- are test paths/patterns (positional). Arguments after -- are
pytest testing flags shared between discovery and individual test runs. For example:

  mngr tmr tests/e2e -- -m release

This discovers tests with `pytest --collect-only tests/e2e -m release` and runs
each test with `pytest tests/e2e/test_foo.py::test_bar -m release`.

Use --provider to run agents on a specific provider (e.g. docker, modal).
On providers that support snapshots (e.g. modal), the orchestrator
automatically builds and provisions one host, snapshots it, then launches
all remaining agents from that snapshot. Pass --snapshot <ID> to reuse an
existing snapshot instead of building one.
Use --env to pass environment variables and --label to tag all agents.
Use --max-parallel-agents to limit how many agents run simultaneously (0 = no limit).

Each agent writes its result to .test_output/testing_agent_outcome.json (in its work directory)
with a structured JSON containing: changes (list of kind/status/summary), errored flag,
tests_passing_before/after booleans, and a markdown summary.""",
    examples=(
        ("Run all tests in current directory", "mngr tmr"),
        ("Run tests in a specific file", "mngr tmr tests/test_foo.py"),
        ("Run tests with a marker", "mngr tmr tests/e2e -- -m release"),
        ("Use Docker provider", "mngr tmr --provider docker tests/"),
        ("Modal (snapshot is automatic)", "mngr tmr --provider modal tests/"),
        ("Pass env vars and labels", "mngr tmr --env API_KEY=xxx --label batch=run1"),
        ("Limit to 4 concurrent agents", "mngr tmr --max-parallel-agents 4 tests/"),
        ("Custom poll interval", "mngr tmr --poll-interval 30"),
        ("Specify output location", "mngr tmr --output-dir reports/run-1"),
    ),
    see_also=(
        ("create", "Create a new agent"),
        ("list", "List agents"),
        ("rsync", "Rsync files between local and a remote host or agent"),
    ),
).register()

add_pager_help_option(tmr)
