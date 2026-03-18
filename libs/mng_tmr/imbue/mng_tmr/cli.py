"""CLI command for test-mapreduce."""

import time
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import assert_never

import click

from imbue.imbue_common.model_update import to_update
from imbue.mng.api.find import ensure_host_started
from imbue.mng.api.list import list_agents
from imbue.mng.api.providers import get_provider_instance
from imbue.mng.cli.common_opts import CommonCliOptions
from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng.cli.env_utils import resolve_env_vars
from imbue.mng.cli.env_utils import resolve_labels
from imbue.mng.cli.help_formatter import CommandHelpMetadata
from imbue.mng.cli.help_formatter import add_pager_help_option
from imbue.mng.cli.output_helpers import emit_event
from imbue.mng.cli.output_helpers import write_human_line
from imbue.mng.config.data_types import MngContext
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.interfaces.host import AgentEnvironmentOptions
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import ErrorBehavior
from imbue.mng.primitives import HostName
from imbue.mng.primitives import LOCAL_PROVIDER_NAME
from imbue.mng.primitives import OutputFormat
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng_tmr.api import build_current_results
from imbue.mng_tmr.api import collect_tests
from imbue.mng_tmr.api import gather_results
from imbue.mng_tmr.api import generate_html_report
from imbue.mng_tmr.api import launch_all_test_agents
from imbue.mng_tmr.api import launch_integrator_agent
from imbue.mng_tmr.api import poll_until_all_done
from imbue.mng_tmr.api import pull_agent_branch
from imbue.mng_tmr.api import wait_for_integrator
from imbue.mng_tmr.data_types import TestMapReduceResult
from imbue.mng_tmr.data_types import TestOutcome
from imbue.mng_tmr.data_types import TmrLaunchConfig

_DEFAULT_TIMEOUT_SECONDS = 3600.0
_DEFAULT_INTEGRATOR_TIMEOUT_SECONDS = 3600.0


class TmrCliOptions(CommonCliOptions):
    """Options passed from the CLI to the tmr command."""

    pytest_args: tuple[str, ...]
    testing_flags: tuple[str, ...]
    agent_type: str
    provider: str
    env: tuple[str, ...]
    label: tuple[str, ...]
    prompt_suffix: str | None
    use_snapshot: bool
    poll_interval: float
    timeout: float
    integrator_timeout: float
    output_html: str | None
    source: str | None


