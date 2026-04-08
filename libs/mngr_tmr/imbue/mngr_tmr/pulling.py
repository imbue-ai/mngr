"""Result and artifact pulling for the test-mapreduce plugin."""

import json
from pathlib import Path

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.mngr.api.pull import pull_files
from imbue.mngr.api.pull import pull_git
from imbue.mngr.errors import AgentNotFoundOnHostError
from imbue.mngr.errors import HostError
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import UncommittedChangesMode
from imbue.mngr_tmr.data_types import Change
from imbue.mngr_tmr.data_types import ChangeKind
from imbue.mngr_tmr.data_types import ChangeStatus
from imbue.mngr_tmr.data_types import IntegratorResult
from imbue.mngr_tmr.data_types import TestResult
from imbue.mngr_tmr.data_types import TestRunInfo
from imbue.mngr_tmr.launching import stop_agent_on_host
from imbue.mngr_tmr.prompts import INTEGRATOR_OUTCOME_FILENAME
from imbue.mngr_tmr.prompts import TESTING_AGENT_OUTCOME_FILENAME


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
    """Try to read an agent's outcome file remotely, returning None if not found."""
    result_path = work_dir / ".test_output" / TESTING_AGENT_OUTCOME_FILENAME
    try:
        raw = host.read_text_file(result_path)
        return _parse_result_json(raw)
    except (HostError, FileNotFoundError, OSError, json.JSONDecodeError, KeyError, ValueError):
        return None


def read_local_result(local_dir: Path, agent_name: AgentName) -> TestResult | None:
    """Read and parse the testing agent outcome from a locally-pulled output directory."""
    result_path = local_dir / TESTING_AGENT_OUTCOME_FILENAME
    try:
        raw = result_path.read_text()
        return _parse_result_json(raw)
    except (FileNotFoundError, OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.warning("Failed to read local result for agent '{}': {}", agent_name, exc)
        return None


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


def pull_agent_outputs(
    agent_id: AgentId,
    agent_name: AgentName,
    host: OnlineHostInterface,
    destination_dir: Path,
    cg: ConcurrencyGroup,
) -> TestResult | None:
    """Pull .test_output (artifacts + outcome file) from an agent via rsync, then read result locally."""
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

    return read_local_result(local_dest, agent_name)


def finalize_agent(
    agent_id: AgentId,
    agent_name: AgentName,
    host: OnlineHostInterface,
    artifact_output_dir: Path | None,
    cg: ConcurrencyGroup,
    should_stop: bool,
) -> TestResult | None:
    """Pull outputs and read result from a finished agent, then optionally stop it."""
    pre_read: TestResult | None = None
    if artifact_output_dir is not None:
        pre_read = pull_agent_outputs(agent_id, agent_name, host, artifact_output_dir, cg)
    if should_stop:
        stop_agent_on_host(host, agent_id, agent_name)
    return pre_read


def pull_integrator_outputs(
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
    """Pull the integrator agent's .test_output and read the outcome file."""
    empty = IntegratorResult(agent_name=agent_detail.name, branch_name=branch_name)

    if destination_dir is not None:
        pull_integrator_outputs(agent_detail, host, destination_dir, cg)
        local_result = destination_dir / str(agent_detail.name) / INTEGRATOR_OUTCOME_FILENAME
        try:
            data = json.loads(local_result.read_text())
        except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to read integrator result locally: {}", exc)
            return empty
    else:
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
    """Create a local git branch from a base commit (without checking it out)."""
    result = cg.run_process_to_completion(
        ["git", "branch", branch_name, base_commit],
        cwd=destination,
        is_checked_after=False,
    )
    if result.returncode == 0:
        logger.info("Created local branch '{}' from commit {}", branch_name, base_commit[:8])
    else:
        logger.info("Branch '{}' already exists, reusing it", branch_name)


def try_read_integrator_outcome(work_dir: Path, host: OnlineHostInterface) -> bool:
    """Check if the integrator's outcome file exists on the remote host."""
    result_path = work_dir / ".test_output" / INTEGRATOR_OUTCOME_FILENAME
    try:
        host.read_text_file(result_path)
        return True
    except (HostError, FileNotFoundError, OSError):
        return False
