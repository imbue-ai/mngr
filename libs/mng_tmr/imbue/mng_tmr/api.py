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
from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.executor import ConcurrencyGroupExecutor
from imbue.imbue_common.model_update import to_update
from imbue.mng.api.create import create as api_create
from imbue.mng.api.data_types import CreateAgentResult
from imbue.mng.api.list import list_agents
from imbue.mng.api.providers import get_provider_instance
from imbue.mng.api.pull import pull_git
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import AgentNotFoundOnHostError
from imbue.mng.errors import HostError
from imbue.mng.errors import MngError
from imbue.mng.errors import SendMessageError
from imbue.mng.hosts.host import HostLocation
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.data_types import AgentDetails
from imbue.mng.interfaces.host import AgentDataOptions
from imbue.mng.interfaces.host import AgentGitOptions
from imbue.mng.interfaces.host import CreateAgentOptions
from imbue.mng.interfaces.host import NewHostBuildOptions
from imbue.mng.interfaces.host import NewHostOptions
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import ErrorBehavior
from imbue.mng.primitives import HostName
from imbue.mng.primitives import LOCAL_PROVIDER_NAME
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.primitives import SnapshotName
from imbue.mng.primitives import UncommittedChangesMode
from imbue.mng.primitives import WorkDirCopyMode
from imbue.mng_tmr.data_types import TestAgentInfo
from imbue.mng_tmr.data_types import TestMapReduceResult
from imbue.mng_tmr.data_types import TestOutcome
from imbue.mng_tmr.data_types import TestResult
from imbue.mng_tmr.data_types import TmrLaunchConfig

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
    TestOutcome.IMPROVED_TEST: "rgb(0, 200, 83)",
    TestOutcome.FIX_TEST_SUCCEEDED: "rgb(33, 150, 243)",
    TestOutcome.FIX_IMPL_SUCCEEDED: "rgb(33, 150, 243)",
    TestOutcome.FIX_TEST_FAILED: "rgb(244, 67, 54)",
    TestOutcome.FIX_IMPL_FAILED: "rgb(244, 67, 54)",
    TestOutcome.FIX_UNCERTAIN: "rgb(255, 152, 0)",
    TestOutcome.TIMED_OUT: "rgb(121, 85, 72)",
    TestOutcome.RUN_SUCCEEDED: "rgb(76, 175, 80)",
    TestOutcome.AGENT_ERROR: "rgb(158, 158, 158)",
    TestOutcome.REMOTE_AGENT_ERROR: "rgb(189, 147, 249)",
}

_OUTCOME_GROUP_ORDER: list[TestOutcome] = [
    TestOutcome.PENDING,
    TestOutcome.IMPROVED_TEST,
    TestOutcome.FIX_IMPL_SUCCEEDED,
    TestOutcome.FIX_TEST_SUCCEEDED,
    TestOutcome.FIX_IMPL_FAILED,
    TestOutcome.FIX_TEST_FAILED,
    TestOutcome.FIX_UNCERTAIN,
    TestOutcome.TIMED_OUT,
    TestOutcome.AGENT_ERROR,
    TestOutcome.REMOTE_AGENT_ERROR,
    TestOutcome.RUN_SUCCEEDED,
]

_SHORT_ID_LENGTH = 6

_MISSING_AGENT_MAX_ROUNDS = 30

_md = MarkdownIt()


class CollectTestsError(MngError, RuntimeError):
    """Raised when pytest test collection fails."""

    ...


def get_base_commit(source_dir: Path, cg: ConcurrencyGroup) -> str:
    """Get the current HEAD commit hash, used as the base for all agent branches."""
    result = cg.run_process_to_completion(["git", "rev-parse", "HEAD"], cwd=source_dir)
    return result.stdout.strip()


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


