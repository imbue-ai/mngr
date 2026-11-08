from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path

from pydantic import AnyUrl

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.backup_env_store import archive_canonical_env
from imbue.minds.desktop_client.backup_env_store import read_canonical_env
from imbue.minds.desktop_client.backup_env_store import write_canonical_env
from imbue.minds.desktop_client.backup_store_migration import BackupStoreMigrationManager
from imbue.minds.desktop_client.backup_store_migration import MAX_MIGRATIONS_PER_PASS
from imbue.minds.desktop_client.backup_store_migration import is_legacy_backup_env
from imbue.minds.desktop_client.backup_store_migration import run_backup_store_migration
from imbue.minds.desktop_client.conftest import FAKE_CONNECTOR_URL
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.imbue_cloud_cli import BackupRepositoryInfo
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.testing import FAKE_BACKUP_USER_ID
from imbue.minds.desktop_client.testing import FAKE_BACKUP_USER_SEGMENT
from imbue.minds.desktop_client.testing import OneMachineBackendResolver
from imbue.minds.desktop_client.testing import legacy_restic_env
from imbue.minds.desktop_client.testing import store_repository_url
from imbue.minds.desktop_client.testing import store_restic_env
from imbue.minds.desktop_client.workspace_operations import InMemoryWorkspaceOperationRegistry
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationKind
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationStatus
from imbue.minds.desktop_client.workspace_record_store import RECORD_STATE_ACTIVE
from imbue.minds.desktop_client.workspace_record_store import ReplicaRecord
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.utils.polling import poll_for_value

_OWNER_PREFIX = FAKE_BACKUP_USER_SEGMENT[:16]
_FOREIGN_OWNER_PREFIX = "fedcba9876543210"


def _legacy_env(workspace: object, owner_prefix: str = _OWNER_PREFIX) -> str:
    return legacy_restic_env(f"{owner_prefix}--{workspace}")


def test_is_legacy_backup_env_matches_only_the_accounts_own_r2_buckets() -> None:
    workspace = "agent-" + "a" * 32
    assert is_legacy_backup_env(_legacy_env(workspace), _OWNER_PREFIX) is True
    assert is_legacy_backup_env(store_restic_env(str(workspace)), _OWNER_PREFIX) is False
    assert is_legacy_backup_env("RESTIC_REPOSITORY=s3:https://s3.amazonaws.com/mine\n", _OWNER_PREFIX) is False
    # A user's own R2 bucket over S3 (a bring-your-own backend) is not ours to move.
    assert is_legacy_backup_env(_legacy_env(workspace, _FOREIGN_OWNER_PREFIX), _OWNER_PREFIX) is False
    assert (
        is_legacy_backup_env("RESTIC_REPOSITORY=s3:https://acct.r2.cloudflarestorage.com/mine\n", _OWNER_PREFIX)
        is False
    )


class _RecordingChangeDestination:
    """Stands in for the destination change: records the call and writes a store env as the real one would."""

    def __init__(self, *, paths: InstallationPaths, error: Exception | None = None) -> None:
        self.paths = paths
        self.error = error
        self.calls: list[tuple[str, str]] = []
        self.is_rolled_back_on_failure: bool | None = None

    def __call__(self, *, agent_id: AgentId, agent_address: str, request: object, **kwargs: object) -> None:
        self.calls.append((str(agent_id), agent_address))
        self.is_rolled_back_on_failure = bool(kwargs.get("is_rolled_back_on_failure", False))
        if self.error is not None:
            raise self.error
        write_canonical_env(self.paths, agent_id, store_restic_env(str(agent_id)))


