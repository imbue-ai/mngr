"""Core logic for the test-mapreduce plugin.

Implements the map-reduce pattern: collect tests via pytest, launch an agent per
test, poll for completion, gather results, and pull code changes.
"""

import json
import secrets
import time
from pathlib import Path

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.executor import ConcurrencyGroupExecutor
from imbue.imbue_common.model_update import to_update
from imbue.mng.api.create import create as api_create
from imbue.mng.api.data_types import CreateAgentResult
from imbue.mng.api.list import ListResult
from imbue.mng.api.list import list_agents
from imbue.mng.api.providers import get_provider_instance
from imbue.mng.api.pull import pull_git
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import AgentNotFoundOnHostError
from imbue.mng.errors import HostError
from imbue.mng.errors import MngError
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
from imbue.mng_tmr.data_types import Change
from imbue.mng_tmr.data_types import ChangeKind
from imbue.mng_tmr.data_types import ChangeStatus
from imbue.mng_tmr.data_types import IntegratorResult
from imbue.mng_tmr.data_types import TestAgentInfo
from imbue.mng_tmr.data_types import TestMapReduceResult
from imbue.mng_tmr.data_types import TestResult
from imbue.mng_tmr.data_types import TmrLaunchConfig
from imbue.mng_tmr.report import generate_html_report

PLUGIN_NAME = "test-map-reduce"

_TERMINAL_STATES = frozenset(
    {
        AgentLifecycleState.DONE,
        AgentLifecycleState.STOPPED,
        AgentLifecycleState.WAITING,
    }
)

_SHORT_ID_LENGTH = 6

_MISSING_AGENT_MAX_ROUNDS = 30


def _try_list_agents(mng_ctx: MngContext) -> ListResult | None:
    """List agents, returning None on transient errors.

    Human-sanctioned broad catch: polling must survive transient provider errors.
    """
    try:
        return list_agents(
            mng_ctx=mng_ctx,
            is_streaming=False,
            error_behavior=ErrorBehavior.CONTINUE,
        )
    except Exception as exc:
        logger.warning("Polling failed (will retry next cycle): {}", exc)
        return None