def _build_agent_prompt(
    test_node_id: str,
    pytest_flags: tuple[str, ...],
    prompt_suffix: str = "",
) -> str:
    """Build the prompt/initial message for a test-running agent."""
    flags_str = " ".join(pytest_flags)
    run_cmd = f"pytest {test_node_id}"
    if flags_str:
        run_cmd += f" {flags_str}"

    prompt = f"""Run the test with: {run_cmd}

If the test succeeds, think about how the test can be improved:

- We want the command run in the tests to match the demo script as much as possible.
  Remember that this is an E2E test, so we want to be as realistic as possible.
  We can afford to run real Claude Code sessions and even real Modal instances.

- We want the assertions to be complete and reflect things that can be easily
  tested in a real-world situation - note that the check is usually not part of
  the demo script, so you need to come up with the check yourself. For example,
  if a command is supposed to have an effect on Git, use Git commands to check
  the outcome. If a command is supposed to create a file, check the actual file.

- We also want the test to go a little bit beyond what the demo script literally
  says. Sometimes a simple command will work a little bit differently depending
  on the exact circumstances. In those cases, err on the side of having more tests
  rather than fewer. You can attach the same demo block to multiple tests.

The outcome in this case is IMPROVED_TEST. If no improvements are needed, the
outcome is RUN_SUCCEEDED.

If the test fails:

Read the output carefully for any debugging instructions. There should be
instructions for how to keep the test environment alive to debug it. While the
test environment is alive, debug agents "interactively" by using commands like
`mng capture` and `mng message` to get a good idea of what's happening inside
agents.

Then:

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

Valid outcome values: RUN_SUCCEEDED, IMPROVED_TEST, FIX_TEST_SUCCEEDED,
FIX_TEST_FAILED, FIX_IMPL_SUCCEEDED, FIX_IMPL_FAILED, FIX_UNCERTAIN.
"""
    if prompt_suffix:
        prompt += f"\n{prompt_suffix}\n"
    return prompt


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


def _copy_mode_for_provider(provider_name: ProviderInstanceName) -> WorkDirCopyMode:
    """Determine the git copy mode based on the provider.

    WORKTREE only works when source and target are on the same host, so it is
    only usable with the local provider. Remote providers (docker, modal, etc.)
    use CLONE to transfer git history efficiently.
    """
    is_local = provider_name.lower() == LOCAL_PROVIDER_NAME
    return WorkDirCopyMode.WORKTREE if is_local else WorkDirCopyMode.CLONE


def _build_agent_options(
    agent_name: AgentName,
    branch_name: str,
    config: TmrLaunchConfig,
) -> CreateAgentOptions:
    """Build CreateAgentOptions for a tmr agent."""
    copy_mode = _copy_mode_for_provider(config.provider_name)
    is_remote = config.provider_name.lower() != LOCAL_PROVIDER_NAME
    return CreateAgentOptions(
        agent_type=config.agent_type,
        name=agent_name,
        git=AgentGitOptions(
            copy_mode=copy_mode,
            new_branch_name=branch_name,
        ),
        data_options=AgentDataOptions(is_rsync_enabled=False),
        environment=config.env_options,
        label_options=config.label_options,
        ready_timeout_seconds=60.0 if is_remote else 10.0,
    )


def _create_tmr_agent(
    agent_name: AgentName,
    branch_name: str,
    config: TmrLaunchConfig,
    mng_ctx: MngContext,
    message: str | None = None,
) -> CreateAgentResult:
    """Create an agent on the configured provider and optionally send a message.

    If message is provided, it is sent after agent creation. SendMessageError
    is caught and logged (the signal detection can fail on remote hosts even
    when the message is actually delivered).
    """
    agent_options = _build_agent_options(agent_name, branch_name, config)

    source_location = HostLocation(host=config.source_host, path=config.source_dir)
    snapshot = config.snapshot
    build = NewHostBuildOptions(snapshot=snapshot) if snapshot is not None else NewHostBuildOptions()
    is_local = config.provider_name.lower() == LOCAL_PROVIDER_NAME
    host_name = None if is_local else HostName(str(agent_name))
    target_host = NewHostOptions(provider=config.provider_name, name=host_name, build=build)

    result = api_create(
        source_location=source_location,
        target_host=target_host,
        agent_options=agent_options,
        mng_ctx=mng_ctx,
    )

    if message is not None:
        try:
            result.agent.send_message(message)
        except (SendMessageError, HostError, TimeoutError) as exc:
            logger.warning(
                "Failed to confirm message delivery to '{}' (message likely delivered): {}", agent_name, exc
            )

    return result


