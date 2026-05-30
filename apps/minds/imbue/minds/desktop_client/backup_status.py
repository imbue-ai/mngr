"""Compute per-workspace backup status by querying restic from the minds app.

Because minds holds the canonical ``restic.env`` for every workspace with
backups configured, it can run restic against each repository directly --
without the workspace being reachable -- to report when the last backup
succeeded and whether one is currently running. The landing page fetches
this once on load.
"""

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from concurrent.futures import wait
from datetime import datetime
from datetime import timezone
from enum import auto
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client import restic_cli
from imbue.minds.desktop_client.backup_env_store import parse_restic_env
from imbue.minds.desktop_client.backup_env_store import read_canonical_env
from imbue.minds.errors import BackupProvisioningError
from imbue.mngr.primitives import AgentId

# Wall-clock budget for the whole batch status check. After this elapses the
# response is returned (still-running workspaces report UNKNOWN); the executor
# is shut down non-blocking so a slow/unreachable repo can't stall the route.
_STATUS_BATCH_TIMEOUT_SECONDS: Final[float] = 20.0
# Hard cap on each restic invocation made for status, so a straggler thread
# that outlives the batch budget still terminates promptly in the background.
_STATUS_RESTIC_TIMEOUT_SECONDS: Final[float] = 12.0
_MAX_STATUS_WORKERS: Final[int] = 8


class BackupStatusState(UpperCaseStrEnum):
    """The backup state shown for a project on the landing page."""

    # No canonical env -- backups were never configured for this workspace.
    NOT_CONFIGURED = auto()
    # Configured, but no successful snapshot exists yet.
    NEVER = auto()
    # At least one snapshot exists; ``last_success_at`` is populated.
    BACKED_UP = auto()
    # A non-stale restic lock indicates a backup is running right now.
    BACKING_UP = auto()
    # restic errored / timed out / the env was malformed.
    UNKNOWN = auto()


class BackupStatus(FrozenModel):
    """The backup status of a single workspace."""

    state: BackupStatusState = Field(description="Coarse backup state for the project tile")
    last_success_at: datetime | None = Field(
        default=None,
        description="Time of the most recent successful snapshot, when known (UTC)",
    )


def compute_backup_status_for_workspace(
    paths: WorkspacePaths,
    agent_id: AgentId,
    *,
    now: datetime,
    parent_cg: ConcurrencyGroup | None = None,
    restic_timeout_seconds: float = _STATUS_RESTIC_TIMEOUT_SECONDS,
) -> BackupStatus:
    """Compute the backup status for one workspace from its canonical restic.env.

    Returns ``NOT_CONFIGURED`` when no canonical env exists, and ``UNKNOWN``
    on any restic error / malformed env rather than propagating -- a single
    bad repo must not break the whole landing-page status fill.
    """
    content = read_canonical_env(paths, agent_id)
    if content is None:
        return BackupStatus(state=BackupStatusState.NOT_CONFIGURED)

    env = parse_restic_env(content)
    repository = env.get("RESTIC_REPOSITORY", "")
    if not repository:
        logger.warning("Canonical restic.env for {} has no RESTIC_REPOSITORY", agent_id)
        return BackupStatus(state=BackupStatusState.UNKNOWN)
    password = env.get("RESTIC_PASSWORD")
    backend_env = {key: value for key, value in env.items() if key not in ("RESTIC_REPOSITORY", "RESTIC_PASSWORD")}

    try:
        if restic_cli.is_backup_in_progress(
            repository=repository,
            backend_env=backend_env,
            password=password,
            now=now,
            parent_cg=parent_cg,
            timeout_seconds=restic_timeout_seconds,
        ):
            last_success = _safe_latest_snapshot(repository, backend_env, password, parent_cg, restic_timeout_seconds)
            return BackupStatus(state=BackupStatusState.BACKING_UP, last_success_at=last_success)
        last_success = restic_cli.get_latest_snapshot_time(
            repository=repository,
            backend_env=backend_env,
            password=password,
            parent_cg=parent_cg,
            timeout_seconds=restic_timeout_seconds,
        )
    except BackupProvisioningError as e:
        logger.warning("Could not read backup status for {}: {}", agent_id, e)
        return BackupStatus(state=BackupStatusState.UNKNOWN)

    if last_success is None:
        return BackupStatus(state=BackupStatusState.NEVER)
    return BackupStatus(state=BackupStatusState.BACKED_UP, last_success_at=last_success)


def _safe_latest_snapshot(
    repository: str,
    backend_env: dict[str, str],
    password: str | None,
    parent_cg: ConcurrencyGroup | None,
    timeout_seconds: float,
) -> datetime | None:
    """Best-effort latest-snapshot lookup used while a backup is in progress."""
    try:
        return restic_cli.get_latest_snapshot_time(
            repository=repository,
            backend_env=backend_env,
            password=password,
            parent_cg=parent_cg,
            timeout_seconds=timeout_seconds,
        )
    except BackupProvisioningError:
        return None


def compute_backup_status_for_workspaces(
    paths: WorkspacePaths,
    agent_ids: Sequence[AgentId],
    *,
    parent_cg: ConcurrencyGroup | None = None,
) -> dict[str, BackupStatus]:
    """Compute backup status for many workspaces in parallel, bounded in wall-clock.

    Returns within roughly ``_STATUS_BATCH_TIMEOUT_SECONDS``: any workspace
    whose check hasn't finished by then is reported as ``UNKNOWN`` so a single
    slow/unreachable repository never stalls the landing page. The executor is
    shut down non-blocking, so straggler threads (each bounded by the inner
    restic timeout) finish in the background after the response is sent.
    """
    if not agent_ids:
        return {}
    now = datetime.now(timezone.utc)
    result_by_agent_id: dict[str, BackupStatus] = {}
    worker_count = min(_MAX_STATUS_WORKERS, len(agent_ids))
    executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="backup-status")
    try:
        future_by_agent_id = {
            agent_id: executor.submit(
                compute_backup_status_for_workspace, paths, agent_id, now=now, parent_cg=parent_cg
            )
            for agent_id in agent_ids
        }
        wait(future_by_agent_id.values(), timeout=_STATUS_BATCH_TIMEOUT_SECONDS)
        for agent_id, future in future_by_agent_id.items():
            try:
                result_by_agent_id[str(agent_id)] = future.result(timeout=0)
            except (FuturesTimeoutError, BackupProvisioningError) as e:
                logger.warning("Backup status for {} unavailable: {}", agent_id, e)
                result_by_agent_id[str(agent_id)] = BackupStatus(state=BackupStatusState.UNKNOWN)
    finally:
        # Non-blocking: don't wait on stragglers (that would defeat the batch
        # timeout). cancel_futures drops any not-yet-started task.
        executor.shutdown(wait=False, cancel_futures=True)
    return result_by_agent_id