def should_pull_changes(result: TestMapReduceResult) -> bool:
    """Determine whether an agent's changes should be pulled.

    Pull when: not errored, at least one succeeded change, and tests are at
    least as good as before (if they were passing, they must still be passing).
    """
    if result.errored:
        return False
    if not any(c.status == ChangeStatus.SUCCEEDED for c in result.changes.values()):
        return False
    if result.tests_passing_before is True and result.tests_passing_after is not True:
        return False
    return True


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
    """Build the prompt/initial message for a test-running agent.

    Human-sanctioned: prompt is currently specific to mng's E2E tutorial tests.
    This should be made generic in the future, but is acceptable for now.
    """
    flags_str = " ".join(pytest_flags)
    run_cmd = f"pytest {test_node_id}"
    if flags_str:
        run_cmd += f" {flags_str}"

    prompt = f"""Run the test with: {run_cmd}

# If the test fails

You can record multiple kinds of changes -- they are not mutually exclusive (one
entry per kind, not per individual edit):

- "FIX_TEST": fix the test code (including fixtures).
- "FIX_IMPL": fix the program being tested.

Each change has a status: "SUCCEEDED" if the fix worked, "FAILED" if you tried
but could not complete it, or "BLOCKED" if the issue needs larger intervention
beyond this task. If you cannot determine what is wrong, report no changes.

# If the test succeeds - or after you fixed a failing test

Consider whether the test can be improved:

- Are the assertions good enough? Try to test by observing the actual effect of
  commands, like how a human would do when debugging interactively, by looking at
  e.g. files, git status, and so on. Avoid having too many specific assertions,
  because this can make the tests very brittle.

- Are there interesting edge cases worth covering?

- Is the code run in the pytest function close enough to the tutorial block?

If you make improvements, record a change under the key "IMPROVE_TEST". If you
identify an improvement that needs a larger-scale intervention, use status
"BLOCKED". If no improvements are needed, leave the changes object empty.

# Inspecting tutorial blocks

Each of those tests are also associated with a tutorial block in
libs/mng/tutorials/mega_tutorial.sh; we divide the file into blocks by splitting
around empty lines. You'll find a reproduction of a tutorial block using the API
e2e.write_tutorial_block. When modifying the test, you should normally keep the
tutorial block unchanged: they should match exactly with the block in the tutorial
file (modulo leading whitespaces).

However, try to think if tutorial itself could be wrong or outdated. This should be
a rare case - often the tutorial block is a bit too concise to be run as-is, and
that may be intentional.

If you do think that the tutorial block is wrong or outdated, update both the
tutorial block in the mega_tutorial.sh file and the test code itself, and record
a change under the key "FIX_TUTORIAL".

# Writing the result

In all cases, write the result to a JSON file at
$MNG_AGENT_STATE_DIR/plugin/{PLUGIN_NAME}/result.json with this schema:

{{"changes": {{"FIX_TEST": {{"status": "SUCCEEDED", "summary_markdown": "Fixed assertion"}}}},
 "errored": false,
 "tests_passing_before": false,
 "tests_passing_after": true,
 "summary_markdown": "Fixed test assertion and verified it passes."}}

Fields:
- changes: object keyed by change kind (IMPROVE_TEST, FIX_TEST, FIX_IMPL,
  FIX_TUTORIAL). Each value has status (SUCCEEDED, FAILED, BLOCKED) and
  summary_markdown. One entry per kind -- do not duplicate kinds.
- errored: true only for infrastructure errors that prevented you from working.
- tests_passing_before: were tests passing before you made any changes?
- tests_passing_after: are tests passing now, after all your changes?
- summary_markdown: overall markdown summary of what happened.
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
    initial_message: str | None = None,
) -> CreateAgentOptions:
    """Build CreateAgentOptions for a tmr agent."""
    copy_mode = _copy_mode_for_provider(config.provider_name)
    is_remote = config.provider_name.lower() != LOCAL_PROVIDER_NAME
    return CreateAgentOptions(
        agent_type=config.agent_type,
        name=agent_name,
        initial_message=initial_message,
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
    initial_message: str | None = None,
) -> CreateAgentResult:
    """Create an agent on the configured provider with an optional initial message.

    The initial_message is passed via CreateAgentOptions so it is delivered
    during agent creation (more reliable than sending after creation).
    """
    agent_options = _build_agent_options(agent_name, branch_name, config, initial_message=initial_message)

    source_location = HostLocation(host=config.source_host, path=config.source_dir)
    snapshot = config.snapshot
    build = NewHostBuildOptions(snapshot=snapshot) if snapshot is not None else NewHostBuildOptions()
    is_local = config.provider_name.lower() == LOCAL_PROVIDER_NAME
    host_name = None if is_local else HostName(str(agent_name))
    target_host = NewHostOptions(provider=config.provider_name, name=host_name, build=build)

    return api_create(
        source_location=source_location,
        target_host=target_host,
        agent_options=agent_options,
        mng_ctx=mng_ctx,
    )


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
        initial_message=_build_agent_prompt(test_node_id, pytest_flags, prompt_suffix),
    )

    return (
        TestAgentInfo(
            test_node_id=test_node_id,
            agent_id=create_result.agent.id,
            agent_name=create_result.agent.name,
            created_at=time.monotonic(),
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


def _launch_agents_up_to_limit(
    remaining_tests: list[str],
    pending_ids: set[str],
    max_agents: int,
    config: TmrLaunchConfig,
    mng_ctx: MngContext,
    pytest_flags: tuple[str, ...],
    prompt_suffix: str,
    all_agents: list[TestAgentInfo],
    all_hosts: dict[str, OnlineHostInterface],
    agent_id_to_info: dict[str, TestAgentInfo],
) -> None:
    """Launch agents from remaining_tests until we hit max_agents running.

    Mutates remaining_tests (pops from front), pending_ids, all_agents,
    all_hosts, and agent_id_to_info in place.
    """
    while remaining_tests and len(pending_ids) < max_agents:
        test_node_id = remaining_tests.pop(0)
        info, host = launch_test_agent(test_node_id, config, mng_ctx, pytest_flags, prompt_suffix)
        all_agents.append(info)
        all_hosts[str(info.agent_id)] = host
        agent_id_to_info[str(info.agent_id)] = info
        pending_ids.add(str(info.agent_id))


def launch_and_poll_agents(
    test_node_ids: list[str],
    config: TmrLaunchConfig,
    mng_ctx: MngContext,
    pytest_flags: tuple[str, ...],
    prompt_suffix: str,
    max_agents: int,
    agent_timeout_seconds: float,
    poll_interval_seconds: float,
    report_path: Path | None,
    all_agents: list[TestAgentInfo],
    all_hosts: dict[str, OnlineHostInterface],
) -> tuple[dict[str, AgentDetails], set[str]]:
    """Launch agents incrementally and poll until all finish.

    Handles two modes depending on arguments:

    1. Incremental launching (max_agents > 0, test_node_ids non-empty): launches
       up to max_agents at a time, polling and launching more as capacity opens.
    2. Pre-launched polling (test_node_ids empty, all_agents pre-populated):
       polls the already-launched agents without launching any new ones.

    all_agents and all_hosts are input/output parameters: pre-existing entries
    are tracked from the start, and newly launched agents are appended during
    execution.

    Returns (final_details, timed_out_ids) where final_details maps agent_id
    strings to AgentDetails, and timed_out_ids is the set of agent_id strings
    that were stopped because they exceeded agent_timeout_seconds.
    """
    remaining_tests = list(test_node_ids)
    pending_ids: set[str] = set()
    agent_id_to_info: dict[str, TestAgentInfo] = {}
    final_details: dict[str, AgentDetails] = {}
    timed_out_ids: set[str] = set()
    missing_rounds: dict[str, int] = {}

    # Initialize tracking from any pre-launched agents already in all_agents
    for info in all_agents:
        agent_id_str = str(info.agent_id)
        agent_id_to_info[agent_id_str] = info
        pending_ids.add(agent_id_str)

    launch_args = (
        remaining_tests,
        pending_ids,
        max_agents,
        config,
        mng_ctx,
        pytest_flags,
        prompt_suffix,
        all_agents,
        all_hosts,
        agent_id_to_info,
    )

    # Launch initial batch (no-op when remaining_tests is empty)
    _launch_agents_up_to_limit(*launch_args)

    if report_path is not None:
        current_results = build_current_results(all_agents, final_details, timed_out_ids, all_hosts)
        generate_html_report(current_results, report_path)

    while pending_ids or remaining_tests:
        # Check per-agent timeouts
        now = time.monotonic()
        timed_out_this_round = False
        for agent_id_str in list(pending_ids):
            info = agent_id_to_info[agent_id_str]
            elapsed = now - info.created_at
            if elapsed >= agent_timeout_seconds:
                logger.warning("Agent '{}' timed out after {:.0f}s, stopping", info.agent_name, elapsed)
                _stop_agent_on_host(all_hosts[agent_id_str], AgentId(agent_id_str), info.agent_name)
                pending_ids.discard(agent_id_str)
                timed_out_ids.add(agent_id_str)
                timed_out_this_round = True

        # Launch new agents if capacity opened up
        _launch_agents_up_to_limit(*launch_args)

        if timed_out_this_round and report_path is not None:
            current_results = build_current_results(all_agents, final_details, timed_out_ids, all_hosts)
            generate_html_report(current_results, report_path)

        if not pending_ids and not remaining_tests:
            break

        if not pending_ids:
            continue

        pending_names = [agent_id_to_info[aid].agent_name for aid in pending_ids]
        logger.info("Polling {} pending agent(s): {}", len(pending_ids), ", ".join(str(n) for n in pending_names))
        list_result = _try_list_agents(mng_ctx)
        if list_result is None:
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

            logger.info("Agent '{}' finished (state={})", agent_detail.name, agent_detail.state)
            final_details[agent_id_str] = agent_detail
            pending_ids.discard(agent_id_str)
            missing_rounds.pop(agent_id_str, None)
            changed = True

            if agent_detail.state == AgentLifecycleState.WAITING:
                _stop_agent_on_host(all_hosts[agent_id_str], agent_detail.id, agent_detail.name)

        for agent_id_str in list(pending_ids):
            if agent_id_str not in seen_ids:
                rounds = missing_rounds.get(agent_id_str, 0) + 1
                missing_rounds[agent_id_str] = rounds
                if rounds >= _MISSING_AGENT_MAX_ROUNDS:
                    logger.warning("Agent {} disappeared after {} rounds, treating as error", agent_id_str, rounds)
                    pending_ids.discard(agent_id_str)
                    changed = True

        # Launch new agents if capacity opened up from finished agents
        _launch_agents_up_to_limit(*launch_args)

        if (changed or timed_out_this_round) and report_path is not None:
            current_results = build_current_results(all_agents, final_details, timed_out_ids, all_hosts)
            generate_html_report(current_results, report_path)

        if pending_ids or remaining_tests:
            time.sleep(poll_interval_seconds)

    return final_details, timed_out_ids


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
        raw_changes = data.get("changes", {})
        changes: dict[ChangeKind, Change] = {
            ChangeKind(kind_str): Change(
                status=ChangeStatus(entry["status"]),
                summary_markdown=entry.get("summary_markdown", entry.get("summary", "")),
            )
            for kind_str, entry in raw_changes.items()
        }
        return TestResult(
            changes=changes,
            errored=data.get("errored", False),
            tests_passing_before=data.get("tests_passing_before"),
            tests_passing_after=data.get("tests_passing_after"),
            summary_markdown=data.get("summary_markdown", ""),
        )
    except HostError as exc:
        logger.warning("Lost connection to agent {} while fetching result file: {}", agent_detail.name, exc)
        return TestResult(
            errored=True,
            summary_markdown=f"Connection lost while fetching result file from agent host: {exc}",
        )
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.warning("Failed to read result from agent {}: {}", agent_detail.name, exc)
        return TestResult(
            errored=True,
            summary_markdown=f"Failed to read agent result: {exc}",
        )


def pull_test_outputs(
    agent_detail: AgentDetails,
    host: OnlineHostInterface,
    local_host: OnlineHostInterface,
    destination_dir: Path,
) -> None:
    """Pull the .test_output directory from an agent's work_dir to a local directory."""
    remote_test_output = agent_detail.work_dir / ".test_output"
    local_dest = destination_dir / str(agent_detail.name)
    local_dest.mkdir(parents=True, exist_ok=True)
    try:
        local_host.copy_directory(
            source_host=host,
            source_path=remote_test_output,
            target_path=local_dest,
        )
        logger.info("Pulled .test_output from agent '{}' to {}", agent_detail.name, local_dest)
    except (MngError, HostError, OSError) as exc:
        logger.warning("Failed to pull .test_output from agent '{}': {}", agent_detail.name, exc)