class _TmrCommand(click.Command):
    """Custom Command that handles -- separator for testing flags.

    Everything before -- is treated as positional args (test paths/patterns).
    Everything after -- is captured as testing_flags and shared between
    pytest discovery and individual test runs.

    This is the same trick used by _CreateCommand in the mng create CLI.
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


def _emit_test_count(count: int, output_opts: OutputOptions) -> None:
    """Emit the number of tests collected."""
    match output_opts.output_format:
        case OutputFormat.JSON | OutputFormat.JSONL:
            emit_event("tests_collected", {"count": count}, output_opts.output_format)
        case OutputFormat.HUMAN:
            write_human_line("Collected {} test(s)", count)
        case _ as unreachable:
            assert_never(unreachable)


def _emit_agents_launched(count: int, output_opts: OutputOptions) -> None:
    """Emit the number of agents launched."""
    match output_opts.output_format:
        case OutputFormat.JSON | OutputFormat.JSONL:
            emit_event("agents_launched", {"count": count}, output_opts.output_format)
        case OutputFormat.HUMAN:
            write_human_line("Launched {} agent(s)", count)
        case _ as unreachable:
            assert_never(unreachable)


def _emit_report_path(path: Path, output_opts: OutputOptions) -> None:
    """Emit the path to the generated HTML report."""
    match output_opts.output_format:
        case OutputFormat.JSON | OutputFormat.JSONL:
            emit_event("report_generated", {"path": str(path)}, output_opts.output_format)
        case OutputFormat.HUMAN:
            write_human_line("Report: {}", path)
        case _ as unreachable:
            assert_never(unreachable)


def _run_integrator_phase(
    results: list[TestMapReduceResult],
    config: TmrLaunchConfig,
    mng_ctx: MngContext,
    opts: TmrCliOptions,
) -> str | None:
    """Launch an integrator agent to merge fix branches, if any exist."""
    fix_branches = [
        r.branch_name
        for r in results
        if r.outcome in (TestOutcome.FIX_TEST_SUCCEEDED, TestOutcome.FIX_IMPL_SUCCEEDED) and r.branch_name is not None
    ]
    if not fix_branches:
        return None

    integrator, integrator_host = launch_integrator_agent(
        fix_branches=fix_branches,
        config=config,
        mng_ctx=mng_ctx,
    )

    integrator_deadline = time.monotonic() + opts.integrator_timeout
    integrator_branch = wait_for_integrator(
        integrator=integrator,
        mng_ctx=mng_ctx,
        poll_interval_seconds=opts.poll_interval,
        host=integrator_host,
        deadline=integrator_deadline,
    )

    if integrator_branch is not None:
        list_result = list_agents(
            mng_ctx=mng_ctx,
            is_streaming=False,
            error_behavior=ErrorBehavior.CONTINUE,
        )
        for agent_detail in list_result.agents:
            if str(agent_detail.id) == str(integrator.agent_id):
                pull_agent_branch(agent_detail, integrator_host, config.source_dir, mng_ctx.concurrency_group)
                break

    return integrator_branch


def _emit_summary(results: list[TestMapReduceResult], output_opts: OutputOptions) -> None:
    """Print per-test outcome summary in human mode."""
    if output_opts.output_format != OutputFormat.HUMAN:
        return
    for r in results:
        branch_info = f" -> {r.branch_name}" if r.branch_name else ""
        write_human_line(
            "  {} [{}] {}{}",
            r.outcome.value,
            r.agent_name,
            r.summary,
            branch_info,
        )


@click.command("tmr", cls=_TmrCommand, context_settings={"ignore_unknown_options": True})
@click.argument("pytest_args", nargs=-1, type=click.UNPROCESSED)
@click.option(
    "--agent-type",
    default="claude",
    show_default=True,
    help="Type of agent to launch for each test",
)
@click.option(
    "--provider",
    default="local",
    show_default=True,
    help="Provider for agent hosts (e.g. local, docker, modal)",
)
@click.option(
    "--env",
    multiple=True,
    help="Environment variable KEY=VALUE to pass to agents [repeatable]",
)
@click.option(
    "--label",
    multiple=True,
    help="Agent label KEY=VALUE to attach to all launched agents [repeatable]",
)
@click.option(
    "--prompt-suffix",
    default=None,
    help="Additional text to append to the agent prompt",
)
@click.option(
    "--use-snapshot",
    is_flag=True,
    default=False,
    help="Build one agent first, snapshot its host, then launch remaining agents from the snapshot (faster for remote providers)",
)
@click.option(
    "--poll-interval",
    default=10.0,
    show_default=True,
    type=float,
    help="Seconds between polling cycles when waiting for agents to finish",
)
@click.option(
    "--timeout",
    default=_DEFAULT_TIMEOUT_SECONDS,
    show_default=True,
    type=float,
    help="Maximum seconds to wait for agents after launch (stops all pending agents when reached)",
)
@click.option(
    "--integrator-timeout",
    default=_DEFAULT_INTEGRATOR_TIMEOUT_SECONDS,
    show_default=True,
    type=float,
    help="Maximum seconds to wait for the integrator agent to merge fix branches",
)
@click.option(
    "--output-html",
    default=None,
    type=click.Path(),
    help="Path for the HTML report [default: tmr-report-<timestamp>.html]",
)
@click.option(
    "--source",
    default=None,
    type=click.Path(exists=True, file_okay=False),
    help="Source directory for test collection and agent work dirs [default: current directory]",
)
@add_common_options
@click.pass_context
def tmr(ctx: click.Context, **kwargs: object) -> None:
    mng_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="tmr",
        command_class=TmrCliOptions,
    )

    source_dir = Path(opts.source) if opts.source is not None else Path.cwd()
    testing_flags = opts.testing_flags

    # Step 1: Collect tests (positional paths + testing flags go to discovery)
    test_node_ids = collect_tests(
        pytest_args=opts.pytest_args + testing_flags,
        source_dir=source_dir,
        cg=mng_ctx.concurrency_group,
    )
    _emit_test_count(len(test_node_ids), output_opts)

    # Step 2: Get the local host for source_location (tests are collected locally)
    local_provider = get_provider_instance(LOCAL_PROVIDER_NAME, mng_ctx)
    local_host_ref = local_provider.get_host(HostName("localhost"))
    source_host, _ = ensure_host_started(local_host_ref, is_start_desired=True, provider=local_provider)

    # Step 3: Build launch config and launch agents
    config = TmrLaunchConfig(
        source_dir=source_dir,
        source_host=source_host,
        agent_type=AgentTypeName(opts.agent_type),
        provider_name=ProviderInstanceName(opts.provider),
        env_options=AgentEnvironmentOptions(env_vars=resolve_env_vars((), opts.env)),
        label_options=resolve_labels(opts.label),
    )
    agent_infos, agent_hosts, snapshot_name = launch_all_test_agents(
        test_node_ids=test_node_ids,
        config=config,
        mng_ctx=mng_ctx,
        pytest_flags=testing_flags,
        prompt_suffix=opts.prompt_suffix or "",
        use_snapshot=opts.use_snapshot,
    )
    _emit_agents_launched(len(agent_infos), output_opts)
    # Update config with snapshot for integrator phase
    if snapshot_name is not None:
        config = config.model_copy_update(to_update(config.field_ref().snapshot, snapshot_name))

    # Step 4: Compute html_path before polling
    if opts.output_html is not None:
        html_path = Path(opts.output_html)
    else:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        html_path = Path(f"tmr-report-{timestamp}.html")

    # Step 5: Write initial report (all PENDING)
    initial_results = build_current_results(agent_infos, {}, set(), agent_hosts)
    generate_html_report(initial_results, html_path)

    # Step 6: Poll until all agents are done (or timeout), updating report continuously
    deadline = time.monotonic() + opts.timeout
    final_details, timed_out_ids = poll_until_all_done(
        agents=agent_infos,
        mng_ctx=mng_ctx,
        poll_interval_seconds=opts.poll_interval,
        hosts=agent_hosts,
        deadline=deadline,
        report_path=html_path,
    )

    # Step 7: Gather final results (read result.json, pull branches for fixes)
    results = gather_results(
        agents=agent_infos,
        final_details=final_details,
        timed_out_ids=timed_out_ids,
        hosts=agent_hosts,
        source_dir=source_dir,
        cg=mng_ctx.concurrency_group,
    )

    # Step 8: Write report with final results
    generate_html_report(results, html_path)

    # Step 9: Integrate fix branches and write final report
    integrator_branch = _run_integrator_phase(results, config, mng_ctx, opts)
    generate_html_report(results, html_path, integrator_branch=integrator_branch)
    _emit_report_path(html_path, output_opts)
    _emit_summary(results, output_opts)


CommandHelpMetadata(
    key="tmr",
    one_line_description="Run and fix tests in parallel using agents (test map-reduce)",
    synopsis="mng tmr [TEST_PATHS...] [-- TESTING_FLAGS...] [--provider <PROVIDER>] [--use-snapshot] [--env KEY=VALUE] [--label KEY=VALUE] [--timeout <SECS>] [--agent-type <TYPE>]",
    description="""This command implements a map-reduce pattern for tests:

