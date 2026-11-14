"""Tests for the desktop-side backup operation workers.

The restore worker resolves its target snapshot from Imbue Studio's own view of the
repository before it touches the machine, so these run against a real local
restic repo and never need a reachable machine.
"""

from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest
from pydantic import AnyUrl

from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client import backup_status
from imbue.minds.desktop_client import restic_cli
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.backup_env_store import write_canonical_env
from imbue.minds.desktop_client.backup_provisioning import BackupSetupRequest
from imbue.minds.desktop_client.backup_provisioning import RESTIC_PREVIOUS_ENV_REMOTE_PATH
from imbue.minds.desktop_client.backup_update import BELOW_UPDATE_FLOOR_MESSAGE
from imbue.minds.desktop_client.backup_update import BackupOperationFailure
from imbue.minds.desktop_client.backup_update import _apply_update_and_verify
from imbue.minds.desktop_client.backup_update import _chained_update_warning
from imbue.minds.desktop_client.backup_update import _complete_restore
from imbue.minds.desktop_client.backup_update import _end_unfinished_restore
from imbue.minds.desktop_client.backup_update import _resolve_restore_snapshot
from imbue.minds.desktop_client.backup_update import _resolve_restore_subpath
from imbue.minds.desktop_client.backup_update import _restore_services_down
from imbue.minds.desktop_client.backup_update import build_restore_program_arguments
from imbue.minds.desktop_client.backup_update import run_backup_configure_sequence
from imbue.minds.desktop_client.backup_update import run_backup_restore_sequence
from imbue.minds.desktop_client.backup_update import run_backup_update_sequence
from imbue.minds.desktop_client.imbue_cloud_cli import BackupRepositoryCredentials
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudQuotaExceededCliError
from imbue.minds.desktop_client.testing import restic_backup_a_file
from imbue.minds.desktop_client.workspace_facts_cache import WorkspaceFactsCache
from imbue.minds.desktop_client.workspace_operations import InMemoryWorkspaceOperationRegistry
from imbue.minds.desktop_client.workspace_operations import OperationFailureReason
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationKind
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationStatus
from imbue.minds.errors import BackupProvisioningError
from imbue.minds.primitives import BackupProvider
from imbue.minds.workspace_programs.backup_protocol import BackupProgramFailureReason
from imbue.mngr.primitives import AgentId


@pytest.mark.timeout(60)
def test_restore_fails_for_an_unknown_snapshot_without_dispatching_a_worker(tmp_path: Path) -> None:
    # An id that is not in the repository must fail the operation outright --
    # before the gate waits, before the workspace is touched at all. Nothing
    # here can reach a workspace, so a run that got as far as dispatching an
    # exec would fail loudly rather than pass.
    paths = InstallationPaths(data_dir=tmp_path)
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
        facts_cache=WorkspaceFactsCache(records_dir=tmp_path / "workspace_facts_cache"),
        registry=registry,
        parent_cg=None,
        snapshot_id="ffffffffffffffff",
        is_stop_chats=False,
        is_update_after=True,
        is_skip_safety_snapshot=False,
        is_skip_chat_gate=False,
        workspace_version_ref="minds-v0.5.2",
    )

    record = registry.get(agent_id)
    assert record is not None
    assert record.status == WorkspaceOperationStatus.FAILED
    assert record.error is not None
    assert "ffffffffffffffff" in record.error
    # A backup that is not there is told apart from one that could not be read.
    assert record.failure_reason == OperationFailureReason.SNAPSHOT_UNRESTORABLE
    # It never reached the point of no return, so it stayed cancellable.
    assert record.is_mutating is False


def _write_env_for_local_repo(paths: InstallationPaths, agent_id: AgentId, repository: Path) -> None:
    write_canonical_env(paths, agent_id, f"RESTIC_REPOSITORY={repository}\nRESTIC_PASSWORD=workspace-key\n")


def _backup_tree(repository: Path, source: Path, *, is_tree_at_snapshot_root: bool = False) -> None:
    restic_backup_a_file(str(repository), "workspace-key", source, is_tree_at_snapshot_root=is_tree_at_snapshot_root)


