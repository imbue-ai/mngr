"""Core logic for the test-mapreduce plugin.

Implements the map-reduce pattern: collect tests via pytest, launch an agent per
test, poll for completion, gather results, and pull code changes.
"""

import json
import secrets
import threading
import time
from pathlib import Path

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.executor import ConcurrencyGroupExecutor
from imbue.imbue_common.model_update import to_update
from imbue.mngr.api.create import create as api_create
from imbue.mngr.api.data_types import CreateAgentResult
from imbue.mngr.api.list import ListResult
from imbue.mngr.api.list import list_agents
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.api.pull import pull_files
from imbue.mngr.api.pull import pull_git
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import AgentNotFoundOnHostError
from imbue.mngr.errors import HostError
from imbue.mngr.errors import MngrError
from imbue.mngr.hosts.host import HostLocation
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.host import AgentDataOptions
from imbue.mngr.interfaces.host import AgentGitOptions
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import NewHostBuildOptions
from imbue.mngr.interfaces.host import NewHostOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SnapshotName
from imbue.mngr.primitives import TransferMode
from imbue.mngr.primitives import UncommittedChangesMode
from imbue.mngr_tmr.data_types import Change
from imbue.mngr_tmr.data_types import ChangeKind
from imbue.mngr_tmr.data_types import ChangeStatus
from imbue.mngr_tmr.data_types import IntegratorResult
from imbue.mngr_tmr.data_types import TestAgentInfo
from imbue.mngr_tmr.data_types import TestMapReduceResult
from imbue.mngr_tmr.data_types import TestResult
from imbue.mngr_tmr.data_types import TestRunInfo
from imbue.mngr_tmr.data_types import TmrLaunchConfig
from imbue.mngr_tmr.prompts import INTEGRATOR_OUTCOME_FILENAME
from imbue.mngr_tmr.prompts import TESTING_AGENT_OUTCOME_FILENAME
from imbue.mngr_tmr.prompts import build_integrator_prompt
from imbue.mngr_tmr.prompts import build_test_agent_prompt
from imbue.mngr_tmr.report import generate_html_report

_TERMINAL_STATES = frozenset(
    {
        AgentLifecycleState.DONE,
        AgentLifecycleState.STOPPED,
        AgentLifecycleState.WAITING,
    }
)

_SHORT_ID_LENGTH = 6

_MISSING_AGENT_MAX_ROUNDS = 30


_LIST_AGENTS_TIMEOUT_SECONDS = 60.0


def _list_agents_thread_target(
    mngr_ctx: MngrContext,
    result_holder: list[ListResult | None],
    error_holder: list[Exception | None],
) -> None:
    """Thread target for try_list_agents. Catches all exceptions."""
    try:
        result_holder[0] = list_agents(
            mngr_ctx=mngr_ctx,
            is_streaming=False,
            error_behavior=ErrorBehavior.CONTINUE,
        )
    # Human-sanctioned broad catch: thread must not propagate exceptions
    except Exception as exc:
        error_holder[0] = exc


def try_list_agents(mngr_ctx: MngrContext) -> ListResult | None:
    """List agents, returning None on transient errors or timeout.

    Runs list_agents in a daemon thread with a 60s timeout to work around
    Modal API hangs where discover_hosts_and_agents never returns.

    Human-sanctioned broad catch: polling must survive transient provider errors.
    """
    result_holder: list[ListResult | None] = [None]
    error_holder: list[Exception | None] = [None]

    thread = threading.Thread(
        target=_list_agents_thread_target,
        args=(mngr_ctx, result_holder, error_holder),
        daemon=True,
    )
    thread.start()
    thread.join(timeout=_LIST_AGENTS_TIMEOUT_SECONDS)

    if thread.is_alive():
        logger.warning("list_agents timed out after {:.0f}s (will retry next cycle)", _LIST_AGENTS_TIMEOUT_SECONDS)
        return None

    if error_holder[0] is not None:
        logger.warning("Polling failed (will retry next cycle): {}", error_holder[0])
        return None

    return result_holder[0]