1. Collects tests using pytest --collect-only, passing through all arguments.
2. Launches one agent per test. Each agent runs the test and, if it fails,
   attempts to diagnose and fix either the test code or the implementation.
3. Polls agents until all finish (stops agents when they enter WAITING state).
   An HTML report is updated continuously during polling.
4. For successful fixes, pulls the agent's code changes into branches
   named mng-tmr/*.
5. If any fixes succeeded, launches an integrator agent to merge all fix
   branches into a single integrated branch (mng-tmr/integrated-*).
6. Generates a final HTML report summarizing all outcomes with markdown
   summaries, including the integrated branch name if applicable.

Arguments before -- are test paths/patterns (positional). Arguments after -- are
pytest testing flags shared between discovery and individual test runs. For example:

  mng tmr tests/e2e -- -m release

This discovers tests with `pytest --collect-only tests/e2e -m release` and runs
each test with `pytest tests/e2e/test_foo.py::test_bar -m release`.

Use --provider to run agents on a specific provider (e.g. docker, modal).
Use --use-snapshot with remote providers to build and provision one host first,
snapshot it, then launch all remaining agents from the snapshot (much faster).
Use --env to pass environment variables and --label to tag all agents.
Use --prompt-suffix to append custom instructions to the agent prompt.

Each agent writes its result to $MNG_AGENT_STATE_DIR/plugin/test-map-reduce/result.json
with an outcome enum and a markdown summary.""",
    examples=(
        ("Run all tests in current directory", "mng tmr"),
        ("Run tests in a specific file", "mng tmr tests/test_foo.py"),
        ("Run tests with a marker", "mng tmr tests/e2e -- -m release"),
        ("Use Docker provider", "mng tmr --provider docker tests/"),
        ("Modal with snapshot", "mng tmr --provider modal --use-snapshot tests/"),
        ("Pass env vars and labels", "mng tmr --env API_KEY=xxx --label batch=run1"),
        ("Custom poll interval", "mng tmr --poll-interval 30"),
        ("Specify output location", "mng tmr --output-html report.html"),
    ),
    see_also=(
        ("create", "Create a new agent"),
        ("list", "List agents"),
        ("pull", "Pull files or git commits from an agent"),
    ),
).register()

add_pager_help_option(tmr)
