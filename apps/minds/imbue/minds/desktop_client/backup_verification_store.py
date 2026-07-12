"""Per-workspace "backup verification enabled" flag, owned by the minds app.

Verification (the exec-based check that a workspace's backup service matches
what minds would install today) is on by default for every workspace. A user
who genuinely doesn't want backups can disable it per workspace; while
disabled, no checks run against that workspace and no warning badge is shown
(the laptop-side snapshot status keeps working regardless).

The flag is stored as a marker file per disabled workspace under the minds
env's data dir -- absence of a marker means enabled. Marker files are tiny
and never auto-deleted on destroy (consistent with the canonical env store).
"""

from pathlib import Path

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.errors import BackupProvisioningError
from imbue.mngr.primitives import AgentId

_VERIFICATION_DISABLED_DIRNAME = "backup_verification_disabled"


def _verification_disabled_dir(paths: WorkspacePaths) -> Path:
    return paths.data_dir / _VERIFICATION_DISABLED_DIRNAME


def _marker_path(paths: WorkspacePaths, agent_id: AgentId) -> Path:
    return _verification_disabled_dir(paths) / str(agent_id)


def is_backup_verification_enabled(paths: WorkspacePaths, agent_id: AgentId) -> bool:
    """Return whether backup verification is enabled for this workspace (default True)."""
    return not _marker_path(paths, agent_id).exists()


def set_backup_verification_enabled(paths: WorkspacePaths, agent_id: AgentId, is_enabled: bool) -> None:
    """Persist the verification flag; enabling removes the marker, disabling creates it."""
    marker = _marker_path(paths, agent_id)
    try:
        if is_enabled:
            marker.unlink(missing_ok=True)
        else:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
    except OSError as e:
        raise BackupProvisioningError(f"Could not update backup verification flag at {marker}: {e}") from e