@pytest.mark.timeout(60)
def test_resolve_restore_subpath_uses_the_snapshot_root_for_current_layout_snapshots(tmp_path: Path) -> None:
    # Current layout: the snapshot root is the unified /home/user tree, whose
    # repo checkout is a workspace/ child; the subpath is the recorded root.
    paths = InstallationPaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    repository = tmp_path / "repo"
    restic_cli.init_repo(repository=str(repository), backend_env={}, password="workspace-key")
    _write_env_for_local_repo(paths, agent_id, repository)
    home = (tmp_path / "home-user").resolve()
    (home / "workspace").mkdir(parents=True)
    (home / "workspace" / "file.txt").write_text("content\n")
    (home / ".mngr").mkdir()
    _backup_tree(repository, home)

    snapshot = _resolve_restore_snapshot(
        agent_id=agent_id, paths=paths, snapshot_id=_only_snapshot_id(paths, agent_id), archive_id=None, parent_cg=None
    )
    subpath = _resolve_restore_subpath(
        agent_id=agent_id, paths=paths, snapshot=snapshot, archive_id=None, parent_cg=None
    )

    assert subpath == snapshot.paths[0]


@pytest.mark.timeout(60)
def test_resolve_restore_subpath_uses_the_tree_root_for_snapshots_stored_at_the_root(tmp_path: Path) -> None:
    # host_backup backs up `.` from inside the backup root, so the snapshot
    # holds workspace/ at its tree root while its recorded path is still the
    # absolute directory it ran in -- which, in the snapshot, is empty.
    paths = InstallationPaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    repository = tmp_path / "repo"
    restic_cli.init_repo(repository=str(repository), backend_env={}, password="workspace-key")
    _write_env_for_local_repo(paths, agent_id, repository)
    home = (tmp_path / "snapshots" / "2026-09-24T10:00:00.000000Z" / "home").resolve()
    (home / "workspace").mkdir(parents=True)
    (home / "workspace" / "file.txt").write_text("content\n")
    _backup_tree(repository, home, is_tree_at_snapshot_root=True)

    snapshot = _resolve_restore_snapshot(
        agent_id=agent_id, paths=paths, snapshot_id=_only_snapshot_id(paths, agent_id), archive_id=None, parent_cg=None
    )
    subpath = _resolve_restore_subpath(
        agent_id=agent_id, paths=paths, snapshot=snapshot, archive_id=None, parent_cg=None
    )

    assert snapshot.paths[0] == str(home)
    assert subpath == "/"


@pytest.mark.timeout(60)
def test_resolve_restore_subpath_uses_the_snapshot_root_for_plain_snapshots(tmp_path: Path) -> None:
    # Legacy plain-docker shape: the snapshot root is the host dir itself (it
    # carries code/ directly), so the subpath is just the recorded root.
    paths = InstallationPaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    repository = tmp_path / "repo"
    restic_cli.init_repo(repository=str(repository), backend_env={}, password="workspace-key")
    _write_env_for_local_repo(paths, agent_id, repository)
    host = (tmp_path / "host").resolve()
    (host / "code").mkdir(parents=True)
    (host / "code" / "file.txt").write_text("content\n")
    _backup_tree(repository, host)

    snapshot = _resolve_restore_snapshot(
        agent_id=agent_id, paths=paths, snapshot_id=_only_snapshot_id(paths, agent_id), archive_id=None, parent_cg=None
    )
    subpath = _resolve_restore_subpath(
        agent_id=agent_id, paths=paths, snapshot=snapshot, archive_id=None, parent_cg=None
    )

    assert subpath == snapshot.paths[0]


@pytest.mark.flaky
@pytest.mark.timeout(60)
def test_resolve_restore_subpath_descends_into_the_nested_host_dir(tmp_path: Path) -> None:
    # Btrfs-volume shape: the snapshot root carries volume-level entries next
    # to a host_dir/ child holding the workspace; the subpath must point at
    # that child, never the volume level.
    paths = InstallationPaths(data_dir=tmp_path)
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
        agent_id=agent_id, paths=paths, snapshot_id=_only_snapshot_id(paths, agent_id), archive_id=None, parent_cg=None
    )
    subpath = _resolve_restore_subpath(
        agent_id=agent_id, paths=paths, snapshot=snapshot, archive_id=None, parent_cg=None
    )

    assert subpath == snapshot.paths[0] + "/host_dir"