def launch_test_agent(
    test_node_id: str,
    config: TmrLaunchConfig,
    mng_ctx: MngContext,
    pytest_flags: tuple[str, ...],
    prompt_suffix: str = "",
) -> tuple[TestAgentInfo, OnlineHostInterface]:
    """Launch a single agent to run and optionally fix one test."""
    agent_name_suffix = _sanitize_test_name_for_agent(test_node_id)
    short_id = _short_random_id()
    agent_name = AgentName(f"tmr-{agent_name_suffix}-{short_id}")

    logger.info("Launching agent '{}' for test: {}", agent_name, test_node_id)
    create_result = _create_tmr_agent(
        agent_name=agent_name,
        branch_name=f"mng-tmr/{agent_name_suffix}-{short_id}",
        config=config,
        mng_ctx=mng_ctx,
        message=_build_agent_prompt(test_node_id, pytest_flags, prompt_suffix),
    )

    return (
        TestAgentInfo(
            test_node_id=test_node_id,
            agent_id=create_result.agent.id,
            agent_name=create_result.agent.name,
        ),
        create_result.host,
    )


def _create_snapshot_host(
    config: TmrLaunchConfig,
    mng_ctx: MngContext,
) -> SnapshotName:
    """Launch a dedicated snapshotter agent, snapshot its host, then stop it.

    Creates a headless agent (no initial message) purely to trigger host
    provisioning, snapshots the resulting host, and immediately stops the
    agent. All real test agents are then launched from the snapshot.
    """
    short_id = _short_random_id()
    agent_name = AgentName(f"tmr-snapshotter-{short_id}")

    logger.info("Launching snapshotter agent '{}' for provisioning...", agent_name)
    create_result = _create_tmr_agent(
        agent_name=agent_name,
        branch_name=f"mng-tmr/snapshotter-{short_id}",
        config=config,
        mng_ctx=mng_ctx,
    )

    snapshotter_host = create_result.host
    snapshotter_agent_id = create_result.agent.id

    try:
        provider = get_provider_instance(config.provider_name, mng_ctx)
        snapshot_id = provider.create_snapshot(snapshotter_host)
        snapshot_name = SnapshotName(str(snapshot_id))
        logger.info("Created snapshot '{}' from snapshotter host", snapshot_name)
        return snapshot_name
    finally:
        _stop_agent_on_host(snapshotter_host, snapshotter_agent_id, agent_name)


def launch_all_test_agents(
    test_node_ids: list[str],
    config: TmrLaunchConfig,
    mng_ctx: MngContext,
    pytest_flags: tuple[str, ...],
    prompt_suffix: str = "",
    use_snapshot: bool = False,
    max_parallel: int = 4,
    launch_delay_seconds: float = 2.0,
) -> tuple[list[TestAgentInfo], dict[str, OnlineHostInterface], SnapshotName | None]:
    """Launch agents for all collected tests.

    Returns (agent_infos, agent_hosts, snapshot_name) where agent_hosts maps
    agent_id strings to the host each agent was created on, and snapshot_name
    is the snapshot used (if use_snapshot was True and the provider supports it).

    When use_snapshot is True, a dedicated snapshotter agent is launched first
    (without an initial message) purely to trigger provisioning. Its host is
    snapshotted and the snapshotter is stopped. All test agents are then
    launched from the snapshot for faster startup.
    """
    agents: list[TestAgentInfo] = []
    agent_hosts: dict[str, OnlineHostInterface] = {}

    # Optionally create a snapshot before launching any test agents
    launch_config = config
    if use_snapshot:
        provider = get_provider_instance(config.provider_name, mng_ctx)
        if provider.supports_snapshots:
            snapshot_name = _create_snapshot_host(config, mng_ctx)
            launch_config = config.model_copy_update(to_update(config.field_ref().snapshot, snapshot_name))
        else:
            logger.warning(
                "Provider '{}' does not support snapshots, launching all agents without snapshot",
                config.provider_name,
            )

    # Launch all test agents with staggered submissions to avoid rate limits
    with ConcurrencyGroupExecutor(
        parent_cg=mng_ctx.concurrency_group,
        name="tmr_launch",
        max_workers=max_parallel,
    ) as executor:
        futures = []
        for i, test_node_id in enumerate(test_node_ids):
            if i > 0 and launch_delay_seconds > 0:
                time.sleep(launch_delay_seconds)
            futures.append(
                executor.submit(
                    launch_test_agent,
                    test_node_id,
                    launch_config,
                    mng_ctx,
                    pytest_flags,
                    prompt_suffix,
                )
            )
        for future in futures:
            info, host = future.result()
            agents.append(info)
            agent_hosts[str(info.agent_id)] = host

    logger.info("Launched {} agent(s)", len(agents))
    return agents, agent_hosts, launch_config.snapshot


