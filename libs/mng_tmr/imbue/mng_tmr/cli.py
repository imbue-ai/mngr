"""CLI command for test-mapreduce."""

import resource
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
from imbue.mng.interfaces.data_types import AgentDetails
from imbue.mng.interfaces.host import AgentEnvironmentOptions
from imbue.mng.interfaces.host import AgentLabelOptions
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import ErrorBehavior
from imbue.mng.primitives import HostName
from imbue.mng.primitives import LOCAL_PROVIDER_NAME
from imbue.mng.primitives import OutputFormat
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.primitives import SnapshotName
from imbue.mng_tmr.api import collect_tests
from imbue.mng_tmr.api import gather_results
from imbue.mng_tmr.api import get_base_commit
from imbue.mng_tmr.api import launch_all_test_agents
from imbue.mng_tmr.api import launch_and_poll_agents
from imbue.mng_tmr.api import launch_integrator_agent
from imbue.mng_tmr.api import pull_agent_branch
from imbue.mng_tmr.api import pull_test_outputs_by_id
from imbue.mng_tmr.api import read_integrator_result
from imbue.mng_tmr.api import should_pull_changes
from imbue.mng_tmr.api import wait_for_integrator
from imbue.mng_tmr.data_types import IntegratorResult
from imbue.mng_tmr.data_types import TestAgentInfo
from imbue.mng_tmr.data_types import TestMapReduceResult
from imbue.mng_tmr.data_types import TmrLaunchConfig
from imbue.mng_tmr.report import generate_html_report

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
    max_agents: int
    launch_delay: float
    poll_interval: float
    timeout: float
    result_check_interval: float
    integrator_timeout: float
    output_html: str | None
    source: str | None
    reintegrate: str | None


_MIN_FD_LIMIT = 4096


def _raise_fd_limit() -> None:
    """Raise the soft file descriptor limit to handle many concurrent agents."""
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < _MIN_FD_LIMIT:
            new_soft = min(_MIN_FD_LIMIT, hard)
            resource.setrlimit(resource.RLIMIT_NOFILE, (new_soft, hard))
    except (ValueError, OSError):
        pass


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


def _run_reintegrate(
    opts: TmrCliOptions,
    mng_ctx: MngContext,
    output_opts: OutputOptions,
    source_dir: Path,
) -> None:
    """Re-read outcomes from a previous TMR run and re-run integration.

    Discovers agents by the tmr_run_name label, reads their result files,
    re-runs the integrator, and generates a fresh report.
    """
    run_name = opts.reintegrate
    write_human_line("Reintegrating run: {}", run_name)

    # Discover agents from the previous run by label
    list_result = list_agents(
        mng_ctx=mng_ctx,
        is_streaming=False,
        error_behavior=ErrorBehavior.CONTINUE,
    )
    matching_agents = [
        detail
        for detail in list_result.agents
        if detail.labels.get("tmr_run_name") == run_name and not str(detail.name).startswith("tmr-integrator-")
    ]
    write_human_line("Found {} agent(s) from run {}", len(matching_agents), run_name)

    if not matching_agents:
        write_human_line("No agents found for run name '{}'. Nothing to reintegrate.", run_name)
        return

    # Get local host for artifact pulling
    local_provider = get_provider_instance(LOCAL_PROVIDER_NAME, mng_ctx)
    local_host_ref = local_provider.get_host(HostName("localhost"))
    source_host, _ = ensure_host_started(local_host_ref, is_start_desired=True, provider=local_provider)

    # Build agent infos and hosts from discovered agents
    agent_infos: list[TestAgentInfo] = []
    agent_hosts: dict[str, OnlineHostInterface] = {}
    final_details: dict[str, AgentDetails] = {}

    for detail in matching_agents:
        agent_id_str = str(detail.id)
        info = TestAgentInfo(
            test_node_id=detail.labels.get("test_node_id", str(detail.name)),
            agent_id=detail.id,
            agent_name=detail.name,
            created_at=0.0,
        )
        agent_infos.append(info)
        final_details[agent_id_str] = detail
        if detail.host is not None:
            host_provider = get_provider_instance(detail.host.provider_name, mng_ctx)
            host_ref = host_provider.get_host(HostName(detail.host.name))
            host, _ = ensure_host_started(host_ref, is_start_desired=True, provider=host_provider)
            agent_hosts[agent_id_str] = host

    # Compute output directory
    if opts.output_html is not None:
        html_path = Path(opts.output_html)
        output_dir = html_path.parent
    else:
        output_dir = Path(f"tmr_{run_name}_reintegrate")
        html_path = output_dir / "index.html"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Gather results from all agents (re-reads outcome files)
    base_commit = get_base_commit(source_dir, mng_ctx.concurrency_group)
    is_remote_provider = any(
        d.host is not None and d.host.provider_name != LOCAL_PROVIDER_NAME for d in matching_agents
    )
    results = gather_results(
        agents=agent_infos,
        final_details=final_details,
        timed_out_ids=set(),
        hosts=agent_hosts,
        source_dir=source_dir,
        cg=mng_ctx.concurrency_group,
        base_commit=base_commit if is_remote_provider else None,
    )

    # Pull artifacts
    for info in agent_infos:
        agent_id_str = str(info.agent_id)
        if agent_id_str in agent_hosts:
            pull_test_outputs_by_id(info.agent_id, info.agent_name, agent_hosts[agent_id_str], source_host, output_dir)

    # Write pre-integrator report
    generate_html_report(results, html_path, test_artifacts_dir=output_dir)

    # Run integrator
    env_options = AgentEnvironmentOptions(env_vars=resolve_env_vars((), opts.env))
    label_options = resolve_labels(opts.label)
    integrator_config = TmrLaunchConfig(
        source_dir=source_dir,
        source_host=source_host,
        agent_type=AgentTypeName(opts.agent_type),
        provider_name=ProviderInstanceName(opts.integrator_provider),
        env_options=env_options,
        label_options=label_options,
    )
    integrator_result = _run_integrator_phase(results, integrator_config, mng_ctx, opts, base_commit=base_commit)
    generate_html_report(results, html_path, integrator=integrator_result, test_artifacts_dir=output_dir)
    _emit_report_path(html_path, output_opts)


