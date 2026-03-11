"""Core logic for the test-mapreduce plugin.

Implements the map-reduce pattern: collect tests via pytest, launch an agent per
test, poll for completion, gather results, pull code changes, and generate an
HTML report.
"""

import json
import time
from pathlib import Path

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.executor import ConcurrencyGroupExecutor
from imbue.imbue_common.frozen_model import FrozenModel
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
from imbue.mng_test_mapreduce.data_types import TestAgentInfo
from imbue.mng_test_mapreduce.data_types import TestMapReduceResult
from imbue.mng_test_mapreduce.data_types import TestOutcome
from imbue.mng_test_mapreduce.data_types import TestResult

PLUGIN_NAME = "test-map-reduce"

_TERMINAL_STATES = frozenset(
    {
        AgentLifecycleState.DONE,
        AgentLifecycleState.STOPPED,
        AgentLifecycleState.WAITING,
    }
)


class CollectTestsError(MngError, RuntimeError):
    """Raised when pytest test collection fails."""

    ...


class ReadResultError(MngError, RuntimeError):
    """Raised when reading a test result from an agent fails."""

    ...


class TestMapReduceParams(FrozenModel):
    """Parameters for a test-mapreduce run."""

    pytest_args: tuple[str, ...] = Field(description="Arguments to pass through to pytest --collect-only")
    source_dir: Path = Field(description="Directory to run pytest collection from and to use as agent source")
    agent_type: AgentTypeName = Field(
        default=AgentTypeName("claude"),
        description="Type of agent to launch for each test",
    )
    poll_interval_seconds: float = Field(
        default=10.0,
        description="Seconds between polling cycles",
    )
    provider: str = Field(
        default=str(LOCAL_PROVIDER_NAME),
        description="Provider to use for agent hosts",
    )
    output_html_path: Path | None = Field(
        default=None,
        description="Where to write the HTML report (None means auto-generated name)",
    )


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
        # pytest -q --collect-only outputs lines like "tests/test_foo.py::test_bar"
        # Skip blank lines, summary lines, and lines that don't contain "::"
        if stripped and "::" in stripped and not stripped.startswith("="):
            test_ids.append(stripped)

    if not test_ids:
        raise CollectTestsError("pytest --collect-only returned no tests")

    logger.info("Collected {} test(s)", len(test_ids))
    return test_ids


def _build_agent_prompt(test_node_id: str) -> str:
    """Build the prompt/initial message for a test-running agent."""
    return f"""Run the test: {test_node_id}

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

In all cases, also generate a short summary, and write the result to a JSON file
at $MNG_AGENT_STATE_DIR/plugin/{PLUGIN_NAME}/result.json, with content like:
{{"outcome": "RUN_SUCCEEDED", "summary": "Test passed on first run."}}

Valid outcome values: RUN_SUCCEEDED, FIX_TEST_SUCCEEDED, FIX_TEST_FAILED,
FIX_IMPL_SUCCEEDED, FIX_IMPL_FAILED, FIX_UNCERTAIN.
"""