def read_integrator_result(
    agent_detail: AgentDetails,
    host: OnlineHostInterface,
    branch_name: str | None,
) -> IntegratorResult:
    """Read the integrator agent's result.json and build an IntegratorResult."""
    agent_state_dir = host.host_dir / "agents" / str(agent_detail.id)
    result_path = agent_state_dir / "plugin" / PLUGIN_NAME / "result.json"

    try:
        raw = host.read_text_file(result_path)
        data = json.loads(raw)
        return IntegratorResult(
            merged=tuple(data.get("merged", ())),
            failed=tuple(data.get("failed", ())),
            branch_name=branch_name,
            summary_markdown=data.get("summary_markdown", data.get("summary", "")),
        )
    except (HostError, OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.warning("Failed to read integrator result: {}", exc)
        return IntegratorResult(
            branch_name=branch_name,
            summary_markdown=f"Failed to read integrator result: {exc}",
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
    missing_detail_errored: bool,
    missing_detail_summary: str,
) -> list[TestMapReduceResult]:
    """Shared iteration over agents to build result list.

    Each agent is classified as timed-out, missing (with the caller-specified
    errored flag and summary), or finished (result read from the agent's state
    directory). The hosts dict maps agent_id strings to their respective hosts.
    """
    results: list[TestMapReduceResult] = []

    for agent_info in agents:
        agent_id_str = str(agent_info.agent_id)

        if agent_id_str in timed_out_ids:
            results.append(
                TestMapReduceResult(
                    test_node_id=agent_info.test_node_id,
                    agent_name=agent_info.agent_name,
                    errored=True,
                    summary_markdown="Agent was stopped because the timeout was reached.",
                )
            )
            continue

        detail = final_details.get(agent_id_str)

        if detail is None:
            results.append(
                TestMapReduceResult(
                    test_node_id=agent_info.test_node_id,
                    agent_name=agent_info.agent_name,
                    errored=missing_detail_errored,
                    summary_markdown=missing_detail_summary,
                )
            )
            continue

        test_result = read_agent_result(detail, hosts[agent_id_str])
        results.append(
            TestMapReduceResult(
                test_node_id=agent_info.test_node_id,
                agent_name=agent_info.agent_name,
                changes=test_result.changes,
                errored=test_result.errored,
                tests_passing_before=test_result.tests_passing_before,
                tests_passing_after=test_result.tests_passing_after,
                summary_markdown=test_result.summary_markdown,
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

    If base_commit is provided (remote providers), a local branch is created from
    that commit before pulling each agent's changes. For local providers,
    base_commit should be None and branches are not pulled (they already exist
    as git worktrees).
    """
    results = _collect_agent_results(
        agents=agents,
        final_details=final_details,
        timed_out_ids=timed_out_ids,
        hosts=hosts,
        missing_detail_errored=True,
        missing_detail_summary="Agent details not found after polling",
    )

    # Pull branches from remote agents whose changes should be kept.
    # For local agents (base_commit is None), branches already exist as worktrees.
    if base_commit is not None:
        for result in results:
            if should_pull_changes(result):
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
        missing_detail_errored=False,
        missing_detail_summary="Agent is still running...",
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
{{"merged": ["branch1", "branch2"], "failed": ["branch3"], "summary_markdown": "Merged 2 of 3 branches."}}

- merged: list of branch names that were successfully merged
- failed: list of branch names that could not be merged
- summary_markdown: overall markdown summary
"""

    logger.info("Launching integrator agent '{}' to merge {} branches", agent_name, len(fix_branches))
    create_result = _create_tmr_agent(
        agent_name=agent_name,
        branch_name=f"mng-tmr/integrated-{short_id}",
        config=config,
        mng_ctx=mng_ctx,
        initial_message=prompt,
    )

    return (
        TestAgentInfo(
            test_node_id="integrator",
            agent_id=create_result.agent.id,
            agent_name=create_result.agent.name,
            created_at=time.monotonic(),
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
        list_result = _try_list_agents(mng_ctx)
        if list_result is None:
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