@pytest.mark.timeout(60)
def test_resolve_restore_subpath_rejects_a_snapshot_without_a_workspace(tmp_path: Path) -> None:
    # A snapshot with no workspace/ or code/ checkout anywhere cannot be
    # restored; the dispatch must fail before the workspace is touched, not after.
    paths = InstallationPaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    repository = tmp_path / "repo"
    restic_cli.init_repo(repository=str(repository), backend_env={}, password="workspace-key")
    _write_env_for_local_repo(paths, agent_id, repository)
    junk = (tmp_path / "junk").resolve()
    junk.mkdir()
    (junk / "unrelated.txt").write_text("not a machine\n")
    _backup_tree(repository, junk)

    snapshot = _resolve_restore_snapshot(
        agent_id=agent_id, paths=paths, snapshot_id=_only_snapshot_id(paths, agent_id), archive_id=None, parent_cg=None
    )

    with pytest.raises(BackupProvisioningError, match="no workspace/ or code/ checkout"):
        _resolve_restore_subpath(agent_id=agent_id, paths=paths, snapshot=snapshot, archive_id=None, parent_cg=None)


def _only_snapshot_id(paths: InstallationPaths, agent_id: AgentId) -> str:
    snapshots = backup_status.list_workspace_snapshots(paths, agent_id, parent_cg=None)
    assert len(snapshots) == 1
    return snapshots[0].snapshot_id


def test_restore_services_down_carries_the_names_as_data() -> None:
    # The restore script's services_down list (services outside the
    # restore-critical set that did not come back) rides on the record for the
    # UI to word; a payload without the list yields none, keeping clean
    # restores caveat-free.
    assert _restore_services_down({"services_down": ["browser", "xvfb"]}) == ("browser", "xvfb")
    assert _restore_services_down({}) == ()
    assert _restore_services_down({"services_down": []}) == ()


_UNREADABLE_CONFIG_OUTPUT = "ERROR: CANT_REREAD: File contains no section headers."


def _started_restore() -> tuple[InMemoryWorkspaceOperationRegistry, AgentId]:
    registry = InMemoryWorkspaceOperationRegistry()
    agent_id = AgentId.generate()
    registry.start(agent_id, WorkspaceOperationKind.BACKUP_RESTORE, datetime.now(timezone.utc))
    return registry, agent_id


@pytest.mark.witnesses(
    "backup-restore.restored-configuration-unreadable", partial="covers the operation record the UI words it from"
)
def test_a_restore_whose_restored_service_config_is_unreadable_completes_with_it_as_data() -> None:
    # The restore program brings the services back with `restart all` when the
    # restored supervisord config does not parse. The restore still succeeded,
    # and the record carries supervisorctl's output for the UI to word.
    registry, agent_id = _started_restore()

    _complete_restore(
        registry,
        agent_id,
        None,
        {"status": "ok", "services_down": ["browser"], "supervisord_config_error": _UNREADABLE_CONFIG_OUTPUT},
    )

    record = registry.get(agent_id)
    assert record is not None
    assert record.status == WorkspaceOperationStatus.DONE
    assert record.service_config_error == _UNREADABLE_CONFIG_OUTPUT
    assert record.services_down == ("browser",)
    assert record.warning is None


def test_a_failed_restore_keeps_its_reason_and_carries_an_unreadable_service_config_apart() -> None:
    # The program resumes the services on its way out of a failure too, so a
    # failed restore can also report the restored config unreadable. It rides
    # on the record next to the failure, not inside the failure text.
    registry, agent_id = _started_restore()

    is_ended = _end_unfinished_restore(
        registry,
        agent_id,
        {
            "status": "failed",
            "detail": "restored, but uv sync failed: resolver error",
            "safety_snapshot_taken": True,
            "supervisord_config_error": _UNREADABLE_CONFIG_OUTPUT,
        },
    )

    assert is_ended
    record = registry.get(agent_id)
    assert record is not None
    assert record.status == WorkspaceOperationStatus.FAILED
    assert record.failure_reason == OperationFailureReason.RESTORE_INCOMPLETE_AFTER_SAFETY_BACKUP
    assert record.service_config_error == _UNREADABLE_CONFIG_OUTPUT
    assert record.error is not None
    assert _UNREADABLE_CONFIG_OUTPUT not in record.error


