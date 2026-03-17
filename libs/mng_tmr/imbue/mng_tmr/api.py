"""Core logic for the test-mapreduce plugin.

Implements the map-reduce pattern: collect tests via pytest, launch an agent per
test, poll for completion, gather results, pull code changes, and generate an
HTML report.
"""

import html
import json
import secrets
import time
from pathlib import Path

from loguru import logger
from markdown_it import MarkdownIt

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.executor import ConcurrencyGroupExecutor
from imbue.mng.api.create import create as api_create
from imbue.mng.api.data_types import CreateAgentResult
from imbue.mng.api.list import list_agents
from imbue.mng.api.pull import pull_git
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import AgentNotFoundOnHostError
from imbue.mng.errors import MngError
from imbue.mng.hosts.host import HostLocation
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.data_types import AgentDetails
from imbue.mng.interfaces.host import AgentGitOptions
from imbue.mng.interfaces.host import CreateAgentOptions
from imbue.mng.interfaces.host import NewHostOptions
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import ErrorBehavior
from imbue.mng.primitives import LOCAL_PROVIDER_NAME
from imbue.mng.primitives import UncommittedChangesMode
from imbue.mng.primitives import WorkDirCopyMode
from imbue.mng_tmr.data_types import TestAgentInfo
from imbue.mng_tmr.data_types import TestMapReduceResult
from imbue.mng_tmr.data_types import TestOutcome
from imbue.mng_tmr.data_types import TestResult

PLUGIN_NAME = "test-map-reduce"

_TERMINAL_STATES = frozenset(
    {
        AgentLifecycleState.DONE,
        AgentLifecycleState.STOPPED,
        AgentLifecycleState.WAITING,
    }
)

_OUTCOME_COLORS: dict[TestOutcome, str] = {
    TestOutcome.PENDING: "rgb(3, 169, 244)",
    TestOutcome.FIX_TEST_SUCCEEDED: "rgb(33, 150, 243)",
    TestOutcome.FIX_IMPL_SUCCEEDED: "rgb(33, 150, 243)",
    TestOutcome.FIX_TEST_FAILED: "rgb(244, 67, 54)",
    TestOutcome.FIX_IMPL_FAILED: "rgb(244, 67, 54)",
    TestOutcome.FIX_UNCERTAIN: "rgb(255, 152, 0)",
    TestOutcome.TIMED_OUT: "rgb(121, 85, 72)",
    TestOutcome.RUN_SUCCEEDED: "rgb(76, 175, 80)",
    TestOutcome.AGENT_ERROR: "rgb(158, 158, 158)",
}

_OUTCOME_GROUP_ORDER: list[TestOutcome] = [
    TestOutcome.PENDING,
    TestOutcome.FIX_IMPL_SUCCEEDED,
    TestOutcome.FIX_TEST_SUCCEEDED,
    TestOutcome.FIX_IMPL_FAILED,
    TestOutcome.FIX_TEST_FAILED,
    TestOutcome.FIX_UNCERTAIN,
    TestOutcome.TIMED_OUT,
    TestOutcome.AGENT_ERROR,
    TestOutcome.RUN_SUCCEEDED,
]

_SHORT_ID_LENGTH = 6

_MISSING_AGENT_MAX_ROUNDS = 30

_md = MarkdownIt()


class CollectTestsError(MngError, RuntimeError):
    """Raised when pytest test collection fails."""

    ...


