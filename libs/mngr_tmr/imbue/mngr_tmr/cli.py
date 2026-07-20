"""CLI command for test-mapreduce.

The framework (:mod:`imbue.mngr_mapreduce`) does the heavy lifting. This module
defines the click command, parses the TMR-specific options on top of the
framework's common options, and hands off to ``run_mapreduce``.
"""

from pathlib import Path

import click

from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr_mapreduce.cli import MapReduceCliOptions
from imbue.mngr_mapreduce.cli import add_mapreduce_options
from imbue.mngr_mapreduce.cli import run_mapreduce
from imbue.mngr_tmr.recipe import TestMapReduceRecipe
from imbue.mngr_tmr.task_file_recipe import TaskFileMapReduceRecipe
from imbue.mngr_tmr.task_file_recipe import load_task_packets


class TmrCliOptions(MapReduceCliOptions):
    """Options for the ``mngr tmr`` command (TMR specifics on top of the framework's)."""

    name: str
    mapper_prompt: str | None
    reducer_prompt: str | None
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
@click.option(
    "--name",
    default="tmr",
    show_default=True,
    help="Variant name, used as the prefix for this run's agent/branch/host names "
    "(e.g. tmr-mngr, tmr-minds) so distinct test suites stay separable and reviewable "
    "on their own. Distinct from --run-name, which identifies one run within a variant.",
)
@click.option(
    "--mapper-prompt",
    "mapper_prompt",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Override the packaged mapper prompt with this Jinja template file. It may "
    "'{% extends %}' or '{% include %}' the packaged mapper.j2 by name.",
)
@click.option(
    "--reducer-prompt",
    "reducer_prompt",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Override the packaged reducer prompt with this Jinja template file. It may "
    "'{% extends %}' or '{% include %}' the packaged reducer.j2 by name.",
)
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
        name=opts.name,
        pytest_args=opts.pytest_args,
        testing_flags=opts.testing_flags,
        mapper_prompt_path=Path(opts.mapper_prompt) if opts.mapper_prompt is not None else None,
        reducer_prompt_path=Path(opts.reducer_prompt) if opts.reducer_prompt is not None else None,
    )
    run_mapreduce(recipe, opts, mngr_ctx, output_opts)


CommandHelpMetadata(
    key="tmr",
    one_line_description="Run and fix tests in parallel using agents (test map-reduce)",
    synopsis="mngr tmr [TEST_PATHS...] [-- TESTING_FLAGS...] [--name <VARIANT>] [--mapper-prompt <FILE>] [--reducer-prompt <FILE>] [--provider <PROVIDER>] [--env KEY=VALUE] [--label KEY=VALUE] [--timeout <SECS>] [--agent-type <TYPE>]",
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

Use --name to give a run its own variant prefix (agent/branch/host names), so
distinct suites stay separable and reviewable on their own. For example, run the
mngr suite and the minds suite as separate variants:

  mngr tmr libs/mngr --name tmr-mngr -- -m "release and not docker and not docker_sdk"
  mngr tmr apps/minds --name tmr-minds -- -m "release and not minds_deployment and not minds_services and not minds_snapshot_resume"

Use --mapper-prompt / --reducer-prompt to point a variant at its own Jinja
prompt templates (they may extend or include the packaged ones by name).

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
        ("Run the minds suite as its own variant", "mngr tmr apps/minds --name tmr-minds -- -m release"),
        (
            "Use a custom mapper prompt",
            "mngr tmr apps/minds --name tmr-minds --mapper-prompt apps/minds/tmr/mapper.j2",
        ),
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


class TmrTasksCliOptions(MapReduceCliOptions):
    """Options for the ``mngr tmr-tasks`` command (task-file specifics on top of the framework's)."""

    name: str
    tasks_file: str
    mapper_prompt: str
    reducer_prompt: str


@click.command("tmr-tasks")
@click.option(
    "--tasks-file",
    "tasks_file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="JSONL task file: one packet per line with schema_version, id, optional display_id, kind, "
    "and a free-form context object handed to the mapper prompt template.",
)
@click.option(
    "--name",
    default="tmr-tasks",
    show_default=True,
    help="Variant name, used as the prefix for this run's agent/branch/host names.",
)
@click.option(
    "--mapper-prompt",
    "mapper_prompt",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Mapper prompt template (required): rendered with task_id, kind, context_json, "
    "outcome_filename, and publish_snippet. There is no packaged default because the task "
    "semantics live with the producer of the task file.",
)
@click.option(
    "--reducer-prompt",
    "reducer_prompt",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Reducer prompt template (required): rendered with inputs_dirname, "
    "mapper_outcome_filename, reducer_outcome_filename, and publish_snippet.",
)
@add_mapreduce_options
@add_common_options
@click.pass_context
def tmr_tasks(ctx: click.Context, **kwargs: object) -> None:
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="tmr-tasks",
        command_class=TmrTasksCliOptions,
    )
    tasks_file = Path(opts.tasks_file)
    recipe = TaskFileMapReduceRecipe(
        name=opts.name,
        packets=load_task_packets(tasks_file),
        tasks_file=tasks_file,
        mapper_prompt_path=Path(opts.mapper_prompt),
        reducer_prompt_path=Path(opts.reducer_prompt),
    )
    run_mapreduce(recipe, opts, mngr_ctx, output_opts)


