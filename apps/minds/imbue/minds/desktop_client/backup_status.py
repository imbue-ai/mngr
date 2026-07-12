"""Read per-workspace backup snapshot state by querying restic from the minds app.

Because minds holds the canonical ``restic.env`` for every workspace with
backups configured, it can run restic against each repository directly --
without the workspace being reachable -- to list a workspace's snapshots and
report whether a backup is running right now. Both feed the per-workspace
``GET /api/v1/workspaces/<id>/backups`` route.
"""

from datetime import datetime
from typing import Final

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client import restic_cli
from imbue.minds.desktop_client.backup_env_store import parse_restic_env
from imbue.minds.desktop_client.backup_env_store import read_canonical_env
from imbue.minds.errors import BackupProvisioningError
from imbue.mngr.primitives import AgentId

# Hard cap on each restic invocation made for status, so a slow/unreachable
# repository can't stall the route serving the backups list.
_STATUS_RESTIC_TIMEOUT_SECONDS: Final[float] = 12.0


def list_workspace_snapshots(
    paths: WorkspacePaths,
    agent_id: AgentId,
    *,
    parent_cg: ConcurrencyGroup | None = None,
    timeout_seconds: float = _STATUS_RESTIC_TIMEOUT_SECONDS,
) -> tuple[restic_cli.ResticSnapshot, ...]:
    """List a workspace's restic snapshots from its canonical restic.env.

    Works even when the workspace is offline or destroyed, because minds holds
    the canonical ``restic.env``. Raises ``BackupProvisioningError`` when no
    backups are configured (no canonical env) or its repository is missing, and
    propagates restic failures.
    """
    content = read_canonical_env(paths, agent_id)
    if content is None:
        raise BackupProvisioningError(f"No backups are configured for {agent_id}")
    env = parse_restic_env(content)
    repository = env.get("RESTIC_REPOSITORY", "")
    if not repository:
        raise BackupProvisioningError(f"Canonical restic.env for {agent_id} has no RESTIC_REPOSITORY")
    password = env.get("RESTIC_PASSWORD")
    backend_env = {key: value for key, value in env.items() if key not in ("RESTIC_REPOSITORY", "RESTIC_PASSWORD")}
    return restic_cli.list_snapshots(
        repository=repository,
        backend_env=backend_env,
        password=password,
        parent_cg=parent_cg,
        timeout_seconds=timeout_seconds,
    )


def is_workspace_backing_up(
    paths: WorkspacePaths,
    agent_id: AgentId,
    *,
    now: datetime,
    parent_cg: ConcurrencyGroup | None = None,
    restic_timeout_seconds: float = _STATUS_RESTIC_TIMEOUT_SECONDS,
) -> bool:
    """Whether a restic backup is currently running for this workspace (non-stale lock).

    A lighter probe than :func:`list_workspace_snapshots` -- it checks only
    the repository lock, not the snapshots. Returns ``False`` when no
    canonical restic.env exists or on any restic error, so a status probe never
    raises into the route that serves the backups list.
    """
    content = read_canonical_env(paths, agent_id)
    if content is None:
        return False
    env = parse_restic_env(content)
    repository = env.get("RESTIC_REPOSITORY", "")
    if not repository:
        return False
    password = env.get("RESTIC_PASSWORD")
    backend_env = {key: value for key, value in env.items() if key not in ("RESTIC_REPOSITORY", "RESTIC_PASSWORD")}
    try:
        return restic_cli.is_backup_in_progress(
            repository=repository,
            backend_env=backend_env,
            password=password,
            now=now,
            parent_cg=parent_cg,
            timeout_seconds=restic_timeout_seconds,
        )
    except BackupProvisioningError as e:
        logger.warning("Could not check backup-in-progress for {}: {}", agent_id, e)
        return False
