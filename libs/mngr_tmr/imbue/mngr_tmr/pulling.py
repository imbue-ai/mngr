"""Result and artifact pulling for the test-mapreduce plugin."""

import io
import json
import tarfile
from pathlib import Path

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.api.pull import pull_files
from imbue.mngr.api.pull import pull_git
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import AgentNotFoundOnHostError
from imbue.mngr.errors import HostError
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.volume import Volume
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import UncommittedChangesMode
from imbue.mngr_tmr.data_types import Change
from imbue.mngr_tmr.data_types import ChangeKind
from imbue.mngr_tmr.data_types import ChangeStatus
from imbue.mngr_tmr.data_types import IntegratorResult
from imbue.mngr_tmr.data_types import TestResult
from imbue.mngr_tmr.data_types import TestRunInfo
from imbue.mngr_tmr.launching import stop_agent_on_host
from imbue.mngr_tmr.prompts import INTEGRATOR_OUTCOME_FILENAME
from imbue.mngr_tmr.prompts import PLUGIN_NAME
from imbue.mngr_tmr.prompts import TESTING_AGENT_OUTCOME_FILENAME

_OUTPUTS_ARCHIVE_NAME = "outputs.tar.gz"
_OUTPUTS_ARCHIVE_SUBPATH = f"plugin/{PLUGIN_NAME}/{_OUTPUTS_ARCHIVE_NAME}"
_OUTPUTS_ARCHIVE_PARENT_SUBPATH = f"plugin/{PLUGIN_NAME}"
_EXTRACTED_TEST_OUTPUT_DIR = "test_output"
_BRANCH_BUNDLE_NAME = "branch.bundle"


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


