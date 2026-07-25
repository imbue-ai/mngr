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
from imbue.minds.desktop_client import backup_status
from imbue.minds.desktop_client import restic_cli
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.backup_env_store import write_canonical_env
from imbue.minds.desktop_client.backup_update import _resolve_restore_snapshot
from imbue.minds.desktop_client.backup_update import _resolve_restore_subpath
from imbue.minds.desktop_client.backup_update import run_backup_restore_sequence
from imbue.minds.desktop_client.testing import restic_backup_a_file
from imbue.minds.desktop_client.workspace_operations import InMemoryWorkspaceOperationRegistry
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationKind
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationStatus
from imbue.minds.errors import BackupProvisioningError
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
    assert registry.start_if_idle(agent_id, WorkspaceOperationKind.BACKUP_RESTORE, datetime.now(timezone.utc), None)

    run_backup_restore_sequence(
        agent_id=agent_id,
        paths=paths,
        resolver=StaticBackendResolver(url_by_agent_and_service={}),
        registry=registry,
        parent_cg=None,
        snapshot_id="ffffffffffffffff",
        is_stop_chats=False,
        is_update_after=True,
        is_skip_safety_snapshot=False,
        is_skip_chat_gate=False,
    )

    record = registry.get(agent_id)
    assert record is not None
    assert record.status == WorkspaceOperationStatus.FAILED
    assert record.error is not None
    assert "ffffffffffffffff" in record.error
    # It never reached the point of no return, so it stayed cancellable.
    assert record.is_mutating is False


def _write_env_for_local_repo(paths: WorkspacePaths, agent_id: AgentId, repository: Path) -> None:
    write_canonical_env(paths, agent_id, f"RESTIC_REPOSITORY={repository}\nRESTIC_PASSWORD=workspace-key\n")


def _backup_tree(repository: Path, source: Path) -> None:
    restic_backup_a_file(str(repository), "workspace-key", source)


@pytest.mark.timeout(60)
def test_resolve_restore_subpath_uses_the_snapshot_root_for_plain_snapshots(tmp_path: Path) -> None:
    # Plain-docker shape: the snapshot root is the host dir itself (it carries
    # code/ directly), so the subpath is just the recorded root.
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    repository = tmp_path / "repo"
    restic_cli.init_repo(repository=str(repository), backend_env={}, password="workspace-key")
    _write_env_for_local_repo(paths, agent_id, repository)
    host = (tmp_path / "host").resolve()
    (host / "code").mkdir(parents=True)
    (host / "code" / "file.txt").write_text("content\n")
    _backup_tree(repository, host)

    snapshot = _resolve_restore_snapshot(
        agent_id=agent_id, paths=paths, snapshot_id=_only_snapshot_id(paths, agent_id), parent_cg=None
    )
    subpath = _resolve_restore_subpath(agent_id=agent_id, paths=paths, snapshot=snapshot, parent_cg=None)

    assert subpath == snapshot.paths[0]


@pytest.mark.timeout(60)
def test_resolve_restore_subpath_descends_into_the_nested_host_dir(tmp_path: Path) -> None:
    # Btrfs-volume shape: the snapshot root carries volume-level entries next
    # to a host_dir/ child holding the workspace; the subpath must point at
    # that child, never the volume level.
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    repository = tmp_path / "repo"
    restic_cli.init_repo(repository=str(repository), backend_env={}, password="workspace-key")
    _write_env_for_local_repo(paths, agent_id, repository)
    volume = (tmp_path / "volume").resolve()
    (volume / "host_dir" / "code").mkdir(parents=True)
    (volume / "host_dir" / "code" / "file.txt").write_text("content\n")
    (volume / "agents").mkdir()
    (volume / "host_state.json").write_text("{}\n")
    _backup_tree(repository, volume)

    snapshot = _resolve_restore_snapshot(
        agent_id=agent_id, paths=paths, snapshot_id=_only_snapshot_id(paths, agent_id), parent_cg=None
    )
    subpath = _resolve_restore_subpath(agent_id=agent_id, paths=paths, snapshot=snapshot, parent_cg=None)

    assert subpath == snapshot.paths[0] + "/host_dir"


@pytest.mark.timeout(60)
def test_resolve_restore_subpath_rejects_a_snapshot_without_a_workspace(tmp_path: Path) -> None:
    # A snapshot with no code/ checkout anywhere cannot be restored; the
    # dispatch must fail before the workspace is touched, not after.
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    repository = tmp_path / "repo"
    restic_cli.init_repo(repository=str(repository), backend_env={}, password="workspace-key")
    _write_env_for_local_repo(paths, agent_id, repository)
    junk = (tmp_path / "junk").resolve()
    junk.mkdir()
    (junk / "unrelated.txt").write_text("not a workspace\n")
    _backup_tree(repository, junk)

    snapshot = _resolve_restore_snapshot(
        agent_id=agent_id, paths=paths, snapshot_id=_only_snapshot_id(paths, agent_id), parent_cg=None
    )

    with pytest.raises(BackupProvisioningError, match="no code/ checkout"):
        _resolve_restore_subpath(agent_id=agent_id, paths=paths, snapshot=snapshot, parent_cg=None)


def _only_snapshot_id(paths: WorkspacePaths, agent_id: AgentId) -> str:
    snapshots = backup_status.list_workspace_snapshots(paths, agent_id, parent_cg=None)
    assert len(snapshots) == 1
    return snapshots[0].snapshot_id