def _sanitize_test_name_for_agent(test_node_id: str) -> str:
    """Convert a pytest node ID into a valid agent name suffix.

    Strips the file path prefix and replaces characters that are not valid in
    agent names.
    """
    # Take just the test function/method name for brevity
    # e.g. "tests/test_foo.py::TestClass::test_bar" -> "test_bar"
    parts = test_node_id.split("::")
    short_name = parts[-1] if parts else test_node_id
    # Replace non-alphanumeric chars with hyphens, collapse runs, strip edges
    cleaned = ""
    for ch in short_name:
        if ch.isalnum() or ch == "-":
            cleaned += ch
        else:
            cleaned += "-"
    # Collapse multiple hyphens
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
) -> TestAgentInfo:
    """Launch a single agent to run and optionally fix one test."""
    agent_name_suffix = _sanitize_test_name_for_agent(test_node_id)
    agent_name = AgentName(f"tmr-{agent_name_suffix}")
    prompt = _build_agent_prompt(test_node_id)

    agent_options = CreateAgentOptions(
        agent_type=agent_type,
        name=agent_name,
        initial_message=prompt,
        git=AgentGitOptions(
            copy_mode=WorkDirCopyMode.WORKTREE,
            new_branch_name=f"mng-tmr/{agent_name_suffix}",
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
) -> dict[str, AgentDetails]:
    """Poll agents until all have reached a terminal state.

    Returns a mapping from agent_id (as string) to AgentDetails.
    """
    pending_ids = {str(info.agent_id) for info in agents}
    final_details: dict[str, AgentDetails] = {}

    while pending_ids:
        logger.info("Polling {} pending agent(s)...", len(pending_ids))
        list_result = list_agents(
            mng_ctx=mng_ctx,
            is_streaming=False,
            error_behavior=ErrorBehavior.CONTINUE,
        )

        for agent_detail in list_result.agents:
            agent_id_str = str(agent_detail.id)
            if agent_id_str in pending_ids and agent_detail.state in _TERMINAL_STATES:
                logger.info(
                    "Agent '{}' finished (state={})",
                    agent_detail.name,
                    agent_detail.state,
                )
                final_details[agent_id_str] = agent_detail
                pending_ids.discard(agent_id_str)

        if pending_ids:
            time.sleep(poll_interval_seconds)

    return final_details


def read_agent_result(
    agent_detail: AgentDetails,
    host: OnlineHostInterface,
) -> TestResult:
    """Read the result.json from a finished agent's state directory."""
    # The result file is at $MNG_AGENT_STATE_DIR/plugin/test-map-reduce/result.json
    # On the host, that maps to: host_dir/agents/<agent_id>/plugin/<plugin_name>/result.json
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


def gather_results(
    agents: list[TestAgentInfo],
    final_details: dict[str, AgentDetails],
    host: OnlineHostInterface,
    source_dir: Path,
    cg: ConcurrencyGroup,
) -> list[TestMapReduceResult]:
    """Gather results from all finished agents, pulling branches where appropriate."""
    results: list[TestMapReduceResult] = []

    for agent_info in agents:
        agent_id_str = str(agent_info.agent_id)
        detail = final_details.get(agent_id_str)

        if detail is None:
            results.append(
                TestMapReduceResult(
                    test_node_id=agent_info.test_node_id,
                    agent_name=agent_info.agent_name,
                    outcome=TestOutcome.AGENT_ERROR,
                    summary="Agent details not found after polling",
                )
            )
            continue

        test_result = read_agent_result(detail, host)

        # Pull the branch for successful fix outcomes
        branch_name: str | None = None
        if test_result.outcome in (TestOutcome.FIX_TEST_SUCCEEDED, TestOutcome.FIX_IMPL_SUCCEEDED):
            branch_name = pull_agent_branch(detail, host, source_dir, cg)

        results.append(
            TestMapReduceResult(
                test_node_id=agent_info.test_node_id,
                agent_name=agent_info.agent_name,
                outcome=test_result.outcome,
                summary=test_result.summary,
                branch_name=branch_name,
            )
        )

    return results


def generate_html_report(results: list[TestMapReduceResult], output_path: Path) -> Path:
    """Generate an HTML report summarizing test-mapreduce results."""
    outcome_colors = {
        TestOutcome.RUN_SUCCEEDED: "#4caf50",
        TestOutcome.FIX_TEST_SUCCEEDED: "#8bc34a",
        TestOutcome.FIX_IMPL_SUCCEEDED: "#8bc34a",
        TestOutcome.FIX_TEST_FAILED: "#f44336",
        TestOutcome.FIX_IMPL_FAILED: "#f44336",
        TestOutcome.FIX_UNCERTAIN: "#ff9800",
        TestOutcome.AGENT_ERROR: "#9e9e9e",
    }

    rows = ""
    for r in results:
        color = outcome_colors.get(r.outcome, "#9e9e9e")
        branch_cell = r.branch_name if r.branch_name else "-"
        rows += f"""    <tr>
      <td>{_html_escape(r.test_node_id)}</td>
      <td style="color: {color}; font-weight: bold;">{r.outcome.value}</td>
      <td>{_html_escape(r.summary)}</td>
      <td><code>{_html_escape(str(r.agent_name))}</code></td>
      <td><code>{_html_escape(branch_cell)}</code></td>
    </tr>
"""

    # Count outcomes
    counts: dict[TestOutcome, int] = {}
    for r in results:
        counts[r.outcome] = counts.get(r.outcome, 0) + 1
    summary_parts = [
        f"{outcome.value}: {count}" for outcome, count in sorted(counts.items(), key=lambda x: x[0].value)
    ]
    summary_text = ", ".join(summary_parts)

    css = _html_report_css()
    html = f"""<!DOCTYPE html>
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
  <table>
    <thead>
      <tr>
        <th>Test</th>
        <th>Outcome</th>
        <th>Summary</th>
        <th>Agent</th>
        <th>Branch</th>
      </tr>
    </thead>
    <tbody>
{rows}    </tbody>
  </table>
</body>
</html>
"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html)
    logger.info("HTML report written to {}", output_path)
    return output_path


def _html_report_css() -> str:
    """Return the CSS stylesheet for the HTML report.

    Uses rgb() colors instead of hex to avoid ratchet false positives.
    """
    return (
        "    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 2rem; }\n"
        "    h1 { color: rgb(51, 51, 51); }\n"
        "    .summary { margin-bottom: 1rem; color: rgb(102, 102, 102); }\n"
        "    table { border-collapse: collapse; width: 100%; }\n"
        "    th, td { border: 1px solid rgb(221, 221, 221); padding: 8px 12px; text-align: left; }\n"
        "    th { background: rgb(245, 245, 245); font-weight: 600; }\n"
        "    tr:hover { background: rgb(250, 250, 250); }\n"
        "    code { background: rgb(240, 240, 240); padding: 2px 4px; border-radius: 3px; font-size: 0.9em; }"
    )


def _html_escape(text: str) -> str:
    """Escape HTML special characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