def _should_pull(
    errored: bool,
    changes: dict[ChangeKind, Change],
    tests_passing_before: bool | None,
    tests_passing_after: bool | None,
) -> bool:
    """Core logic: should we pull an agent's changes?

    Pull when: not errored, at least one succeeded change, and tests are at
    least as good as before (if they were passing, they must still be passing).
    """
    if errored:
        return False
    if not any(c.status == ChangeStatus.SUCCEEDED for c in changes.values()):
        return False
    if tests_passing_before is True and tests_passing_after is not True:
        return False
    return True


def should_pull_changes(result: TestMapReduceResult) -> bool:
    """Determine whether an agent's changes should be pulled (from a TestMapReduceResult)."""
    return _should_pull(result.errored, result.changes, result.tests_passing_before, result.tests_passing_after)


def should_pull_changes_from_result(result: TestResult) -> bool:
    """Determine whether an agent's changes should be pulled (from a TestResult)."""
    return _should_pull(result.errored, result.changes, result.tests_passing_before, result.tests_passing_after)


class CollectTestsError(MngrError, RuntimeError):
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


def _transfer_mode_for_provider(provider_name: ProviderInstanceName) -> TransferMode:
    """Determine the transfer mode based on the provider.

    GIT_WORKTREE only works when source and target are on the same host, so it is
    only usable with the local provider. Remote providers (docker, modal, etc.)
    use GIT_MIRROR to transfer git history efficiently.
    """
    is_local = provider_name.lower() == LOCAL_PROVIDER_NAME
    return TransferMode.GIT_WORKTREE if is_local else TransferMode.GIT_MIRROR