def poll_until_all_done(
    agents: list[TestAgentInfo],
    mng_ctx: MngContext,
    poll_interval_seconds: float,
    hosts: dict[str, OnlineHostInterface],
    deadline: float,
    report_path: Path | None = None,
) -> tuple[dict[str, AgentDetails], set[str]]:
    """Poll agents until all have reached a terminal state or the deadline is reached.

    Returns (final_details, timed_out_ids) where final_details maps agent_id
    strings to AgentDetails, and timed_out_ids is the set of agent_id strings
    that were still running when the deadline was reached.

    The hosts dict maps agent_id strings to their respective hosts. This is
    needed because agents may be running on different hosts (e.g. separate
    Docker containers or Modal instances).

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
                _stop_agent_on_host(hosts[agent_id_str], AgentId(agent_id_str), info.agent_name)
            if report_path is not None:
                current_results = build_current_results(agents, final_details, set(pending_ids), hosts)
                generate_html_report(current_results, report_path)
            return final_details, set(pending_ids)

        pending_names = [agent_id_to_info[aid].agent_name for aid in pending_ids]
        logger.info("Polling {} pending agent(s): {}", len(pending_ids), ", ".join(str(n) for n in pending_names))
        try:
            list_result = list_agents(
                mng_ctx=mng_ctx,
                is_streaming=False,
                error_behavior=ErrorBehavior.CONTINUE,
            )
        except Exception as exc:
            logger.warning("Polling failed (will retry next cycle): {}", exc)
            time.sleep(poll_interval_seconds)
            continue

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
                _stop_agent_on_host(hosts[agent_id_str], agent_detail.id, agent_detail.name)

        for agent_id_str in list(pending_ids):
            if agent_id_str not in seen_ids:
                rounds = missing_rounds.get(agent_id_str, 0) + 1
                missing_rounds[agent_id_str] = rounds
                if rounds >= _MISSING_AGENT_MAX_ROUNDS:
                    logger.warning("Agent {} disappeared after {} rounds, treating as error", agent_id_str, rounds)
                    pending_ids.discard(agent_id_str)
                    changed = True

        if changed and report_path is not None:
            current_results = build_current_results(agents, final_details, set(), hosts)
            generate_html_report(current_results, report_path)

        if pending_ids:
            time.sleep(poll_interval_seconds)

    return final_details, set()


def _stop_agent_on_host(host: OnlineHostInterface, agent_id: AgentId, agent_name: AgentName) -> None:
    """Stop a single agent on the host."""
    try:
        host.stop_agents([agent_id])
        logger.info("Stopped agent '{}'", agent_name)
    except (MngError, HostError) as exc:
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
    except HostError as exc:
        logger.warning("Lost connection to agent {} while fetching result file: {}", agent_detail.name, exc)
        return TestResult(
            outcome=TestOutcome.REMOTE_AGENT_ERROR,
            summary=f"Connection lost while fetching result file from agent host: {exc}",
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
    base_commit: str | None = None,
) -> str | None:
    """Pull the agent's git branch into the local repo.

    If base_commit is provided, a new local branch is created from that commit
    before pulling. This is needed for remote agents where the branch doesn't
    exist locally yet.

    Returns the branch name if successful, None otherwise.
    """
    branch_name = agent_detail.initial_branch
    if branch_name is None:
        logger.warning("Agent '{}' has no branch to pull", agent_detail.name)
        return None

    try:
        if base_commit is not None:
            _create_local_branch(destination, branch_name, base_commit, cg)

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
    except HostError as exc:
        logger.warning("Connection lost while pulling branch from agent '{}': {}", agent_detail.name, exc)
        return None
    except (MngError, ProcessError) as exc:
        logger.warning("Failed to pull branch from agent '{}': {}", agent_detail.name, exc)
        return None


def _create_local_branch(destination: Path, branch_name: str, base_commit: str, cg: ConcurrencyGroup) -> None:
    """Create a local git branch from a base commit and switch to it.

    If the branch already exists (e.g. from a previous run), it is reused.
    """
    result = cg.run_process_to_completion(
        ["git", "branch", branch_name, base_commit],
        cwd=destination,
        is_checked_after=False,
    )
    if result.returncode == 0:
        logger.info("Created local branch '{}' from commit {}", branch_name, base_commit[:8])
    else:
        logger.info("Branch '{}' already exists, reusing it", branch_name)
    cg.run_process_to_completion(["git", "checkout", branch_name], cwd=destination)


def _get_agent_from_host(
    host: OnlineHostInterface,
    agent_id: AgentId,
) -> AgentInterface:
    """Look up an agent on a host by ID.

    Raises AgentNotFoundOnHostError if not found, or HostError if the host
    is unreachable (callers should catch both).
    """
    for agent in host.get_agents():
        if agent.id == agent_id:
            return agent
    raise AgentNotFoundOnHostError(agent_id, host.id)


def _collect_agent_results(
    agents: list[TestAgentInfo],
    final_details: dict[str, AgentDetails],
    timed_out_ids: set[str],
    hosts: dict[str, OnlineHostInterface],
    missing_detail_outcome: TestOutcome,
    missing_detail_summary: str,
) -> list[TestMapReduceResult]:
    """Shared iteration over agents to build result list.

    Each agent is classified as timed-out, missing (with the caller-specified
    outcome), or finished (result read from the agent's state directory).
    The hosts dict maps agent_id strings to their respective hosts.
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

        test_result = read_agent_result(detail, hosts[agent_id_str])
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
    hosts: dict[str, OnlineHostInterface],
    source_dir: Path,
    cg: ConcurrencyGroup,
    base_commit: str | None = None,
) -> list[TestMapReduceResult]:
    """Gather results from all finished agents, pulling branches where appropriate.

    If base_commit is provided, a local branch is created from that commit before
    pulling each agent's changes. This is needed for remote agents whose branches
    don't exist locally.
    """
    results = _collect_agent_results(
        agents=agents,
        final_details=final_details,
        timed_out_ids=timed_out_ids,
        hosts=hosts,
        missing_detail_outcome=TestOutcome.AGENT_ERROR,
        missing_detail_summary="Agent details not found after polling",
    )

    # Pull branches for successful fixes and improvements
    for result in results:
        if result.outcome in (
            TestOutcome.FIX_TEST_SUCCEEDED,
            TestOutcome.FIX_IMPL_SUCCEEDED,
            TestOutcome.IMPROVED_TEST,
        ):
            agent_id_str = next(str(info.agent_id) for info in agents if info.test_node_id == result.test_node_id)
            detail = final_details.get(agent_id_str)
            if detail is not None:
                pull_agent_branch(detail, hosts[agent_id_str], source_dir, cg, base_commit=base_commit)

    return results


