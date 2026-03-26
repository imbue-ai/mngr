"""CLI command for test-mapreduce."""

import time
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import assert_never

import click

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
from imbue.mng.primitives import SnapshotName
from imbue.mng_tmr.api import build_current_results
from imbue.mng_tmr.api import collect_tests
from imbue.mng_tmr.api import gather_results
from imbue.mng_tmr.api import generate_html_report
from imbue.mng_tmr.api import get_base_commit
from imbue.mng_tmr.api import launch_all_test_agents
from imbue.mng_tmr.api import launch_integrator_agent
from imbue.mng_tmr.api import poll_until_all_done
from imbue.mng_tmr.api import pull_agent_branch
from imbue.mng_tmr.api import pull_test_outputs
from imbue.mng_tmr.api import read_integrator_result
from imbue.mng_tmr.api import should_pull_changes
from imbue.mng_tmr.api import wait_for_integrator
from imbue.mng_tmr.data_types import IntegratorResult
from imbue.mng_tmr.data_types import TestMapReduceResult
from imbue.mng_tmr.data_types import TmrLaunchConfig

_DEFAULT_TIMEOUT_SECONDS = 3600.0
_DEFAULT_INTEGRATOR_TIMEOUT_SECONDS = 3600.0


class TmrCliOptions(CommonCliOptions):
    """Options passed from the CLI to the tmr command."""

    pytest_args: tuple[str, ...]
    testing_flags: tuple[str, ...]
    agent_type: str
    provider: str
    integrator_provider: str
    env: tuple[str, ...]
    label: tuple[str, ...]
    prompt_suffix: str | None
    use_snapshot: bool
    snapshot: str | None
    max_parallel: int
    launch_delay: float
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
    base_commit: str | None = None,
) -> IntegratorResult | None:
    """Launch an integrator agent to merge fix branches, if any exist."""
    fix_branches = [r.branch_name for r in results if should_pull_changes(r) and r.branch_name is not None]
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

    integrator_result: IntegratorResult | None = None
    if integrator_branch is not None:
        is_remote = config.provider_name.lower() != LOCAL_PROVIDER_NAME
        list_result = list_agents(
            mng_ctx=mng_ctx,
            is_streaming=False,
            error_behavior=ErrorBehavior.CONTINUE,
        )
        for agent_detail in list_result.agents:
            if str(agent_detail.id) == str(integrator.agent_id):
                integrator_result = read_integrator_result(agent_detail, integrator_host, integrator_branch)
                # Only pull branches from remote providers; local worktree branches already exist
                if is_remote:
                    pull_agent_branch(
                        agent_detail,
                        integrator_host,
                        config.source_dir,
                        mng_ctx.concurrency_group,
                        base_commit=base_commit,
                    )
                break

    if integrator_result is None:
        integrator_result = IntegratorResult(
            branch_name=integrator_branch,
            summary_markdown="Integrator timed out or could not be reached",
        )

    return integrator_result


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
    "--integrator-provider",
    default="local",
    show_default=True,
    help="Provider for the integrator agent (defaults to local since there is only one)",
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
    "--snapshot",
    default=None,
    help="Use an existing snapshot/image ID for all agents (skips building; implies --use-snapshot behavior)",
)
@click.option(
    "--max-parallel",
    default=4,
    show_default=True,
    type=int,
    help="Maximum number of agents to launch concurrently",
)
@click.option(
    "--launch-delay",
    default=2.0,
    show_default=True,
    type=float,
    help="Seconds to wait between launching each agent (avoids provider rate limits)",
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
    help="Path for the HTML report [default: tmr_<timestamp>/index.html]",
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

    # Step 1: Remember the base commit so we can create local branches for remote agents
    base_commit = get_base_commit(source_dir, mng_ctx.concurrency_group)

    # Step 2: Collect tests (positional paths + testing flags go to discovery)
    test_node_ids = collect_tests(
        pytest_args=opts.pytest_args + testing_flags,
        source_dir=source_dir,
        cg=mng_ctx.concurrency_group,
    )
    _emit_test_count(len(test_node_ids), output_opts)

    # Step 3: Get the local host for source_location (tests are collected locally)
    local_provider = get_provider_instance(LOCAL_PROVIDER_NAME, mng_ctx)
    local_host_ref = local_provider.get_host(HostName("localhost"))
    source_host, _ = ensure_host_started(local_host_ref, is_start_desired=True, provider=local_provider)

    # Step 4: Build launch config and launch agents
    env_options = AgentEnvironmentOptions(env_vars=resolve_env_vars((), opts.env))
    label_options = resolve_labels(opts.label)
    provided_snapshot = SnapshotName(opts.snapshot) if opts.snapshot is not None else None
    config = TmrLaunchConfig(
        source_dir=source_dir,
        source_host=source_host,
        agent_type=AgentTypeName(opts.agent_type),
        provider_name=ProviderInstanceName(opts.provider),
        env_options=env_options,
        label_options=label_options,
        snapshot=provided_snapshot,
    )
    # When --snapshot is provided, all agents use it directly (no need for --use-snapshot)
    agent_infos, agent_hosts, _snapshot_name = launch_all_test_agents(
        test_node_ids=test_node_ids,
        config=config,
        mng_ctx=mng_ctx,
        pytest_flags=testing_flags,
        prompt_suffix=opts.prompt_suffix or "",
        use_snapshot=opts.use_snapshot and provided_snapshot is None,
        max_parallel=opts.max_parallel,
        launch_delay_seconds=opts.launch_delay,
    )
    _emit_agents_launched(len(agent_infos), output_opts)

    # Step 5: Compute output directory and html_path before polling
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if opts.output_html is not None:
        html_path = Path(opts.output_html)
        output_dir = html_path.parent
    else:
        output_dir = Path(f"tmr_{timestamp}")
        html_path = output_dir / "index.html"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Step 6: Write initial report (all PENDING)
    initial_results = build_current_results(agent_infos, {}, set(), agent_hosts)
    generate_html_report(initial_results, html_path)

    # Step 7: Poll until all agents are done (or timeout), updating report continuously
    deadline = time.monotonic() + opts.timeout
    final_details, timed_out_ids = poll_until_all_done(
        agents=agent_infos,
        mng_ctx=mng_ctx,
        poll_interval_seconds=opts.poll_interval,
        hosts=agent_hosts,
        deadline=deadline,
        report_path=html_path,
    )

    # Step 8: Gather final results (read result.json, pull branches for fixes)
    # Only pass base_commit for remote providers -- local worktree branches already exist
    is_remote_provider = ProviderInstanceName(opts.provider).lower() != LOCAL_PROVIDER_NAME
    results = gather_results(
        agents=agent_infos,
        final_details=final_details,
        timed_out_ids=timed_out_ids,
        hosts=agent_hosts,
        source_dir=source_dir,
        cg=mng_ctx.concurrency_group,
        base_commit=base_commit if is_remote_provider else None,
    )

    # Step 9: Pull .test_output from each finished agent
    for agent_info in agent_infos:
        agent_id_str = str(agent_info.agent_id)
        detail = final_details.get(agent_id_str)
        if detail is not None and agent_id_str in agent_hosts:
            pull_test_outputs(detail, agent_hosts[agent_id_str], source_host, output_dir)

    # Step 10: Write report with final results
    generate_html_report(results, html_path)

    # Step 11: Build integrator config (defaults to local provider) and integrate
    integrator_config = TmrLaunchConfig(
        source_dir=source_dir,
        source_host=source_host,
        agent_type=AgentTypeName(opts.agent_type),
        provider_name=ProviderInstanceName(opts.integrator_provider),
        env_options=env_options,
        label_options=label_options,
    )
    integrator_result = _run_integrator_phase(results, integrator_config, mng_ctx, opts, base_commit=base_commit)
    generate_html_report(results, html_path, integrator=integrator_result)
    _emit_report_path(html_path, output_opts)


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
with a structured JSON containing: changes (list of kind/status/summary), errored flag,
tests_passing_before/after booleans, and a markdown summary.""",
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
