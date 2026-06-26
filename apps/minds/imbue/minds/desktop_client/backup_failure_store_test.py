"""Unit tests for the per-workspace backup-setup failure store."""

from datetime import datetime
from datetime import timezone
from pathlib import Path

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.backup_failure_store import BackupSetupFailure
from imbue.minds.desktop_client.backup_failure_store import backup_failure_path
from imbue.minds.desktop_client.backup_failure_store import clear_backup_setup_failure
from imbue.minds.desktop_client.backup_failure_store import read_backup_setup_failure
from imbue.minds.desktop_client.backup_failure_store import record_backup_setup_failure
from imbue.mngr.primitives import AgentId


def _paths(tmp_path: Path) -> WorkspacePaths:
    return WorkspacePaths(data_dir=tmp_path)


def test_record_then_read_roundtrips(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    agent_id = AgentId.generate()
    failed_at = datetime(2026, 6, 26, 18, 51, 32, tzinfo=timezone.utc)
    record_backup_setup_failure(paths, agent_id, BackupSetupFailure(error="restic not found", failed_at=failed_at))

    loaded = read_backup_setup_failure(paths, agent_id)
    assert loaded is not None
    assert loaded.error == "restic not found"
    assert loaded.failed_at == failed_at
    # The record can hold secrets-adjacent error text, so keep it owner-only.
    assert (backup_failure_path(paths, agent_id).stat().st_mode & 0o777) == 0o600


def test_read_missing_returns_none(tmp_path: Path) -> None:
    assert read_backup_setup_failure(_paths(tmp_path), AgentId.generate()) is None


def test_clear_removes_the_record(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    agent_id = AgentId.generate()
    record_backup_setup_failure(
        paths, agent_id, BackupSetupFailure(error="boom", failed_at=datetime.now(timezone.utc))
    )
    clear_backup_setup_failure(paths, agent_id)
    assert read_backup_setup_failure(paths, agent_id) is None
    # Clearing again is a no-op, not an error.
    clear_backup_setup_failure(paths, agent_id)


def test_malformed_record_is_treated_as_absent(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    agent_id = AgentId.generate()
    path = backup_failure_path(paths, agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json{")
    assert read_backup_setup_failure(paths, agent_id) is None
