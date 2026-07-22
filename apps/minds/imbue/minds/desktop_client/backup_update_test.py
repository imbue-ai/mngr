"""Tests for the desktop-side backup operation workers.

The restore worker resolves its target snapshot from minds' own view of the
repository before it touches the workspace, so these run against a real local
restic repo and never need a reachable workspace.
"""

from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client import restic_cli
from imbue.minds.desktop_client.backup_env_store import write_canonical_env
from imbue.minds.desktop_client.backup_update import run_backup_restore_sequence
from imbue.minds.desktop_client.testing import restic_backup_a_file
from imbue.minds.desktop_client.workspace_operations import InMemoryWorkspaceOperationRegistry
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationKind
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationStatus
from imbue.mngr.primitives import AgentId


@pytest.mark.timeout(60)
def test_restore_fails_for_an_unknown_snapshot_without_dispatching_a_worker(tmp_path: Path) -> None:
    # An id that is not in the repository must fail the operation outright --
    # before the gate waits, before the workspace is touched at all. Nothing
    # here can reach a workspace, so a run that got as far as dispatching an
    # exec would fail loudly rather than pass.
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    repository = str(tmp_path / "repo")
    password = "workspace-key"
    restic_cli.init_repo(repository=repository, backend_env={}, password=password)
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("backed up\n")
    restic_backup_a_file(repository, password, source)
    write_canonical_env(paths, agent_id, f"RESTIC_REPOSITORY={repository}\nRESTIC_PASSWORD={password}\n")

    registry = InMemoryWorkspaceOperationRegistry()
    assert registry.start_if_idle(agent_id, WorkspaceOperationKind.BACKUP_RESTORE, datetime.now(timezone.utc))

    run_backup_restore_sequence(
        agent_id=agent_id,
        paths=paths,
        registry=registry,
        parent_cg=None,
        snapshot_id="ffffffffffffffff",
        is_stop_chats=False,
    )

    record = registry.get(agent_id)
    assert record is not None
    assert record.status == WorkspaceOperationStatus.FAILED
    assert record.error is not None
    assert "ffffffffffffffff" in record.error
    # It never reached the point of no return, so it stayed cancellable.
    assert record.is_mutating is False
