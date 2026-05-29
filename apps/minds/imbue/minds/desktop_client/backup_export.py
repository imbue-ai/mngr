"""Export a workspace's latest restic snapshot as a downloadable zip.

minds holds the canonical ``restic.env`` for each workspace, so it can build a
zip of the latest snapshot from the minds machine (via ``restic dump --archive
zip``) without the workspace being reachable. The zip is written to a ``/tmp``
path keyed by host id, so repeated exports overwrite the previous file rather
than accumulating.
"""

from pathlib import Path

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client import restic_cli
from imbue.minds.desktop_client.backup_env_store import parse_restic_env
from imbue.minds.desktop_client.backup_env_store import read_canonical_env
from imbue.minds.errors import BackupProvisioningError
from imbue.mngr.primitives import AgentId

_EXPORT_DIR = Path("/tmp")
_EXPORT_FILENAME_PREFIX = "minds-backup-export-"


class BackupExportError(BackupProvisioningError):
    """Raised when a workspace's snapshot cannot be exported to a zip."""


def export_zip_path_for_host(host_id: str) -> Path:
    """Return the /tmp path the export zip is written to (keyed by host id)."""
    return _EXPORT_DIR / f"{_EXPORT_FILENAME_PREFIX}{host_id}.zip"


def export_latest_snapshot_zip(
    *,
    paths: WorkspacePaths,
    agent_id: AgentId,
    host_id: str,
    parent_cg: ConcurrencyGroup | None = None,
) -> Path:
    """Write a zip of the workspace's latest snapshot and return its path.

    Raises ``BackupExportError`` when the workspace has no canonical restic.env
    (backups were never configured) or its repository address is missing;
    propagates ``BackupProvisioningError`` if restic itself fails.
    """
    content = read_canonical_env(paths, agent_id)
    if content is None:
        raise BackupExportError(f"No backups are configured for {agent_id}")
    env = parse_restic_env(content)
    repository = env.get("RESTIC_REPOSITORY", "")
    if not repository:
        raise BackupExportError(f"Canonical restic.env for {agent_id} has no RESTIC_REPOSITORY")
    password = env.get("RESTIC_PASSWORD")
    backend_env = {key: value for key, value in env.items() if key not in ("RESTIC_REPOSITORY", "RESTIC_PASSWORD")}

    target_path = export_zip_path_for_host(host_id)
    restic_cli.dump_snapshot_archive(
        repository=repository,
        backend_env=backend_env,
        password=password,
        target_path=target_path,
        parent_cg=parent_cg,
    )
    return target_path