def _build_agent_options(
    agent_name: AgentName,
    branch_name: str,
    config: TmrLaunchConfig,
    initial_message: str | None = None,
) -> CreateAgentOptions:
    """Build CreateAgentOptions for a tmr agent."""
    transfer_mode = _transfer_mode_for_provider(config.provider_name)
    is_remote = config.provider_name.lower() != LOCAL_PROVIDER_NAME
    return CreateAgentOptions(
        agent_type=config.agent_type,
        name=agent_name,
        initial_message=initial_message,
        transfer_mode=transfer_mode,
        git=AgentGitOptions(
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
    mngr_ctx: MngrContext,
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
        mngr_ctx=mngr_ctx,
    )


def launch_test_agent(
    test_node_id: str,
    config: TmrLaunchConfig,
    mngr_ctx: MngrContext,
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
        branch_name=f"mngr-tmr/{agent_name_suffix}-{short_id}",
        config=config,
        mngr_ctx=mngr_ctx,
        initial_message=build_test_agent_prompt(test_node_id, pytest_flags, prompt_suffix),
    )

    branch = f"mngr-tmr/{agent_name_suffix}-{short_id}"
    return (
        TestAgentInfo(
            test_node_id=test_node_id,
            agent_id=create_result.agent.id,
            agent_name=create_result.agent.name,
            work_dir=create_result.agent.work_dir,
            branch_name=branch,
            created_at=time.monotonic(),
        ),
        create_result.host,
    )


def _create_snapshot_host(
    config: TmrLaunchConfig,
    mngr_ctx: MngrContext,
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
        branch_name=f"mngr-tmr/snapshotter-{short_id}",
        config=config,
        mngr_ctx=mngr_ctx,
    )

    snapshotter_host = create_result.host
    snapshotter_agent_id = create_result.agent.id

    try:
        provider = get_provider_instance(config.provider_name, mngr_ctx)
        snapshot_id = provider.create_snapshot(snapshotter_host)
        snapshot_name = SnapshotName(str(snapshot_id))
        logger.info("Created snapshot '{}' from snapshotter host", snapshot_name)
        return snapshot_name
    finally:
        _stop_agent_on_host(snapshotter_host, snapshotter_agent_id, agent_name)


def launch_all_test_agents(
    test_node_ids: list[str],
    config: TmrLaunchConfig,
    mngr_ctx: MngrContext,
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
        provider = get_provider_instance(config.provider_name, mngr_ctx)
        if provider.supports_snapshots:
            try:
                snapshot_name = _create_snapshot_host(config, mngr_ctx)
                launch_config = config.model_copy_update(to_update(config.field_ref().snapshot, snapshot_name))
            except (MngrError, HostError, OSError, BaseExceptionGroup) as exc:
                logger.warning("Failed to create snapshot, launching agents without snapshot: {}", exc)
        else:
            logger.warning(
                "Provider '{}' does not support snapshots, launching all agents without snapshot",
                config.provider_name,
            )

    # Launch all test agents with staggered submissions to avoid rate limits
    with ConcurrencyGroupExecutor(
        parent_cg=mngr_ctx.concurrency_group,
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
                    mngr_ctx,
                    pytest_flags,
                    prompt_suffix,
                )
            )
        for future in futures:
            try:
                info, host = future.result()
                agents.append(info)
                agent_hosts[str(info.agent_id)] = host
            except (MngrError, HostError, OSError, BaseExceptionGroup) as exc:
                logger.warning("Failed to launch agent: {}", exc)

    logger.info("Launched {} agent(s)", len(agents))
    return agents, agent_hosts, launch_config.snapshot


def _launch_agents_up_to_limit(
    remaining_tests: list[str],
    pending_ids: set[str],
    max_agents: int,
    config: TmrLaunchConfig,
    mngr_ctx: MngrContext,
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
    while remaining_tests and (max_agents <= 0 or len(pending_ids) < max_agents):
        test_node_id = remaining_tests.pop(0)
        try:
            info, host = launch_test_agent(test_node_id, config, mngr_ctx, pytest_flags, prompt_suffix)
        except (MngrError, HostError, OSError, BaseExceptionGroup) as exc:
            logger.warning("Failed to launch agent for {}: {}", test_node_id, exc)
            continue
        all_agents.append(info)
        all_hosts[str(info.agent_id)] = host
        agent_id_to_info[str(info.agent_id)] = info
        pending_ids.add(str(info.agent_id))


def launch_and_poll_agents(
    test_node_ids: list[str],
    config: TmrLaunchConfig,
    mngr_ctx: MngrContext,
    pytest_flags: tuple[str, ...],
    prompt_suffix: str,
    max_agents: int,
    agent_timeout_seconds: float,
    poll_interval_seconds: float,
    result_check_interval_seconds: float,
    report_path: Path | None,
    all_agents: list[TestAgentInfo],
    all_hosts: dict[str, OnlineHostInterface],
    artifact_output_dir: Path | None = None,
    source_dir: Path | None = None,
    base_commit: str | None = None,
) -> tuple[dict[str, AgentDetails], set[str], dict[str, TestResult]]:
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
    # Results pre-read during finalization (before stopping) to avoid connection issues
    cached_results: dict[str, TestResult] = {}
    # Track when we last attempted to read each agent's result file directly.
    # Initialized to created_at so the first check happens result_check_interval_seconds later.
    last_result_check: dict[str, float] = {}

    # Initialize tracking from any pre-launched agents already in all_agents
    for info in all_agents:
        agent_id_str = str(info.agent_id)
        agent_id_to_info[agent_id_str] = info
        pending_ids.add(agent_id_str)
        last_result_check[agent_id_str] = info.created_at

    # Shared kwargs for _launch_agents_up_to_limit (avoids fragile *args tuple)
    launch_kwargs: dict = {
        "remaining_tests": remaining_tests,
        "pending_ids": pending_ids,
        "max_agents": max_agents,
        "config": config,
        "mngr_ctx": mngr_ctx,
        "pytest_flags": pytest_flags,
        "prompt_suffix": prompt_suffix,
        "all_agents": all_agents,
        "all_hosts": all_hosts,
        "agent_id_to_info": agent_id_to_info,
    }

    # Launch initial batch (no-op when remaining_tests is empty)
    _launch_agents_up_to_limit(**launch_kwargs)
    for aid in pending_ids:
        if aid not in last_result_check:
            last_result_check[aid] = agent_id_to_info[aid].created_at

    if report_path is not None:
        current_results = build_current_results(all_agents, final_details, timed_out_ids, all_hosts)
        generate_html_report(current_results, report_path, test_artifacts_dir=artifact_output_dir)

    while pending_ids or remaining_tests:
        # Check per-agent timeouts
        now = time.monotonic()
        timed_out_this_round = False
        for agent_id_str in list(pending_ids):
            info = agent_id_to_info[agent_id_str]
            elapsed = now - info.created_at
            if elapsed >= agent_timeout_seconds:
                # Before stopping, try to read the result file -- the agent may have finished
                result = try_read_agent_result(info.work_dir, all_hosts[agent_id_str])
                if result is not None:
                    logger.info(
                        "Agent '{}' has result file (found before timeout stop), treating as done", info.agent_name
                    )
                    pending_ids.discard(agent_id_str)
                    continue
                logger.warning("Agent '{}' timed out after {:.0f}s, stopping", info.agent_name, elapsed)
                _stop_agent_on_host(all_hosts[agent_id_str], AgentId(agent_id_str), info.agent_name)
                pending_ids.discard(agent_id_str)
                timed_out_ids.add(agent_id_str)
                timed_out_this_round = True

        # Launch new agents if capacity opened up
        _launch_agents_up_to_limit(**launch_kwargs)
        for aid in pending_ids:
            if aid not in last_result_check:
                last_result_check[aid] = agent_id_to_info[aid].created_at

        if timed_out_this_round and report_path is not None:
            current_results = build_current_results(all_agents, final_details, timed_out_ids, all_hosts)
            generate_html_report(current_results, report_path, test_artifacts_dir=artifact_output_dir)

        if not pending_ids and not remaining_tests:
            break

        if not pending_ids:
            continue

        pending_names = [agent_id_to_info[aid].agent_name for aid in pending_ids]
        logger.info("Polling {} pending agent(s): {}", len(pending_ids), ", ".join(str(n) for n in pending_names))
        list_result = try_list_agents(mngr_ctx)
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

            pre_read = _finalize_agent(
                agent_id=agent_detail.id,
                agent_name=agent_detail.name,
                host=all_hosts[agent_id_str],
                artifact_output_dir=artifact_output_dir,
                cg=mngr_ctx.concurrency_group,
                should_stop=agent_detail.state == AgentLifecycleState.WAITING,
            )
            if pre_read is not None:
                cached_results[agent_id_str] = pre_read
                if base_commit is not None and source_dir is not None and should_pull_changes_from_result(pre_read):
                    pull_agent_branch(
                        agent_detail.id,
                        agent_detail.name,
                        agent_detail.initial_branch,
                        all_hosts[agent_id_str],
                        source_dir,
                        mngr_ctx.concurrency_group,
                        base_commit,
                    )

        for agent_id_str in list(pending_ids):
            if agent_id_str not in seen_ids:
                rounds = missing_rounds.get(agent_id_str, 0) + 1
                missing_rounds[agent_id_str] = rounds
                if rounds >= _MISSING_AGENT_MAX_ROUNDS:
                    logger.warning("Agent {} disappeared after {} rounds, treating as error", agent_id_str, rounds)
                    pending_ids.discard(agent_id_str)
                    changed = True

        # Launch new agents if capacity opened up from finished agents
        _launch_agents_up_to_limit(**launch_kwargs)
        for aid in pending_ids:
            if aid not in last_result_check:
                last_result_check[aid] = agent_id_to_info[aid].created_at

        # Periodically check result files directly for agents whose status may be stale
        for agent_id_str in list(pending_ids):
            if now - last_result_check[agent_id_str] >= result_check_interval_seconds:
                last_result_check[agent_id_str] = now
                info = agent_id_to_info[agent_id_str]
                result = try_read_agent_result(info.work_dir, all_hosts[agent_id_str])
                if result is not None:
                    logger.info(
                        "Agent '{}' has result file (detected via direct check), treating as done",
                        info.agent_name,
                    )
                    pre_read = _finalize_agent(
                        agent_id=AgentId(agent_id_str),
                        agent_name=info.agent_name,
                        host=all_hosts[agent_id_str],
                        artifact_output_dir=artifact_output_dir,
                        cg=mngr_ctx.concurrency_group,
                        should_stop=True,
                    )
                    if pre_read is not None:
                        cached_results[agent_id_str] = pre_read
                        if (
                            base_commit is not None
                            and source_dir is not None
                            and should_pull_changes_from_result(pre_read)
                        ):
                            pull_agent_branch(
                                AgentId(agent_id_str),
                                info.agent_name,
                                info.branch_name,
                                all_hosts[agent_id_str],
                                source_dir,
                                mngr_ctx.concurrency_group,
                                base_commit,
                            )
                    pending_ids.discard(agent_id_str)
                    changed = True

        if (changed or timed_out_this_round) and report_path is not None:
            current_results = build_current_results(all_agents, final_details, timed_out_ids, all_hosts)
            generate_html_report(current_results, report_path, test_artifacts_dir=artifact_output_dir)

        if pending_ids or remaining_tests:
            time.sleep(poll_interval_seconds)

    return final_details, timed_out_ids, cached_results


def _parse_result_json(raw: str) -> TestResult:
    """Parse an outcome JSON string into a TestResult.

    Raises json.JSONDecodeError, KeyError, or ValueError on invalid data.
    """
    data = json.loads(raw)
    raw_changes = data.get("changes", {})
    changes: dict[ChangeKind, Change] = {
        ChangeKind(kind_str): Change(
            status=ChangeStatus(entry["status"]),
            summary_markdown=entry.get("summary_markdown", entry.get("summary", "")),
        )
        for kind_str, entry in raw_changes.items()
    }
    raw_runs = data.get("test_runs", [])
    test_runs = tuple(
        TestRunInfo(
            run_name=run_entry.get("run_name", ""),
            description_markdown=run_entry.get("description_markdown", ""),
        )
        for run_entry in raw_runs
    )
    return TestResult(
        changes=changes,
        errored=data.get("errored", False),
        tests_passing_before=data.get("tests_passing_before"),
        tests_passing_after=data.get("tests_passing_after"),
        summary_markdown=data.get("summary_markdown", ""),
        test_runs=test_runs,
    )


def try_read_agent_result(
    work_dir: Path,
    host: OnlineHostInterface,
) -> TestResult | None:
    """Try to read an agent's outcome file remotely, returning None if not found.

    Used to detect agents that have finished writing their result but whose
    lifecycle status has not yet updated to a terminal state.
    """
    result_path = work_dir / ".test_output" / TESTING_AGENT_OUTCOME_FILENAME
    try:
        raw = host.read_text_file(result_path)
        return _parse_result_json(raw)
    except (HostError, FileNotFoundError, OSError, json.JSONDecodeError, KeyError, ValueError):
        return None


def _stop_agent_on_host(host: OnlineHostInterface, agent_id: AgentId, agent_name: AgentName) -> None:
    """Stop a single agent on the host."""
    try:
        host.stop_agents([agent_id])
        logger.info("Stopped agent '{}'", agent_name)
    except (MngrError, HostError) as exc:
        logger.warning("Failed to stop agent '{}': {}", agent_name, exc)


def pull_agent_outputs(
    agent_id: AgentId,
    agent_name: AgentName,
    host: OnlineHostInterface,
    destination_dir: Path,
    cg: ConcurrencyGroup,
) -> TestResult | None:
    """Pull .test_output (artifacts + result.json) from an agent via rsync, then read result locally.

    Returns the parsed TestResult if result.json was found in the pulled output,
    or None if pulling failed or result.json is missing/unparseable.
    """
    try:
        agent = _get_agent_from_host(host, agent_id)
    except (MngrError, HostError, AgentNotFoundOnHostError) as exc:
        logger.warning("Could not find agent '{}' on host to pull outputs: {}", agent_name, exc)
        return None

    local_dest = destination_dir / str(agent_name)
    local_dest.mkdir(parents=True, exist_ok=True)

    try:
        pull_files(
            agent=agent,
            host=host,
            destination=local_dest,
            source_path=agent.work_dir / ".test_output",
            is_dry_run=False,
            is_delete=False,
            uncommitted_changes=UncommittedChangesMode.CLOBBER,
            cg=cg,
        )
        logger.info("Pulled .test_output from agent '{}' to {}", agent_name, local_dest)
    except (MngrError, HostError, OSError) as exc:
        logger.warning("Failed to pull .test_output from agent '{}': {}", agent_name, exc)
        return None

    return _read_local_result(local_dest, agent_name)


def _read_local_result(local_dir: Path, agent_name: AgentName) -> TestResult | None:
    """Read and parse the testing agent outcome from a locally-pulled output directory."""
    result_path = local_dir / TESTING_AGENT_OUTCOME_FILENAME
    try:
        raw = result_path.read_text()
        return _parse_result_json(raw)
    except (FileNotFoundError, OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.warning("Failed to read local result for agent '{}': {}", agent_name, exc)
        return None


def _finalize_agent(
    agent_id: AgentId,
    agent_name: AgentName,
    host: OnlineHostInterface,
    artifact_output_dir: Path | None,
    cg: ConcurrencyGroup,
    should_stop: bool,
) -> TestResult | None:
    """Pull outputs and read result from a finished agent, then optionally stop it.

    Uses rsync via pull_files to pull .test_output (which contains both artifacts
    and result.json) in a single operation, then reads result.json locally.
    The pull is done BEFORE stopping the agent to avoid connection issues with
    remote hosts that get torn down on stop.
    """
    pre_read: TestResult | None = None
    if artifact_output_dir is not None:
        pre_read = pull_agent_outputs(agent_id, agent_name, host, artifact_output_dir, cg)
    if should_stop:
        _stop_agent_on_host(host, agent_id, agent_name)
    return pre_read


def _pull_integrator_outputs(
    agent_detail: AgentDetails,
    host: OnlineHostInterface,
    destination_dir: Path,
    cg: ConcurrencyGroup,
) -> bool:
    """Pull the integrator agent's .test_output via rsync. Returns True on success."""
    try:
        agent = _get_agent_from_host(host, agent_detail.id)
    except (MngrError, HostError, AgentNotFoundOnHostError) as exc:
        logger.warning("Could not find integrator agent on host: {}", exc)
        return False

    local_dest = destination_dir / str(agent_detail.name)
    local_dest.mkdir(parents=True, exist_ok=True)
    try:
        pull_files(
            agent=agent,
            host=host,
            destination=local_dest,
            source_path=agent.work_dir / ".test_output",
            is_dry_run=False,
            is_delete=False,
            uncommitted_changes=UncommittedChangesMode.CLOBBER,
            cg=cg,
        )
        return True
    except (MngrError, HostError, OSError) as exc:
        logger.warning("Failed to pull integrator outputs: {}", exc)
        return False


def read_integrator_result(
    agent_detail: AgentDetails,
    host: OnlineHostInterface,
    branch_name: str | None,
    destination_dir: Path | None,
    cg: ConcurrencyGroup,
) -> IntegratorResult:
    """Pull the integrator agent's .test_output and read result.json."""
    empty = IntegratorResult(agent_name=agent_detail.name, branch_name=branch_name)

    # Try to pull outputs locally first, then read the local file
    if destination_dir is not None:
        _pull_integrator_outputs(agent_detail, host, destination_dir, cg)
        local_result = destination_dir / str(agent_detail.name) / INTEGRATOR_OUTCOME_FILENAME
        try:
            data = json.loads(local_result.read_text())
        except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to read integrator result locally: {}", exc)
            return empty
    else:
        # Fallback: read result.json directly from the agent's work_dir
        result_path = agent_detail.work_dir / ".test_output" / INTEGRATOR_OUTCOME_FILENAME
        try:
            data = json.loads(host.read_text_file(result_path))
        except (HostError, OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("Failed to read integrator result: {}", exc)
            return empty

    return IntegratorResult(
        agent_name=agent_detail.name,
        squashed_branches=tuple(data.get("squashed_branches", ())),
        squashed_commit_hash=data.get("squashed_commit_hash"),
        impl_priority=tuple(data.get("impl_priority", ())),
        impl_commit_hashes=data.get("impl_commit_hashes", {}),
        failed=tuple(data.get("failed", ())),
        branch_name=branch_name,
    )


def pull_agent_branch(
    agent_id: AgentId,
    agent_name: AgentName,
    branch_name: str | None,
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
    if branch_name is None:
        logger.warning("Agent '{}' has no branch to pull", agent_name)
        return None

    try:
        if base_commit is not None:
            _create_local_branch(destination, branch_name, base_commit, cg)

        pull_git(
            agent=_get_agent_from_host(host, agent_id),
            host=host,
            destination=destination,
            source_branch=branch_name,
            target_branch=branch_name,
            is_dry_run=False,
            uncommitted_changes=UncommittedChangesMode.STASH,
            cg=cg,
        )
        logger.info("Pulled branch '{}' from agent '{}'", branch_name, agent_name)
        return branch_name
    except HostError as exc:
        logger.warning("Connection lost while pulling branch from agent '{}': {}", agent_name, exc)
        return None
    except (MngrError, ProcessError) as exc:
        logger.warning("Failed to pull branch from agent '{}': {}", agent_name, exc)
        return None


def _create_local_branch(destination: Path, branch_name: str, base_commit: str, cg: ConcurrencyGroup) -> None:
    """Create a local git branch from a base commit (without checking it out).

    If the branch already exists (e.g. from a previous run), it is reused.
    The branch is not checked out -- pull_git / _fetch_and_merge handles
    checkout and restores the original branch afterward.
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
    cached_results: dict[str, TestResult] | None = None,
) -> list[TestMapReduceResult]:
    """Shared iteration over agents to build result list.

    Each agent is classified as timed-out, missing (with the caller-specified
    errored flag and summary), or finished (result read from the agent's state
    directory). The hosts dict maps agent_id strings to their respective hosts.
    """
    cached_results = cached_results or {}
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
            # Agent may have been detected as done via direct result file check
            # (without going through list_agents). Try reading result directly.
            if agent_id_str in hosts:
                direct_result = cached_results.get(agent_id_str) or try_read_agent_result(
                    agent_info.work_dir, hosts[agent_id_str]
                )
                if direct_result is not None:
                    results.append(
                        TestMapReduceResult(
                            test_node_id=agent_info.test_node_id,
                            agent_name=agent_info.agent_name,
                            changes=direct_result.changes,
                            errored=direct_result.errored,
                            tests_passing_before=direct_result.tests_passing_before,
                            tests_passing_after=direct_result.tests_passing_after,
                            summary_markdown=direct_result.summary_markdown,
                            branch_name=agent_info.branch_name,
                            test_runs=direct_result.test_runs,
                        )
                    )
                    continue
            results.append(
                TestMapReduceResult(
                    test_node_id=agent_info.test_node_id,
                    agent_name=agent_info.agent_name,
                    errored=missing_detail_errored,
                    summary_markdown=missing_detail_summary,
                )
            )
            continue

        test_result = cached_results.get(agent_id_str) or try_read_agent_result(
            agent_info.work_dir, hosts[agent_id_str]
        )
        if test_result is not None:
            results.append(
                TestMapReduceResult(
                    test_node_id=agent_info.test_node_id,
                    agent_name=agent_info.agent_name,
                    changes=test_result.changes,
                    errored=test_result.errored,
                    tests_passing_before=test_result.tests_passing_before,
                    tests_passing_after=test_result.tests_passing_after,
                    summary_markdown=test_result.summary_markdown,
                    branch_name=detail.initial_branch or agent_info.branch_name,
                    test_runs=test_result.test_runs,
                )
            )
        else:
            results.append(
                TestMapReduceResult(
                    test_node_id=agent_info.test_node_id,
                    agent_name=agent_info.agent_name,
                    errored=True,
                    summary_markdown="Failed to read agent result",
                    branch_name=detail.initial_branch or agent_info.branch_name,
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
    cached_results: dict[str, TestResult] | None = None,
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
        cached_results=cached_results,
    )

    # Pull branches from remote agents whose changes should be kept.
    # For local agents (base_commit is None), branches already exist as worktrees.
    # In the main polling flow, branches are pulled during finalization (before
    # stopping agents). This loop serves as a fallback for the reintegrate flow
    # where agents may already be stopped.
    if base_commit is not None:
        for result in results:
            if should_pull_changes(result):
                agent_id_str = next(str(info.agent_id) for info in agents if info.test_node_id == result.test_node_id)
                detail = final_details.get(agent_id_str)
                if detail is not None:
                    pull_agent_branch(
                        detail.id,
                        detail.name,
                        detail.initial_branch,
                        hosts[agent_id_str],
                        source_dir,
                        cg,
                        base_commit=base_commit,
                    )

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
    mngr_ctx: MngrContext,
) -> tuple[TestAgentInfo, OnlineHostInterface]:
    """Launch an integrator agent that cherry-picks fix branches into a linear stack."""
    short_id = _short_random_id()
    agent_name = AgentName(f"tmr-integrator-{short_id}")
    prompt = build_integrator_prompt(fix_branches)

    logger.info("Launching integrator agent '{}' to integrate {} branches", agent_name, len(fix_branches))
    create_result = _create_tmr_agent(
        agent_name=agent_name,
        branch_name=f"mngr-tmr/integrated-{short_id}",
        config=config,
        mngr_ctx=mngr_ctx,
        initial_message=prompt,
    )

    return (
        TestAgentInfo(
            test_node_id="integrator",
            agent_id=create_result.agent.id,
            agent_name=create_result.agent.name,
            work_dir=create_result.agent.work_dir,
            branch_name=f"mngr-tmr/integrated-{short_id}",
            created_at=time.monotonic(),
        ),
        create_result.host,
    )


def _try_read_integrator_outcome(work_dir: Path, host: OnlineHostInterface) -> bool:
    """Check if the integrator's outcome file exists on the remote host."""
    result_path = work_dir / ".test_output" / INTEGRATOR_OUTCOME_FILENAME
    try:
        host.read_text_file(result_path)
        return True
    except (HostError, FileNotFoundError, OSError):
        return False


def wait_for_integrator(
    integrator: TestAgentInfo,
    mngr_ctx: MngrContext,
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
        list_result = try_list_agents(mngr_ctx)
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

        # Check outcome file directly -- the integrator may have finished writing
        # before its lifecycle status updates.
        if _try_read_integrator_outcome(integrator.work_dir, host):
            logger.info("Integrator outcome file detected, treating as done")
            return integrator.branch_name

        time.sleep(poll_interval_seconds)

    logger.warning("Integrator agent timed out, stopping it")
    _stop_agent_on_host(host, AgentId(agent_id_str), integrator.agent_name)
    return None