def test_a_failed_restore_takes_the_reason_its_program_named() -> None:
    # The UI words the failure from the reason the in-workspace program named.
    registry, agent_id = _started_restore()

    is_ended = _end_unfinished_restore(
        registry,
        agent_id,
        {
            "status": "failed",
            "detail": "pre-restore safety snapshot failed: disk full",
            "failure_reason": "SAFETY_SNAPSHOT_FAILED",
        },
    )

    assert is_ended
    record = registry.get(agent_id)
    assert record is not None
    assert record.failure_reason == BackupProgramFailureReason.SAFETY_SNAPSHOT_FAILED
    assert record.service_config_error is None


def test_a_restore_its_program_reported_ok_is_left_running_for_the_later_phases() -> None:
    registry, agent_id = _started_restore()

    assert not _end_unfinished_restore(registry, agent_id, {"status": "ok"})

    record = registry.get(agent_id)
    assert record is not None
    assert record.status == WorkspaceOperationStatus.RUNNING


@pytest.mark.parametrize("workspace_version_ref", ["minds-v0.3.9", "minds-v0.2.0"])
def test_backup_service_update_refuses_a_machine_below_the_in_place_floor(
    tmp_path: Path, workspace_version_ref: str
) -> None:
    """A machine too old for the running release's backup service is refused, untouched.

    Its volume layout predates the one that service reads the backup root out
    of, so installing it would leave every backup failing. Nothing here can
    reach a machine, so a run that got as far as probing one would fail with a
    different error rather than pass.
    """
    paths = InstallationPaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    registry = InMemoryWorkspaceOperationRegistry()
    assert registry.start_if_idle(agent_id, WorkspaceOperationKind.BACKUP_UPDATE, datetime.now(timezone.utc), None)

    run_backup_update_sequence(
        agent_id=agent_id,
        paths=paths,
        resolver=StaticBackendResolver(url_by_agent_and_service={}),
        facts_cache=WorkspaceFactsCache(records_dir=tmp_path / "workspace_facts_cache"),
        registry=registry,
        parent_cg=None,
        is_stop_chats=False,
        workspace_version_ref=workspace_version_ref,
    )

    record = registry.get(agent_id)
    assert record is not None
    assert record.status == WorkspaceOperationStatus.FAILED
    assert record.error == BELOW_UPDATE_FLOOR_MESSAGE
    assert record.failure_reason == OperationFailureReason.BELOW_UPDATE_FLOOR
    # Refused before the gate probe, so nothing about the machine was read.
    assert record.is_mutating is False


def test_the_mutating_apply_step_refuses_a_machine_below_the_in_place_floor(tmp_path: Path) -> None:
    """The floor is enforced where the mutation happens, so the restore's chained update honours it.

    That chained update does not go through the phase runner the test above
    drives, so this guard is the only thing standing between a restore and a
    backup service the machine cannot run.
    """
    paths = InstallationPaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    registry = InMemoryWorkspaceOperationRegistry()
    assert registry.start_if_idle(agent_id, WorkspaceOperationKind.BACKUP_UPDATE, datetime.now(timezone.utc), None)

    refusal = _apply_update_and_verify(
        agent_id=agent_id,
        paths=paths,
        resolver=StaticBackendResolver(url_by_agent_and_service={}),
        facts_cache=WorkspaceFactsCache(records_dir=tmp_path / "workspace_facts_cache"),
        registry=registry,
        parent_cg=None,
        is_stop_chats=False,
        workspace_version_ref="minds-v0.3.9",
    )

    # The exact message, not merely "some error": nothing here can reach a
    # machine, so a run that got as far as the apply script would also return
    # non-None -- with restic's or mngr's words rather than the refusal.
    assert refusal == BackupOperationFailure(
        detail=BELOW_UPDATE_FLOOR_MESSAGE, reason=OperationFailureReason.BELOW_UPDATE_FLOOR
    )
    assert registry.log_lines_by_agent_id[agent_id] == []