def build_current_results(
    agents: list[TestAgentInfo],
    final_details: dict[str, AgentDetails],
    timed_out_ids: set[str],
    hosts: dict[str, OnlineHostInterface],
) -> list[TestMapReduceResult]:
    """Build current results without pulling branches, for intermediate reports."""
    return _collect_agent_results(
        agents=agents,
        final_details=final_details,
        timed_out_ids=timed_out_ids,
        hosts=hosts,
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
    config: TmrLaunchConfig,
    mng_ctx: MngContext,
) -> tuple[TestAgentInfo, OnlineHostInterface]:
    """Launch an integrator agent that merges all fix branches into one."""
    short_id = _short_random_id()
    agent_name = AgentName(f"tmr-integrator-{short_id}")

    branch_list = "\n".join(f"  - {b}" for b in fix_branches)
    prompt = f"""Merge the following branches into one integrated branch:
{branch_list}

For each branch, run `git merge <branch>` (resolve conflicts if needed).
After merging all branches, verify that the code still compiles/passes basic checks.
Write the result to $MNG_AGENT_STATE_DIR/plugin/{PLUGIN_NAME}/result.json with:
{{"outcome": "FIX_IMPL_SUCCEEDED", "summary": "Merged {len(fix_branches)} branches successfully."}}
If merging fails, use outcome FIX_IMPL_FAILED.
"""

    logger.info("Launching integrator agent '{}' to merge {} branches", agent_name, len(fix_branches))
    create_result = _create_tmr_agent(
        agent_name=agent_name,
        branch_name=f"mng-tmr/integrated-{short_id}",
        config=config,
        mng_ctx=mng_ctx,
        message=prompt,
    )

    return (
        TestAgentInfo(
            test_node_id="integrator",
            agent_id=create_result.agent.id,
            agent_name=create_result.agent.name,
        ),
        create_result.host,
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
        try:
            list_result = list_agents(
                mng_ctx=mng_ctx,
                is_streaming=False,
                error_behavior=ErrorBehavior.CONTINUE,
            )
        except Exception as exc:
            logger.warning("Integrator polling failed (will retry next cycle): {}", exc)
            time.sleep(poll_interval_seconds)
            continue

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