CommandHelpMetadata(
    key="tmr-tasks",
    one_line_description="Fan out a JSONL task file to one agent per task and integrate their branches",
    synopsis="mngr tmr-tasks --tasks-file <JSONL> --mapper-prompt <FILE> --reducer-prompt <FILE> [--name <VARIANT>] [--provider <PROVIDER>] [--env KEY=VALUE] [--label KEY=VALUE] [--timeout <SECS>] [--agent-type <TYPE>]",
    description="""This command runs the map-reduce framework over an explicit task file instead of pytest collection:

1. Reads and validates --tasks-file: one JSON packet per line with
   schema_version, id, optional display_id (used for agent/branch slugs),
   kind, and a free-form context object.
2. Launches one agent per task. The mapper prompt comes from --mapper-prompt
   (required; there is no packaged default -- the task semantics live with
   the producer of the task file), rendered with task_id, kind, context_json
   (the packet's context as pretty JSON), outcome_filename, and
   publish_snippet.
3. Polls agents until all finish or individually time out; pulls each
   agent's branch.bundle back into local branches.
4. If any mapper produced outputs, launches a reducer agent with
   --reducer-prompt to integrate the mapper branches.
5. Generates the shared HTML report; mapper agents must write the same
   testing_agent_outcome.json contract as `mngr tmr` mappers.

The canonical producer is `minds specs plan --for-tmr`, which emits one
packet per behavioral-spec unit; pair it with the minds spec-witnessing
prompts at apps/minds/tmr/specs_mapper.j2 and apps/minds/tmr/specs_reducer.j2.""",
    examples=(
        (
            "Fan out minds spec units to witness-test-writing agents",
            "minds specs plan --for-tmr > /tmp/spec-tasks.jsonl && "
            "mngr tmr-tasks --tasks-file /tmp/spec-tasks.jsonl --name tmr-minds-specs "
            "--mapper-prompt apps/minds/tmr/specs_mapper.j2 --reducer-prompt apps/minds/tmr/specs_reducer.j2",
        ),
        ("Use Docker provider", "mngr tmr-tasks --provider docker --tasks-file tasks.jsonl ..."),
        ("Limit to 4 concurrent agents", "mngr tmr-tasks --max-parallel-agents 4 --tasks-file tasks.jsonl ..."),
    ),
    see_also=(
        ("tmr", "Run and fix tests in parallel using agents"),
        ("list", "List agents"),
    ),
).register()

add_pager_help_option(tmr_tasks)