def _run_integrator_phase(
    results: list[TestMapReduceResult],
    config: TmrLaunchConfig,
    mng_ctx: MngContext,
    opts: TmrCliOptions,
    base_commit: str | None = None,
) -> IntegratorResult | None:
    """Launch an integrator agent to cherry-pick all fix branches into a linear stack.

    All pullable branches are integrated. Test/doc/tutorial commits are squashed
    into one commit; FIX_IMPL commits are kept separate and stacked by priority.
    """
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
            agent_name=integrator.agent_name,
            branch_name=integrator_branch,
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
    help="Maximum number of agents to launch concurrently (launch-time parallelism)",
)
@click.option(
    "--max-agents",
    default=0,
    show_default=True,
    type=int,
    help="Maximum number of agents running at any one time (0 = no limit). "
    "When set, agents are launched incrementally as earlier ones finish.",
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
    help="Maximum seconds each agent can run before being stopped (per-agent timeout)",
)
@click.option(
    "--result-check-interval",
    default=300.0,
    show_default=True,
    type=float,
    help="Seconds between direct result file checks for agents whose status may be stale",
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
@click.option(
    "--reintegrate",
    default=None,
    help="Re-read outcomes from a previous TMR run (by run name), re-run integrator, and regenerate report. "
    "Skips test collection and agent launching.",
)
@add_common_options
@click.pass_context
def tmr(ctx: click.Context, **kwargs: object) -> None:
    mng_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="tmr",
        command_class=TmrCliOptions,
    )

    # Raise the soft FD limit to handle many concurrent agents.
    # Each agent process (tmux + claude) opens many files, and list_agents
    # enumerates all hosts which can push the system near the FD limit.
    _raise_fd_limit()

    source_dir = Path(opts.source) if opts.source is not None else Path.cwd()

    if opts.reintegrate is not None:
        _run_reintegrate(opts, mng_ctx, output_opts, source_dir)
        return

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

    # Step 5: Generate a shared run name prefix for e2e test output.
    # Agents append _try_1, _try_2 etc. for each test run.
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    e2e_run_prefix = f"{timestamp}_tmr"
    testing_flags = testing_flags + ("--mng-e2e-run-name", e2e_run_prefix)

    # Add tmr_run_name label to all testing agents for discovery during reintegrate
    run_labels = dict(label_options.labels)
    run_labels["tmr_run_name"] = e2e_run_prefix
    label_options = AgentLabelOptions(labels=run_labels)

    config = TmrLaunchConfig(
        source_dir=source_dir,
        source_host=source_host,
        agent_type=AgentTypeName(opts.agent_type),
        provider_name=ProviderInstanceName(opts.provider),
        env_options=env_options,
        label_options=label_options,
        snapshot=provided_snapshot,
    )

    # Step 6: Compute output directory and html_path before launching
    if opts.output_html is not None:
        html_path = Path(opts.output_html)
        output_dir = html_path.parent
    else:
        output_dir = Path(f"tmr_{timestamp}")
        html_path = output_dir / "index.html"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Step 7: Launch and poll agents
    # When max_agents > 0, agents are launched incrementally as earlier ones finish.
    # Otherwise, all agents are launched up front and then polled via the same function.
    use_batched = opts.max_agents > 0 and opts.max_agents < len(test_node_ids)

    if use_batched:
        if opts.use_snapshot:
            write_human_line("WARNING: --use-snapshot is not supported with --max-agents and will be ignored")
        agent_infos: list[TestAgentInfo] = []
        agent_hosts: dict[str, OnlineHostInterface] = {}
        remaining_node_ids = test_node_ids
    else:
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
        remaining_node_ids = []

    final_details, timed_out_ids = launch_and_poll_agents(
        test_node_ids=remaining_node_ids,
        config=config,
        mng_ctx=mng_ctx,
        pytest_flags=testing_flags,
        prompt_suffix=opts.prompt_suffix or "",
        max_agents=opts.max_agents,
        agent_timeout_seconds=opts.timeout,
        poll_interval_seconds=opts.poll_interval,
        result_check_interval_seconds=opts.result_check_interval,
        report_path=html_path,
        all_agents=agent_infos,
        all_hosts=agent_hosts,
        artifact_output_dir=output_dir,
        local_host=source_host,
    )

    if use_batched:
        _emit_agents_launched(len(agent_infos), output_opts)

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

    # Step 9: Write report with final results (artifacts already pulled during polling)
    generate_html_report(results, html_path, test_artifacts_dir=output_dir)

    # Step 10: Build integrator config (defaults to local provider) and integrate
    integrator_config = TmrLaunchConfig(
        source_dir=source_dir,
        source_host=source_host,
        agent_type=AgentTypeName(opts.agent_type),
        provider_name=ProviderInstanceName(opts.integrator_provider),
        env_options=env_options,
        label_options=label_options,
    )
    integrator_result = _run_integrator_phase(results, integrator_config, mng_ctx, opts, base_commit=base_commit)
    generate_html_report(results, html_path, integrator=integrator_result, test_artifacts_dir=output_dir)
    _emit_report_path(html_path, output_opts)

    # Print useful commands for managing this run's agents
    write_human_line("")
    write_human_line("List agents from this run:")
    write_human_line("  mng ls --include 'labels.tmr_run_name == \"{}\"'", e2e_run_prefix)
    write_human_line("Reintegrate (after sending followup messages to agents):")
    write_human_line("  mng tmr --reintegrate {}", e2e_run_prefix)


CommandHelpMetadata(
    key="tmr",
    one_line_description="Run and fix tests in parallel using agents (test map-reduce)",
    synopsis="mng tmr [TEST_PATHS...] [-- TESTING_FLAGS...] [--provider <PROVIDER>] [--use-snapshot] [--env KEY=VALUE] [--label KEY=VALUE] [--timeout <SECS>] [--agent-type <TYPE>]",
    description="""This command implements a map-reduce pattern for tests:

1. Collects tests using pytest --collect-only, passing through all arguments.
2. Launches one agent per test. Each agent runs the test and, if it fails,
   attempts to diagnose and fix either the test code or the implementation.
3. Polls agents until all finish or individually time out (per-agent timeout).
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
Use --max-agents to limit how many agents run simultaneously (0 = no limit).

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
        ("Limit to 4 concurrent agents", "mng tmr --max-agents 4 tests/"),
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
