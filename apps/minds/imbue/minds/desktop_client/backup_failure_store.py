"""Per-workspace record of a failed backup-setup attempt, owned by minds.

When backup provisioning gives up for a workspace, no canonical ``restic.env``
is written -- so without a separate marker the landing page can't tell a
*failed* setup apart from one the user deliberately skipped (both look like
"no canonical env"). This store persists a small failure record per workspace
so the status check can surface a distinct "Backup setup failed" state.

The record is transient, unlike the canonical env: it is cleared as soon as
backups are configured successfully (see ``AgentCreator._provision_backups``),
and is purely advisory (it holds no secrets -- just the error text + when it
happened).
"""

import os
from datetime import datetime
from pathlib import Path

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.errors import BackupProvisioningError
from imbue.mngr.primitives import AgentId

_BACKUP_FAILURE_DIRNAME = "backup_setup_failures"


class BackupSetupFailure(FrozenModel):
    """A persisted record that backup setup last failed for a workspace."""

    error: str = Field(description="Human-readable reason the last backup-setup attempt failed")
    failed_at: datetime = Field(description="When the failing attempt gave up (UTC)")


def _backup_failure_dir(paths: WorkspacePaths) -> Path:
    """Return the directory holding per-workspace backup-setup failure records."""
    return paths.data_dir / _BACKUP_FAILURE_DIRNAME


def backup_failure_path(paths: WorkspacePaths, agent_id: AgentId) -> Path:
    """Return the path of the failure record for ``agent_id``."""
    return _backup_failure_dir(paths) / f"{agent_id}.json"


def record_backup_setup_failure(paths: WorkspacePaths, agent_id: AgentId, failure: BackupSetupFailure) -> None:
    """Write (overwriting) the backup-setup failure record for ``agent_id``."""
    path = backup_failure_path(paths, agent_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write so a concurrent status reader never sees a partial record.
        tmp_path = path.with_suffix(".tmp")
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, failure.model_dump_json().encode("utf-8"))
        finally:
            os.close(fd)
        tmp_path.rename(path)
    except OSError as e:
        raise BackupProvisioningError(f"Could not write backup failure record at {path}: {e}") from e


def read_backup_setup_failure(paths: WorkspacePaths, agent_id: AgentId) -> BackupSetupFailure | None:
    """Return the failure record for ``agent_id``, or None if there is none.

    A malformed record (e.g. a partially-written or hand-edited file) is
    treated as absent rather than raising -- a corrupt marker must not break
    the landing-page status fill.
    """
    path = backup_failure_path(paths, agent_id)
    if not path.is_file():
        return None
    try:
        return BackupSetupFailure.model_validate_json(path.read_text())
    except (OSError, ValueError):
        return None


def clear_backup_setup_failure(paths: WorkspacePaths, agent_id: AgentId) -> None:
    """Remove any failure record for ``agent_id`` (no-op if there is none)."""
    backup_failure_path(paths, agent_id).unlink(missing_ok=True)
