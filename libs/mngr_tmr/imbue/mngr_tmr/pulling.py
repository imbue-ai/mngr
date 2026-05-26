"""Result and artifact pulling for the test-mapreduce plugin.

Pulls the agent's outputs archive onto local disk and applies any branch
bundle. The same path serves both testing agents and the integrator -- both
publish a tarball at ``$MNGR_AGENT_STATE_DIR/plugin/<plugin>/outputs.tar.gz``.
Outcome JSON parsing lives in ``report.py``; orchestration code treats the
extracted contents as opaque.
"""

import io
import tarfile
from pathlib import Path

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.volume import Volume
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_tmr.launching import stop_agent_on_host
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
    """Download and extract the outputs archive for a single agent.

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


def has_local_branch(source_dir: Path, branch_name: str, cg: ConcurrencyGroup) -> bool:
    """Check whether a git branch exists in the local source_dir repo."""
    result = cg.run_process_to_completion(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch_name}"],
        cwd=source_dir,
        is_checked_after=False,
    )
    return result.returncode == 0


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