def _run_migration(
    tmp_path: Path, *, change_error: Exception | None = None, freeze_error: ImbueCloudCliError | None = None
) -> tuple[FakeImbueCloudCli, _RecordingChangeDestination, InMemoryWorkspaceOperationRegistry, AgentId]:
    """Run one direct migration of a registered workspace against the recording change and the fake CLI."""
    paths = InstallationPaths(data_dir=tmp_path)
    agent_id = AgentId()
    cli = FakeImbueCloudCli(connector_url=AnyUrl(FAKE_CONNECTOR_URL), freeze_error_to_raise=freeze_error)
    registry = InMemoryWorkspaceOperationRegistry()
    registry.start(agent_id, WorkspaceOperationKind.BACKUP_CONFIGURE, datetime.now(timezone.utc))
    change = _RecordingChangeDestination(paths=paths, error=change_error)
    run_backup_store_migration(
        agent_id=agent_id,
        agent_address=f"{agent_id}@host",
        account_email="a@b.com",
        legacy_env_content=_legacy_env(agent_id),
        paths=paths,
        imbue_cloud_cli=cli,
        registry=registry,
        parent_cg=None,
        quota_evictor=None,
        change_destination_fn=change,
    )
    return cli, change, registry, agent_id


def test_run_migration_changes_destination_then_freezes_the_legacy_bucket(tmp_path: Path) -> None:
    cli, change, registry, agent_id = _run_migration(tmp_path)

    assert change.calls == [(str(agent_id), f"{agent_id}@host")]
    assert cli.frozen_buckets == [str(agent_id)]
    record = registry.get(agent_id)
    assert record is not None and record.status == WorkspaceOperationStatus.DONE


def test_run_migration_fails_the_operation_and_freezes_nothing_when_provisioning_fails(tmp_path: Path) -> None:
    cli, change, registry, agent_id = _run_migration(tmp_path, change_error=ImbueCloudCliError("store unavailable"))

    assert cli.frozen_buckets == []
    # The change must undo a half-applied switch, or the next pass skips the workspace.
    assert change.is_rolled_back_on_failure is True
    record = registry.get(agent_id)
    assert record is not None and record.status == WorkspaceOperationStatus.FAILED
    assert "store unavailable" in (record.error or "")


def test_run_migration_completes_when_the_legacy_bucket_cannot_be_frozen(tmp_path: Path) -> None:
    # The backups already go to the store; the legacy reap freezes or deletes
    # the bucket later, so a failed freeze is not a failed migration.
    cli, _change, registry, agent_id = _run_migration(tmp_path, freeze_error=ImbueCloudCliError("cloudflare down"))

    assert cli.frozen_buckets == [str(agent_id)]
    record = registry.get(agent_id)
    assert record is not None and record.status == WorkspaceOperationStatus.DONE


def _active_record(agent_id: AgentId, host_id: HostId) -> ReplicaRecord:
    return ReplicaRecord(host_id=str(host_id), agent_id=str(agent_id), display_name="ws", state=RECORD_STATE_ACTIVE)


def _wait_for_terminal_status(
    registry: InMemoryWorkspaceOperationRegistry, agent_id: AgentId
) -> WorkspaceOperationStatus:
    def _status() -> WorkspaceOperationStatus | None:
        record = registry.get(agent_id)
        if record is None or record.status == WorkspaceOperationStatus.RUNNING:
            return None
        return record.status

    status, _poll_count, _elapsed = poll_for_value(_status, timeout=10.0, poll_interval=0.01)
    assert status is not None, "the migration never ended"
    return status


