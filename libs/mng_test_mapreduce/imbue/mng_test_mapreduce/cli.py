"""CLI command for test-mapreduce."""

from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import assert_never

import click

from imbue.mng.api.find import ensure_host_started
from imbue.mng.api.providers import get_provider_instance
from imbue.mng.cli.common_opts import CommonCliOptions
from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng.cli.help_formatter import CommandHelpMetadata
from imbue.mng.cli.help_formatter import add_pager_help_option
from imbue.mng.cli.output_helpers import emit_event
from imbue.mng.cli.output_helpers import write_human_line
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import HostName
from imbue.mng.primitives import LOCAL_PROVIDER_NAME
from imbue.mng.primitives import OutputFormat
from imbue.mng_test_mapreduce.api import collect_tests
from imbue.mng_test_mapreduce.api import gather_results
from imbue.mng_test_mapreduce.api import generate_html_report
from imbue.mng_test_mapreduce.api import launch_all_test_agents
from imbue.mng_test_mapreduce.api import poll_until_all_done


class TmrCliOptions(CommonCliOptions):
    """Options passed from the CLI to the tmr command."""

    pytest_args: tuple[str, ...]
    agent_type: str
    poll_interval: float
    output_html: str | None
    source: str | None


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


@click.command("tmr", context_settings={"ignore_unknown_options": True})
@click.argument("pytest_args", nargs=-1, type=click.UNPROCESSED)
@click.option(
    "--agent-type",
    default="claude",
    show_default=True,
    help="Type of agent to launch for each test",
)
@click.option(
    "--poll-interval",
    default=10.0,
    show_default=True,
    type=float,
    help="Seconds between polling cycles when waiting for agents to finish",
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
    agent_type = AgentTypeName(opts.agent_type)

    # Step 1: Collect tests
    test_node_ids = collect_tests(
        pytest_args=opts.pytest_args,
        source_dir=source_dir,
        cg=mng_ctx.concurrency_group,
    )
    _emit_test_count(len(test_node_ids), output_opts)

    # Step 2: Get the local host (same pattern as `mng create`)
    provider = get_provider_instance(LOCAL_PROVIDER_NAME, mng_ctx)
    host = provider.get_host(HostName("localhost"))
    local_host, _ = ensure_host_started(host, is_start_desired=True, provider=provider)

    # Step 3: Launch agents
    agent_infos = launch_all_test_agents(
        test_node_ids=test_node_ids,
        source_dir=source_dir,
        local_host=local_host,
        mng_ctx=mng_ctx,
        agent_type=agent_type,
    )
    _emit_agents_launched(len(agent_infos), output_opts)

    # Step 4: Poll until all agents are done
    final_details = poll_until_all_done(
        agents=agent_infos,
        mng_ctx=mng_ctx,
        poll_interval_seconds=opts.poll_interval,
    )

    # Step 5: Gather results (read result.json, pull branches for fixes)
    results = gather_results(
        agents=agent_infos,
        final_details=final_details,
        host=local_host,
        source_dir=source_dir,
        cg=mng_ctx.concurrency_group,
    )

    # Step 6: Generate HTML report
    if opts.output_html is not None:
        html_path = Path(opts.output_html)
    else:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        html_path = Path(f"tmr-report-{timestamp}.html")

    generate_html_report(results, html_path)
    _emit_report_path(html_path, output_opts)

    # Print a summary in human mode
    if output_opts.output_format == OutputFormat.HUMAN:
        for r in results:
            branch_info = f" -> {r.branch_name}" if r.branch_name else ""
            write_human_line(
                "  {} [{}] {}{}",
                r.outcome.value,
                r.agent_name,
                r.summary,
                branch_info,
            )


CommandHelpMetadata(
    key="tmr",
    one_line_description="Run and fix tests in parallel using agents (test map-reduce)",
    synopsis="mng tmr [PYTEST_ARGS...] [--agent-type <TYPE>] [--poll-interval <SECS>]",
    description="""This command implements a map-reduce pattern for tests:

1. Collects tests using pytest --collect-only, passing through any arguments.
2. Launches one agent per test. Each agent runs the test and, if it fails,
   attempts to diagnose and fix either the test code or the implementation.
3. Polls agents until all finish, then collects results.
4. For successful fixes, pulls the agent's code changes into branches
   named mng-tmr/*.
5. Generates an HTML report summarizing all outcomes.

Each agent writes its result to $MNG_AGENT_STATE_DIR/plugin/test-map-reduce/result.json
with an outcome enum and a short summary.""",
    examples=(
        ("Run all tests in current directory", "mng tmr"),
        ("Run tests in a specific file", "mng tmr tests/test_foo.py"),
        ("Run a specific test", "mng tmr tests/test_foo.py::test_bar"),
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