def test_the_restore_does_not_offer_a_retry_for_an_update_it_will_always_refuse() -> None:
    """The below-floor refusal is not a failure to retry, and Settings hides the button for it.

    A restore of such a machine still restores the data and reports the skipped
    update, so this warning is what its user actually reads.
    """
    refused = _chained_update_warning(
        BackupOperationFailure(detail=BELOW_UPDATE_FLOOR_MESSAGE, reason=OperationFailureReason.BELOW_UPDATE_FLOOR)
    )
    assert BELOW_UPDATE_FLOOR_MESSAGE in refused.warning
    assert "Update backup software" not in refused.warning
    assert refused.is_update_suggested is False

    failed = _chained_update_warning(
        BackupOperationFailure(detail="restic snapshots timed out after 90s", reason=None)
    )
    assert "restic snapshots timed out after 90s" in failed.warning
    assert failed.is_update_suggested is True


class _OverQuotaImbueCloudCli(ImbueCloudCli):
    """Refuses every repository create the way the connector refuses one over the plan's quota."""

    def create_backup_repository(self, *, account: str, workspace_id: str) -> BackupRepositoryCredentials:
        raise ImbueCloudQuotaExceededCliError("backup repository create: storage quota exceeded")


def test_a_backup_setup_refused_by_the_plan_quota_fails_with_that_reason(tmp_path: Path) -> None:
    # The UI words this as the plan having no room (not as a bug to report),
    # the same way the creation-time failure of the same cause is worded.
    registry = InMemoryWorkspaceOperationRegistry()
    agent_id = AgentId.generate()
    registry.start(agent_id, WorkspaceOperationKind.BACKUP_CONFIGURE, datetime.now(timezone.utc))

    run_backup_configure_sequence(
        agent_id=agent_id,
        agent_address="unreachable",
        request=BackupSetupRequest(backup_provider=BackupProvider.IMBUE_CLOUD, account_email="a@b.com"),
        imbue_cloud_cli=_OverQuotaImbueCloudCli(connector_url=AnyUrl("http://connector.invalid")),
        paths=InstallationPaths(data_dir=tmp_path),
        parent_cg=None,
        registry=registry,
        is_destination_change=False,
        quota_evictor=lambda: False,
    )

    record = registry.get(agent_id)
    assert record is not None
    assert record.status == WorkspaceOperationStatus.FAILED
    assert record.failure_reason == OperationFailureReason.STORAGE_QUOTA_EXCEEDED
    assert record.error == "backup repository create: storage quota exceeded"


def test_restore_program_arguments_name_the_previous_storage_env_only_for_an_archived_restore() -> None:
    agent_id = AgentId.generate()
    at = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
    current = build_restore_program_arguments(
        agent_id=agent_id,
        snapshot_id="abcdef12",
        snapshot_subpath="/home/user",
        snapshot_time=at,
        is_from_previous_storage=False,
        is_stop_chats=False,
        is_skip_chat_gate=False,
        is_skip_safety_snapshot=False,
    )
    assert current == (
        "--agent-id",
        str(agent_id),
        "--snapshot-id",
        "abcdef12",
        "--snapshot-subpath",
        "/home/user",
        "--source-time",
        at.isoformat(),
    )

    previous = build_restore_program_arguments(
        agent_id=agent_id,
        snapshot_id="abcdef12",
        snapshot_subpath="/home/user",
        snapshot_time=at,
        is_from_previous_storage=True,
        is_stop_chats=True,
        is_skip_chat_gate=True,
        is_skip_safety_snapshot=True,
    )
    assert previous[8:] == (
        "--source-env-path",
        RESTIC_PREVIOUS_ENV_REMOTE_PATH,
        "--stop-chats",
        "--skip-chat-gate",
        "--skip-safety-snapshot",
    )