class _MigrationHarness:
    """One signed-in account with active workspace records, their canonical envs, and a running host.

    ``legacy_ids`` and ``store_ids`` are online (the resolver knows them);
    ``offline_ids`` have a legacy env but no host the resolver can reach.
    """

    def __init__(
        self,
        tmp_path: Path,
        *,
        legacy_ids: list[AgentId],
        store_ids: list[AgentId],
        offline_ids: list[AgentId],
        foreign_r2_ids: Sequence[AgentId] = (),
    ) -> None:
        self.cli = FakeImbueCloudCli(connector_url=AnyUrl(FAKE_CONNECTOR_URL))
        self.cli.add_account(user_id=FAKE_BACKUP_USER_ID, email="a@b.com")
        self.session_store = make_session_store_for_test(tmp_path, cli=self.cli)
        record_store = self.session_store.record_store
        assert record_store is not None
        self.record_store = record_store
        self.paths = record_store.paths
        host_id = HostId()
        for agent_id in [*legacy_ids, *store_ids, *offline_ids, *foreign_r2_ids]:
            record_store.upsert_local_record(FAKE_BACKUP_USER_ID, "a@b.com", _active_record(agent_id, host_id))
        for agent_id in [*legacy_ids, *offline_ids]:
            write_canonical_env(self.paths, agent_id, _legacy_env(agent_id))
        for agent_id in store_ids:
            write_canonical_env(self.paths, agent_id, store_restic_env(str(agent_id)))
        for agent_id in foreign_r2_ids:
            write_canonical_env(self.paths, agent_id, _legacy_env(agent_id, _FOREIGN_OWNER_PREFIX))
        self.resolver = OneMachineBackendResolver(
            host_id=host_id,
            url_by_agent_and_service={str(agent_id): {} for agent_id in [*legacy_ids, *store_ids, *foreign_r2_ids]},
        )
        self.resolver.set_host_state(HostState.RUNNING)
        self.registry = InMemoryWorkspaceOperationRegistry()

    def start(
        self, manager: BackupStoreMigrationManager, agent_ids: list[AgentId], parent_cg: ConcurrencyGroup
    ) -> list[AgentId]:
        return manager.maybe_start_migrations(
            agent_ids,
            paths=self.paths,
            imbue_cloud_cli=self.cli,
            resolver=self.resolver,
            registry=self.registry,
            session_store=self.session_store,
            record_store=self.record_store,
            parent_cg=parent_cg,
        )


