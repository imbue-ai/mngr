"""Result and artifact pulling for the test-mapreduce plugin.

Pulls the agent's outputs archive (test agents) or rsyncs ``.test_output``
(integrator) onto local disk and applies any branch bundle. Outcome JSON
parsing lives in ``report.py``; the orchestration code that calls this
module treats the extracted contents as an opaque blob.
"""

import io
import tarfile
from pathlib import Path

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.api.sync import git_pull
from imbue.mngr.api.sync import rsync_from_remote
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import AgentNotFoundOnHostError
from imbue.mngr.errors import HostError
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.volume import Volume
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import UncommittedChangesMode
from imbue.mngr_tmr.launching import stop_agent_on_host
from imbue.mngr_tmr.prompts import INTEGRATOR_OUTCOME_FILENAME
from imbue.mngr_tmr.prompts import PLUGIN_NAME

_OUTPUTS_ARCHIVE_NAME = "outputs.tar.gz"
_OUTPUTS_ARCHIVE_SUBPATH = f"plugin/{PLUGIN_NAME}/{_OUTPUTS_ARCHIVE_NAME}"
_BRANCH_BUNDLE_NAME = "branch.bundle"


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

    Matches the final archive name exactly so the partially-written ``.tmp``
    file (which the agent renames on completion) is not mistaken for the
    finished output.
    """
    agent_volume = _get_agent_volume(mngr_ctx, provider_name, host_id, agent_id)
    if agent_volume is None:
        return False
    try:
        return agent_volume.path_exists(_OUTPUTS_ARCHIVE_SUBPATH)
    except (MngrError, OSError):
        return False


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
) -> bool:
    """Download and extract the outputs archive for a single testing agent.

    Reads ``outputs.tar.gz`` from the agent's state volume, extracts it
    under ``destination_dir/<agent_name>/``, and (if a ``branch.bundle`` is
    present and ``source_dir`` is given) fetches the bundled branch into
    the local repo. Returns True on success, False on any failure. The
    extracted contents are treated as opaque here -- the reporter parses
    the outcome JSON on demand.
    """
    agent_volume = _get_agent_volume(mngr_ctx, provider_name, host_id, agent_id)
    if agent_volume is None:
        logger.warning(
            "Cannot pull outputs for agent '{}': provider '{}' does not expose a volume",
            agent_name,
            provider_name,
        )
        return False

    try:
        archive_bytes = agent_volume.read_file(_OUTPUTS_ARCHIVE_SUBPATH)
    except (MngrError, OSError) as exc:
        logger.warning("Failed to read outputs archive for agent '{}': {}", agent_name, exc)
        return False

    local_dest = destination_dir / str(agent_name)
    local_dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
            tar.extractall(local_dest, filter="data")
    except (tarfile.TarError, OSError) as exc:
        logger.warning("Failed to extract outputs archive for agent '{}': {}", agent_name, exc)
        return False
    logger.info("Extracted outputs archive for agent '{}' to {}", agent_name, local_dest)

    if source_dir is not None and branch_name is not None:
        bundle_path = local_dest / _BRANCH_BUNDLE_NAME
        if bundle_path.is_file():
            _apply_branch_bundle(source_dir, bundle_path, branch_name, agent_name, cg)

    return True


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
) -> bool:
    """Pull outputs from a finished agent, then optionally stop it. Returns pull success."""
    pulled = True
    if artifact_output_dir is not None:
        pulled = pull_agent_outputs(
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
    return pulled


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
    agent_id: AgentId,
    agent_name: AgentName,
    host: OnlineHostInterface,
    destination_dir: Path,
    cg: ConcurrencyGroup,
) -> bool:
    """Pull the integrator agent's .test_output via rsync. Returns True on success.

    Contents land directly under ``destination_dir/<agent_name>/`` (no
    ``test_output/`` subdir, because rsync flattens the trailing-slashed
    source). The reporter parses the outcome JSON from that location.
    """
    try:
        agent = _get_agent_from_host(host, agent_id)
    except (MngrError, HostError, AgentNotFoundOnHostError) as exc:
        logger.warning("Could not find integrator agent on host: {}", exc)
        return False

    local_dest = destination_dir / str(agent_name)
    local_dest.mkdir(parents=True, exist_ok=True)
    try:
        rsync_from_remote(
            remote_host=host,
            remote_path=agent.work_dir / ".test_output",
            local_path=local_dest,
            is_dry_run=False,
            is_delete=False,
            uncommitted_changes=UncommittedChangesMode.CLOBBER,
            cg=cg,
        )
        return True
    except (MngrError, HostError, OSError) as exc:
        logger.warning("Failed to pull integrator outputs: {}", exc)
        return False


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

        agent_for_branch = _get_agent_from_host(host, agent_id)
        git_pull(
            local_path=destination,
            remote_host=host,
            remote_path=agent_for_branch.work_dir,
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


def is_integrator_outputs_ready(work_dir: Path, host: OnlineHostInterface) -> bool:
    """Check if the integrator's outcome file exists on the remote host."""
    result_path = work_dir / ".test_output" / INTEGRATOR_OUTCOME_FILENAME
    try:
        host.read_text_file(result_path)
        return True
    except (HostError, FileNotFoundError, OSError):
        return False