def read_local_result(local_dir: Path, agent_name: AgentName) -> TestResult | None:
    """Read and parse the testing agent outcome from a locally-extracted output directory."""
    result_path = local_dir / _EXTRACTED_TEST_OUTPUT_DIR / TESTING_AGENT_OUTCOME_FILENAME
    try:
        raw = result_path.read_text()
        return _parse_result_json(raw)
    except (FileNotFoundError, OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.warning("Failed to read local result for agent '{}': {}", agent_name, exc)
        return None


def _get_agent_volume(
    mngr_ctx: MngrContext,
    provider_name: ProviderInstanceName,
    host_id: HostId,
    agent_id: AgentId,
) -> Volume | None:
    """Return the volume scoped to a single agent's state directory.

    Returns None when the provider does not expose a host volume (e.g. SSH)
    or when the volume cannot otherwise be resolved.
    """
    try:
        provider = get_provider_instance(provider_name, mngr_ctx)
        host_volume = provider.get_volume_for_host(host_id)
    except (MngrError, OSError) as exc:
        logger.warning("Failed to resolve volume for host {}: {}", host_id, exc)
        return None
    if host_volume is None:
        return None
    return host_volume.get_agent_volume(agent_id)


def is_agent_outputs_ready(
    mngr_ctx: MngrContext,
    provider_name: ProviderInstanceName,
    host_id: HostId,
    agent_id: AgentId,
) -> bool:
    """Check whether the agent has finished writing its outputs archive.

    Listing the archive's parent directory rather than reading the archive
    itself keeps the existence check cheap; the partially-written .tmp file
    is filtered out by matching the final name exactly.
    """
    agent_volume = _get_agent_volume(mngr_ctx, provider_name, host_id, agent_id)
    if agent_volume is None:
        return False
    try:
        entries = agent_volume.listdir(_OUTPUTS_ARCHIVE_PARENT_SUBPATH)
    except (MngrError, OSError):
        return False
    return any(Path(entry.path).name == _OUTPUTS_ARCHIVE_NAME for entry in entries)


def _apply_branch_bundle(
    source_dir: Path,
    bundle_path: Path,
    branch_name: str,
    agent_name: AgentName,
    cg: ConcurrencyGroup,
) -> None:
    """Fetch a branch from a bundle into the local source_dir repo.

    The bundle was created with ``git bundle create ... <base>..<branch>``,
    so it carries the ref under its branch name; the fetch refspec maps
    that ref onto the same local branch name. Idempotent for repeated
    invocations and a no-op on the local provider (where the branch
    already exists in the worktree-sharing repo).
    """
    result = cg.run_process_to_completion(
        ["git", "fetch", "--no-tags", str(bundle_path), f"+{branch_name}:{branch_name}"],
        cwd=source_dir,
        is_checked_after=False,
    )
    if result.returncode != 0:
        logger.warning(
            "Failed to apply branch bundle for agent '{}' (branch {}): {}",
            agent_name,
            branch_name,
            result.stderr.strip(),
        )
    else:
        logger.info("Applied branch bundle for agent '{}' onto branch '{}'", agent_name, branch_name)


def pull_agent_outputs(
    mngr_ctx: MngrContext,
    provider_name: ProviderInstanceName,
    host_id: HostId,
    agent_id: AgentId,
    agent_name: AgentName,
    branch_name: str | None,
    destination_dir: Path,
    source_dir: Path | None,
    cg: ConcurrencyGroup,
) -> TestResult | None:
    """Download and extract the outputs archive for a single testing agent.

    Reads ``outputs.tar.gz`` from the agent's state volume, extracts it
    under ``destination_dir/<agent_name>/``, and (if a ``branch.bundle`` is
    present and ``source_dir`` is given) fetches the bundled branch into
    the local repo. Returns the parsed outcome read from the extracted
    ``test_output/`` directory, or None on any failure.
    """
    agent_volume = _get_agent_volume(mngr_ctx, provider_name, host_id, agent_id)
    if agent_volume is None:
        logger.warning(
            "Cannot pull outputs for agent '{}': provider '{}' does not expose a volume",
            agent_name,
            provider_name,
        )
        return None

    try:
        archive_bytes = agent_volume.read_file(_OUTPUTS_ARCHIVE_SUBPATH)
    except (MngrError, OSError) as exc:
        logger.warning("Failed to read outputs archive for agent '{}': {}", agent_name, exc)
        return None

    local_dest = destination_dir / str(agent_name)
    local_dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
            tar.extractall(local_dest, filter="data")
    except (tarfile.TarError, OSError) as exc:
        logger.warning("Failed to extract outputs archive for agent '{}': {}", agent_name, exc)
        return None
    logger.info("Extracted outputs archive for agent '{}' to {}", agent_name, local_dest)

    if source_dir is not None and branch_name is not None:
        bundle_path = local_dest / _BRANCH_BUNDLE_NAME
        if bundle_path.is_file():
            _apply_branch_bundle(source_dir, bundle_path, branch_name, agent_name, cg)

    return read_local_result(local_dest, agent_name)


def finalize_agent(
    mngr_ctx: MngrContext,
    provider_name: ProviderInstanceName,
    host: OnlineHostInterface,
    agent_id: AgentId,
    agent_name: AgentName,
    branch_name: str | None,
    artifact_output_dir: Path | None,
    source_dir: Path | None,
    cg: ConcurrencyGroup,
    should_stop: bool,
) -> TestResult | None:
    """Pull outputs and read result from a finished agent, then optionally stop it."""
    result: TestResult | None = None
    if artifact_output_dir is not None:
        result = pull_agent_outputs(
            mngr_ctx=mngr_ctx,
            provider_name=provider_name,
            host_id=host.id,
            agent_id=agent_id,
            agent_name=agent_name,
            branch_name=branch_name,
            destination_dir=artifact_output_dir,
            source_dir=source_dir,
            cg=cg,
        )
    if should_stop:
        stop_agent_on_host(host, agent_id, agent_name)
    return result


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

    Used by the integrator path; testing agents go through the bundle
    contained in their outputs archive instead.

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