def test_manager_starts_migrations_for_online_legacy_workspaces_only_and_caps_each_pass(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    legacy_ids = [AgentId() for _ in range(MAX_MIGRATIONS_PER_PASS + 1)]
    store_id = AgentId()
    offline_id = AgentId()
    foreign_id = AgentId()
    harness = _MigrationHarness(
        tmp_path, legacy_ids=legacy_ids, store_ids=[store_id], offline_ids=[offline_id], foreign_r2_ids=[foreign_id]
    )
    change = _RecordingChangeDestination(paths=harness.paths)
    manager = BackupStoreMigrationManager(change_destination_fn=change)

    started = harness.start(manager, [store_id, offline_id, foreign_id, *legacy_ids], root_concurrency_group)

    # The store-backed workspace, the one on the user's own R2 bucket, and the
    # one the resolver does not know (offline) are skipped; the legacy ones
    # start, capped per pass.
    assert started == legacy_ids[:MAX_MIGRATIONS_PER_PASS]
    for agent_id in started:
        assert _wait_for_terminal_status(harness.registry, agent_id) == WorkspaceOperationStatus.DONE
        assert not is_legacy_backup_env(read_canonical_env(harness.paths, agent_id) or "", _OWNER_PREFIX)
    assert sorted(harness.cli.frozen_buckets) == sorted(str(agent_id) for agent_id in started)
    for skipped_id in (store_id, offline_id, foreign_id):
        assert harness.registry.get(skipped_id) is None
    assert read_canonical_env(harness.paths, foreign_id) == _legacy_env(foreign_id, _FOREIGN_OWNER_PREFIX)

    # A second pass picks up the remaining legacy workspace and never re-runs a finished one.
    second = harness.start(manager, legacy_ids, root_concurrency_group)
    assert second == legacy_ids[MAX_MIGRATIONS_PER_PASS:]
    assert _wait_for_terminal_status(harness.registry, second[0]) == WorkspaceOperationStatus.DONE


def _store_repository(agent_id: AgentId) -> BackupRepositoryInfo:
    return BackupRepositoryInfo(
        workspace_id=str(agent_id),
        repository_url=store_repository_url(str(agent_id)),
        used_bytes=0,
        created_at="2026-01-01T00:00:00+00:00",
    )


def test_manager_leaves_a_workspace_another_device_already_moved_alone(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    # Two desktops of one account see the same online legacy workspace; the
    # store repository the other device created is the sign to wait for its
    # env to arrive through the record sync instead of minting a second token.
    taken_id = AgentId()
    retried_id = AgentId()
    harness = _MigrationHarness(tmp_path, legacy_ids=[taken_id, retried_id], store_ids=[], offline_ids=[])
    harness.cli.backup_repositories.extend([_store_repository(taken_id), _store_repository(retried_id)])
    # This device's own rolled-back attempt left the store env archived, so that one resumes.
    write_canonical_env(harness.paths, retried_id, store_restic_env(str(retried_id)))
    archive_canonical_env(harness.paths, retried_id, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    write_canonical_env(harness.paths, retried_id, _legacy_env(retried_id))
    change = _RecordingChangeDestination(paths=harness.paths)
    manager = BackupStoreMigrationManager(change_destination_fn=change)

    started = harness.start(manager, [taken_id, retried_id], root_concurrency_group)

    assert started == [retried_id]
    assert _wait_for_terminal_status(harness.registry, retried_id) == WorkspaceOperationStatus.DONE
    assert harness.registry.get(taken_id) is None
    assert read_canonical_env(harness.paths, taken_id) == _legacy_env(taken_id)
    # Not an attempt: once the other device's env lands, the next pass sees a store env and skips it for good.
    harness.cli.backup_repositories = []
    assert harness.start(manager, [taken_id], root_concurrency_group) == [taken_id]


def test_manager_starts_nothing_when_the_store_listing_fails(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    agent_id = AgentId()
    harness = _MigrationHarness(tmp_path, legacy_ids=[agent_id], store_ids=[], offline_ids=[])
    harness.cli.is_backup_repository_listing_failing = True
    manager = BackupStoreMigrationManager(change_destination_fn=_RecordingChangeDestination(paths=harness.paths))

    assert harness.start(manager, [agent_id], root_concurrency_group) == []
    assert harness.registry.get(agent_id) is None
    # A failed listing is an attempt: the workspace waits out the retry interval.
    harness.cli.is_backup_repository_listing_failing = False
    assert harness.start(manager, [agent_id], root_concurrency_group) == []


def test_manager_fails_the_operation_when_the_migration_worker_crashes(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    # An error the migration does not expect must still end the tracked
    # operation, or the workspace refuses every later backup operation.
    agent_id = AgentId()
    harness = _MigrationHarness(tmp_path, legacy_ids=[agent_id], store_ids=[], offline_ids=[])
    manager = BackupStoreMigrationManager(
        change_destination_fn=_RecordingChangeDestination(paths=harness.paths, error=RuntimeError("boom"))
    )

    started = harness.start(manager, [agent_id], root_concurrency_group)

    assert started == [agent_id]
    assert _wait_for_terminal_status(harness.registry, agent_id) == WorkspaceOperationStatus.FAILED
    record = harness.registry.get(agent_id)
    assert record is not None and "boom" in (record.error or "")


def test_manager_does_nothing_without_a_cli_session_store_or_concurrency_group(tmp_path: Path) -> None:
    manager = BackupStoreMigrationManager()
    paths = InstallationPaths(data_dir=tmp_path)
    resolver = OneMachineBackendResolver(host_id=HostId(), url_by_agent_and_service={})
    assert (
        manager.maybe_start_migrations(
            [AgentId()],
            paths=paths,
            imbue_cloud_cli=None,
            resolver=resolver,
            registry=InMemoryWorkspaceOperationRegistry(),
            session_store=None,
            record_store=None,
            parent_cg=None,
        )
        == []
    )