def _short_random_id() -> str:
    """Generate a short random hex suffix for agent name uniqueness."""
    return secrets.token_hex(_SHORT_ID_LENGTH // 2)


def collect_tests(
    pytest_args: tuple[str, ...],
    source_dir: Path,
    cg: ConcurrencyGroup,
) -> list[str]:
    """Run pytest --collect-only -q and return the list of test node IDs."""
    cmd = ["python", "-m", "pytest", "--collect-only", "-q", *pytest_args]
    logger.info("Collecting tests: {}", " ".join(cmd))
    result = cg.run_process_to_completion(cmd, cwd=source_dir, timeout=60.0, is_checked_after=False)
    if result.returncode != 0:
        raise CollectTestsError(f"pytest --collect-only failed (exit code {result.returncode}):\n{result.stderr}")

    test_ids: list[str] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped and "::" in stripped and not stripped.startswith("="):
            test_ids.append(stripped)

    if not test_ids:
        raise CollectTestsError("pytest --collect-only returned no tests")

    logger.info("Collected {} test(s)", len(test_ids))
    return test_ids


def _build_agent_prompt(test_node_id: str, pytest_flags: tuple[str, ...]) -> str:
    """Build the prompt/initial message for a test-running agent."""
    flags_str = " ".join(pytest_flags)
    run_cmd = f"pytest {test_node_id}"
    if flags_str:
        run_cmd += f" {flags_str}"

    return f"""Run the test with: {run_cmd}

If the test succeeds, there is nothing more to do (outcome = RUN_SUCCEEDED).

If the test fails:

- If you are certain that the test code itself has issues (including test fixture
  code), fix the test code itself. Depending on whether the fix was successful,
  the outcome should be FIX_TEST_SUCCEEDED or FIX_TEST_FAILED.

- If you are certain that the program being tested has issues, fix the program
  itself. Depending on whether the fix was successful, the outcome should be
  FIX_IMPL_SUCCEEDED or FIX_IMPL_FAILED.

- If you are not certain which one is the case, do not try to fix anything. The
  outcome is FIX_UNCERTAIN.

In all cases, also generate a short summary in **markdown** format, and write the
result to a JSON file at $MNG_AGENT_STATE_DIR/plugin/{PLUGIN_NAME}/result.json,
with content like:
{{"outcome": "RUN_SUCCEEDED", "summary": "Test passed on first run."}}

Valid outcome values: RUN_SUCCEEDED, FIX_TEST_SUCCEEDED, FIX_TEST_FAILED,
FIX_IMPL_SUCCEEDED, FIX_IMPL_FAILED, FIX_UNCERTAIN.
"""


def _sanitize_test_name_for_agent(test_node_id: str) -> str:
    """Convert a pytest node ID into a valid agent name suffix.

    Strips the file path prefix and replaces characters that are not valid in
    agent names.
    """
    parts = test_node_id.split("::")
    short_name = parts[-1] if parts else test_node_id
    cleaned = ""
    for ch in short_name:
        if ch.isalnum() or ch == "-":
            cleaned += ch
        else:
            cleaned += "-"
    sanitized = ""
    for ch in cleaned:
        if ch == "-" and sanitized.endswith("-"):
            continue
        sanitized += ch
    return sanitized.strip("-").lower()[:40]


def launch_test_agent(
    test_node_id: str,
    source_dir: Path,
    local_host: OnlineHostInterface,
    mng_ctx: MngContext,
    agent_type: AgentTypeName,
    pytest_flags: tuple[str, ...],
) -> TestAgentInfo:
    """Launch a single agent to run and optionally fix one test."""
    agent_name_suffix = _sanitize_test_name_for_agent(test_node_id)
    short_id = _short_random_id()
    agent_name = AgentName(f"tmr-{agent_name_suffix}-{short_id}")
    prompt = _build_agent_prompt(test_node_id, pytest_flags)

    agent_options = CreateAgentOptions(
        agent_type=agent_type,
        name=agent_name,
        initial_message=prompt,
        git=AgentGitOptions(
            copy_mode=WorkDirCopyMode.WORKTREE,
            new_branch_name=f"mng-tmr/{agent_name_suffix}-{short_id}",
        ),
    )

    source_location = HostLocation(host=local_host, path=source_dir)
    target_host = NewHostOptions(provider=LOCAL_PROVIDER_NAME)

    logger.info("Launching agent '{}' for test: {}", agent_name, test_node_id)
    create_result: CreateAgentResult = api_create(
        source_location=source_location,
        target_host=target_host,
        agent_options=agent_options,
        mng_ctx=mng_ctx,
    )

    return TestAgentInfo(
        test_node_id=test_node_id,
        agent_id=create_result.agent.id,
        agent_name=create_result.agent.name,
    )


def launch_all_test_agents(
    test_node_ids: list[str],
    source_dir: Path,
    local_host: OnlineHostInterface,
    mng_ctx: MngContext,
    agent_type: AgentTypeName,
    pytest_flags: tuple[str, ...],
) -> list[TestAgentInfo]:
    """Launch agents for all collected tests, returning tracking info for each."""
    agents: list[TestAgentInfo] = []
    with ConcurrencyGroupExecutor(
        parent_cg=mng_ctx.concurrency_group,
        name="tmr_launch",
        max_workers=8,
    ) as executor:
        futures = [
            executor.submit(
                launch_test_agent,
                test_node_id,
                source_dir,
                local_host,
                mng_ctx,
                agent_type,
                pytest_flags,
            )
            for test_node_id in test_node_ids
        ]
        for future in futures:
            agents.append(future.result())
    logger.info("Launched {} agent(s)", len(agents))
    return agents


def poll_until_all_done(
    agents: list[TestAgentInfo],
    mng_ctx: MngContext,
    poll_interval_seconds: float,
    host: OnlineHostInterface,
    deadline: float,
    report_path: Path | None = None,
) -> tuple[dict[str, AgentDetails], set[str]]:
    """Poll agents until all have reached a terminal state or the deadline is reached.

    Returns (final_details, timed_out_ids) where final_details maps agent_id
    strings to AgentDetails, and timed_out_ids is the set of agent_id strings
    that were still running when the deadline was reached.

    Agents that disappear from listings are treated as errors after a grace period.
    Agents entering WAITING state are stopped after being recorded.
    When the deadline is reached, all pending agents are stopped unconditionally.

    If report_path is provided, an intermediate HTML report is written after each
    polling round and before returning on timeout.
    """
    pending_ids = {str(info.agent_id) for info in agents}
    agent_id_to_info = {str(info.agent_id): info for info in agents}
    final_details: dict[str, AgentDetails] = {}
    missing_rounds: dict[str, int] = {}

    while pending_ids:
        if time.monotonic() >= deadline:
            logger.warning("Timeout reached with {} agent(s) still pending, stopping them", len(pending_ids))
            for agent_id_str in pending_ids:
                info = agent_id_to_info[agent_id_str]
                _stop_agent_on_host(host, AgentId(agent_id_str), info.agent_name)
            if report_path is not None:
                current_results = build_current_results(agents, final_details, set(pending_ids), host)
                generate_html_report(current_results, report_path)
            return final_details, set(pending_ids)

        logger.info("Polling {} pending agent(s)...", len(pending_ids))
        list_result = list_agents(
            mng_ctx=mng_ctx,
            is_streaming=False,
            error_behavior=ErrorBehavior.CONTINUE,
        )

        seen_ids: set[str] = set()
        changed = False
        for agent_detail in list_result.agents:
            agent_id_str = str(agent_detail.id)
            seen_ids.add(agent_id_str)
            if agent_id_str not in pending_ids:
                continue
            if agent_detail.state not in _TERMINAL_STATES:
                continue

            logger.info(
                "Agent '{}' finished (state={})",
                agent_detail.name,
                agent_detail.state,
            )
            final_details[agent_id_str] = agent_detail
            pending_ids.discard(agent_id_str)
            missing_rounds.pop(agent_id_str, None)
            changed = True

            if agent_detail.state == AgentLifecycleState.WAITING:
                _stop_agent_on_host(host, agent_detail.id, agent_detail.name)

        for agent_id_str in list(pending_ids):
            if agent_id_str not in seen_ids:
                rounds = missing_rounds.get(agent_id_str, 0) + 1
                missing_rounds[agent_id_str] = rounds
                if rounds >= _MISSING_AGENT_MAX_ROUNDS:
                    logger.warning("Agent {} disappeared after {} rounds, treating as error", agent_id_str, rounds)
                    pending_ids.discard(agent_id_str)
                    changed = True

        if changed and report_path is not None:
            current_results = build_current_results(agents, final_details, set(), host)
            generate_html_report(current_results, report_path)

        if pending_ids:
            time.sleep(poll_interval_seconds)

    return final_details, set()


def _stop_agent_on_host(host: OnlineHostInterface, agent_id: AgentId, agent_name: AgentName) -> None:
    """Stop a single agent on the host."""
    try:
        host.stop_agents([agent_id])
        logger.info("Stopped agent '{}'", agent_name)
    except MngError as exc:
        logger.warning("Failed to stop agent '{}': {}", agent_name, exc)


def read_agent_result(
    agent_detail: AgentDetails,
    host: OnlineHostInterface,
) -> TestResult:
    """Read the result.json from a finished agent's state directory."""
    agent_state_dir = host.host_dir / "agents" / str(agent_detail.id)
    result_path = agent_state_dir / "plugin" / PLUGIN_NAME / "result.json"

    try:
        raw = host.read_text_file(result_path)
        data = json.loads(raw)
        return TestResult(
            outcome=TestOutcome(data["outcome"]),
            summary=data.get("summary", ""),
        )
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.warning("Failed to read result from agent {}: {}", agent_detail.name, exc)
        return TestResult(
            outcome=TestOutcome.AGENT_ERROR,
            summary=f"Failed to read agent result: {exc}",
        )


def pull_agent_branch(
    agent_detail: AgentDetails,
    host: OnlineHostInterface,
    destination: Path,
    cg: ConcurrencyGroup,
) -> str | None:
    """Pull the agent's git branch into the local repo.

    Returns the branch name if successful, None otherwise.
    """
    branch_name = agent_detail.initial_branch
    if branch_name is None:
        logger.warning("Agent '{}' has no branch to pull", agent_detail.name)
        return None

    try:
        pull_git(
            agent=_get_agent_from_host(host, agent_detail.id),
            host=host,
            destination=destination,
            source_branch=branch_name,
            target_branch=branch_name,
            is_dry_run=False,
            uncommitted_changes=UncommittedChangesMode.STASH,
            cg=cg,
        )
        logger.info("Pulled branch '{}' from agent '{}'", branch_name, agent_detail.name)
        return branch_name
    except MngError as exc:
        logger.warning("Failed to pull branch from agent '{}': {}", agent_detail.name, exc)
        return None


def _get_agent_from_host(
    host: OnlineHostInterface,
    agent_id: AgentId,
) -> AgentInterface:
    """Look up an agent on a host by ID."""
    for agent in host.get_agents():
        if agent.id == agent_id:
            return agent
    raise AgentNotFoundOnHostError(agent_id, host.id)


def _collect_agent_results(
    agents: list[TestAgentInfo],
    final_details: dict[str, AgentDetails],
    timed_out_ids: set[str],
    host: OnlineHostInterface,
    missing_detail_outcome: TestOutcome,
    missing_detail_summary: str,
) -> list[TestMapReduceResult]:
    """Shared iteration over agents to build result list.

    Each agent is classified as timed-out, missing (with the caller-specified
    outcome), or finished (result read from the agent's state directory).
    """
    results: list[TestMapReduceResult] = []

    for agent_info in agents:
        agent_id_str = str(agent_info.agent_id)

        if agent_id_str in timed_out_ids:
            results.append(
                TestMapReduceResult(
                    test_node_id=agent_info.test_node_id,
                    agent_name=agent_info.agent_name,
                    outcome=TestOutcome.TIMED_OUT,
                    summary="Agent was stopped because the timeout was reached.",
                )
            )
            continue

        detail = final_details.get(agent_id_str)

        if detail is None:
            results.append(
                TestMapReduceResult(
                    test_node_id=agent_info.test_node_id,
                    agent_name=agent_info.agent_name,
                    outcome=missing_detail_outcome,
                    summary=missing_detail_summary,
                )
            )
            continue

        test_result = read_agent_result(detail, host)
        results.append(
            TestMapReduceResult(
                test_node_id=agent_info.test_node_id,
                agent_name=agent_info.agent_name,
                outcome=test_result.outcome,
                summary=test_result.summary,
                branch_name=detail.initial_branch,
            )
        )

    return results


def gather_results(
    agents: list[TestAgentInfo],
    final_details: dict[str, AgentDetails],
    timed_out_ids: set[str],
    host: OnlineHostInterface,
    source_dir: Path,
    cg: ConcurrencyGroup,
) -> list[TestMapReduceResult]:
    """Gather results from all finished agents, pulling branches where appropriate."""
    results = _collect_agent_results(
        agents=agents,
        final_details=final_details,
        timed_out_ids=timed_out_ids,
        host=host,
        missing_detail_outcome=TestOutcome.AGENT_ERROR,
        missing_detail_summary="Agent details not found after polling",
    )

    # Pull branches for successful fixes
    for result in results:
        if result.outcome in (TestOutcome.FIX_TEST_SUCCEEDED, TestOutcome.FIX_IMPL_SUCCEEDED):
            agent_id_str = next(str(info.agent_id) for info in agents if info.test_node_id == result.test_node_id)
            detail = final_details.get(agent_id_str)
            if detail is not None:
                pull_agent_branch(detail, host, source_dir, cg)

    return results


def build_current_results(
    agents: list[TestAgentInfo],
    final_details: dict[str, AgentDetails],
    timed_out_ids: set[str],
    host: OnlineHostInterface,
) -> list[TestMapReduceResult]:
    """Build current results without pulling branches, for intermediate reports."""
    return _collect_agent_results(
        agents=agents,
        final_details=final_details,
        timed_out_ids=timed_out_ids,
        host=host,
        missing_detail_outcome=TestOutcome.PENDING,
        missing_detail_summary="Agent is still running...",
    )


def generate_html_report(
    results: list[TestMapReduceResult],
    output_path: Path,
    integrator_branch: str | None = None,
) -> Path:
    """Generate an HTML report summarizing test-mapreduce results."""
    counts: dict[TestOutcome, int] = {}
    for r in results:
        counts[r.outcome] = counts.get(r.outcome, 0) + 1

    summary_parts = [
        f"{outcome.value}: {count}" for outcome, count in sorted(counts.items(), key=lambda x: x[0].value)
    ]
    summary_text = ", ".join(summary_parts)

    bar_html = _build_stacked_bar(counts, len(results))
    tables_html = _build_grouped_tables(results)

    integrator_html = ""
    if integrator_branch is not None:
        escaped_branch = html.escape(integrator_branch)
        integrator_html = f'  <p class="integrator">Integrated branch: <code>{escaped_branch}</code></p>\n'

    css = _html_report_css()
    report_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Test Map-Reduce Report</title>
  <style>
{css}
  </style>
</head>
<body>
  <h1>Test Map-Reduce Report</h1>
  <p class="summary">{len(results)} test(s) -- {summary_text}</p>
{integrator_html}{bar_html}
{tables_html}
</body>
</html>
"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report_html)
    logger.info("HTML report written to {}", output_path)
    return output_path


def _build_stacked_bar(counts: dict[TestOutcome, int], total: int) -> str:
    """Build an HTML stacked bar showing outcome distribution."""
    if total == 0:
        return ""
    segments = ""
    for outcome in _OUTCOME_GROUP_ORDER:
        count = counts.get(outcome, 0)
        if count == 0:
            continue
        pct = count / total * 100
        color = _OUTCOME_COLORS.get(outcome, "rgb(158, 158, 158)")
        segments += (
            f'    <div style="width: {pct:.1f}%; background: {color};" title="{outcome.value}: {count}"></div>\n'
        )
    return f'  <div class="bar">\n{segments}  </div>'


def _render_markdown(text: str) -> str:
    """Render markdown text to HTML."""
    return _md.render(text)


def _build_grouped_tables(results: list[TestMapReduceResult]) -> str:
    """Build HTML tables grouped by outcome, with RUN_SUCCEEDED last."""
    grouped: dict[TestOutcome, list[TestMapReduceResult]] = {}
    for r in results:
        grouped.setdefault(r.outcome, []).append(r)

    sections = ""
    for outcome in _OUTCOME_GROUP_ORDER:
        group = grouped.get(outcome)
        if not group:
            continue
        color = _OUTCOME_COLORS.get(outcome, "rgb(158, 158, 158)")
        sections += f'  <h2 style="color: {color};">{outcome.value} ({len(group)})</h2>\n'
        sections += "  <table>\n    <thead>\n      <tr>"
        sections += "<th>Test</th><th>Summary</th><th>Agent</th><th>Branch</th>"
        sections += "</tr>\n    </thead>\n    <tbody>\n"
        for r in group:
            branch_cell = r.branch_name if r.branch_name else "-"
            summary_html = _render_markdown(r.summary)
            sections += f"""      <tr>
        <td>{html.escape(r.test_node_id)}</td>
        <td class="md">{summary_html}</td>
        <td><code>{html.escape(str(r.agent_name))}</code></td>
        <td><code>{html.escape(branch_cell)}</code></td>
      </tr>
"""
        sections += "    </tbody>\n  </table>\n"

    return sections


def _html_report_css() -> str:
    """Return the CSS stylesheet for the HTML report.

    Uses rgb() colors instead of hex to avoid ratchet false positives.
    """
    return (
        "    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 2rem; }\n"
        "    h1 { color: rgb(51, 51, 51); }\n"
        "    h2 { margin-top: 1.5rem; font-size: 1.1rem; }\n"
        "    .summary { margin-bottom: 0.5rem; color: rgb(102, 102, 102); }\n"
        "    .bar { display: flex; height: 24px; border-radius: 4px; overflow: hidden;"
        " margin-bottom: 1.5rem; }\n"
        "    .bar > div { min-width: 2px; }\n"
        "    table { border-collapse: collapse; width: 100%; margin-bottom: 1rem; }\n"
        "    th, td { border: 1px solid rgb(221, 221, 221); padding: 8px 12px; text-align: left; }\n"
        "    th { background: rgb(245, 245, 245); font-weight: 600; }\n"
        "    tr:hover { background: rgb(250, 250, 250); }\n"
        "    td.md p { margin: 0.25em 0; }\n"
        "    td.md p:first-child { margin-top: 0; }\n"
        "    td.md p:last-child { margin-bottom: 0; }\n"
        "    code { background: rgb(240, 240, 240); padding: 2px 4px; border-radius: 3px; font-size: 0.9em; }\n"
        "    .integrator { color: rgb(33, 150, 243); font-weight: 600; }"
    )


def launch_integrator_agent(
    fix_branches: list[str],
    source_dir: Path,
    local_host: OnlineHostInterface,
    mng_ctx: MngContext,
    agent_type: AgentTypeName,
) -> TestAgentInfo:
    """Launch an integrator agent that merges all fix branches into one."""
    short_id = _short_random_id()
    agent_name = AgentName(f"tmr-integrator-{short_id}")
    branch_name = f"mng-tmr/integrated-{short_id}"

    branch_list = "\n".join(f"  - {b}" for b in fix_branches)
    prompt = f"""Merge the following branches into one integrated branch:
{branch_list}

For each branch, run `git merge <branch>` (resolve conflicts if needed).
After merging all branches, verify that the code still compiles/passes basic checks.
Write the result to $MNG_AGENT_STATE_DIR/plugin/{PLUGIN_NAME}/result.json with:
{{"outcome": "FIX_IMPL_SUCCEEDED", "summary": "Merged {len(fix_branches)} branches successfully."}}
If merging fails, use outcome FIX_IMPL_FAILED.
"""

    agent_options = CreateAgentOptions(
        agent_type=agent_type,
        name=agent_name,
        initial_message=prompt,
        git=AgentGitOptions(
            copy_mode=WorkDirCopyMode.WORKTREE,
            new_branch_name=branch_name,
        ),
    )

    source_location = HostLocation(host=local_host, path=source_dir)
    target_host = NewHostOptions(provider=LOCAL_PROVIDER_NAME)

    logger.info("Launching integrator agent '{}' to merge {} branches", agent_name, len(fix_branches))
    create_result: CreateAgentResult = api_create(
        source_location=source_location,
        target_host=target_host,
        agent_options=agent_options,
        mng_ctx=mng_ctx,
    )

    return TestAgentInfo(
        test_node_id="integrator",
        agent_id=create_result.agent.id,
        agent_name=create_result.agent.name,
    )


def wait_for_integrator(
    integrator: TestAgentInfo,
    mng_ctx: MngContext,
    poll_interval_seconds: float,
    host: OnlineHostInterface,
    deadline: float,
) -> str | None:
    """Poll the integrator agent until it finishes or times out.

    Returns the integrator's branch name if it finished successfully,
    None if it timed out or errored.
    """
    agent_id_str = str(integrator.agent_id)

    while time.monotonic() < deadline:
        list_result = list_agents(
            mng_ctx=mng_ctx,
            is_streaming=False,
            error_behavior=ErrorBehavior.CONTINUE,
        )

        for agent_detail in list_result.agents:
            if str(agent_detail.id) != agent_id_str:
                continue

            if agent_detail.state == AgentLifecycleState.WAITING:
                _stop_agent_on_host(host, agent_detail.id, agent_detail.name)
                return agent_detail.initial_branch

            if agent_detail.state in _TERMINAL_STATES:
                logger.info("Integrator agent finished (state={})", agent_detail.state)
                return agent_detail.initial_branch

        time.sleep(poll_interval_seconds)

    logger.warning("Integrator agent timed out, stopping it")
    _stop_agent_on_host(host, AgentId(agent_id_str), integrator.agent_name)
    return None
