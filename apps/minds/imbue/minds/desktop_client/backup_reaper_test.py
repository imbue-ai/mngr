from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

from pydantic import AnyUrl
from pydantic import Field

from imbue.minds.desktop_client.backup_env_store import canonical_env_path
from imbue.minds.desktop_client.backup_env_store import write_canonical_env
from imbue.minds.desktop_client.backup_export import _export_in_flight
from imbue.minds.desktop_client.backup_reaper import BackupReaperManager
from imbue.minds.desktop_client.backup_reaper import ReapCandidate
from imbue.minds.desktop_client.backup_reaper import bucket_owner_prefix_from_env
from imbue.minds.desktop_client.backup_reaper import bucket_short_name_from_env
from imbue.minds.desktop_client.backup_reaper import collect_destroyed_candidates
from imbue.minds.desktop_client.backup_reaper import evict_oldest_reapable_backup
from imbue.minds.desktop_client.backup_reaper import list_orphan_env_agent_ids
from imbue.minds.desktop_client.backup_reaper import parse_destroyed_at
from imbue.minds.desktop_client.backup_reaper import user_id_prefix_for
from imbue.minds.desktop_client.conftest import FAKE_CONNECTOR_URL
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.workspace_record_store import RECORD_STATE_DESTROYED
from imbue.minds.desktop_client.workspace_record_store import ReplicaRecord
from imbue.minds.desktop_client.workspace_record_store import WorkspaceRecordStore
from imbue.mngr.primitives import AgentId

_R2_ENV = (
    "RESTIC_REPOSITORY=s3:https://acct.r2.cloudflarestorage.com/{prefix}--{host_id}\n"
    "RESTIC_PASSWORD=pw\nAWS_ACCESS_KEY_ID=k\nAWS_SECRET_ACCESS_KEY=s\n"
)
_BYO_ENV = "RESTIC_REPOSITORY=s3:https://s3.amazonaws.com/my-own-bucket\nRESTIC_PASSWORD=pw\n"

_AGENT_OLD = "agent-" + "a" * 32
_AGENT_NEW = "agent-" + "b" * 32
_AGENT_NOSTAMP = "agent-" + "c" * 32
_AGENT_RECORDED = "agent-" + "d" * 32
_AGENT_ORPHAN = "agent-" + "e" * 32
_AGENT_NEWER = "agent-" + "f" * 32
_AGENT_OLDER = "agent-" + "0" * 32
_AGENT_GONE = "agent-" + "1" * 32
_AGENT_LIVE = "agent-" + "2" * 32


class _RecordingReaperCli(FakeImbueCloudCli):
    """FakeImbueCloudCli that records force-destroys and simulates configurable outcomes."""

    destroyed_names: list[str] = Field(default_factory=list)
    missing_bucket_names: set[str] = Field(default_factory=set)
    failing_bucket_names: set[str] = Field(default_factory=set)

    def destroy_bucket_force(self, account: str, name: str) -> None:
        if name in self.failing_bucket_names:
            error = ImbueCloudCliError("bucket destroy failed")
            error.stderr = '{"error": "CloudflareApiError"}'
            raise error
        if name in self.missing_bucket_names:
            error = ImbueCloudCliError("bucket destroy failed")
            error.stderr = '{"error_class": "R2BucketNotFoundError"}'
            raise error
        self.destroyed_names.append(name)


def _make_reaper_fixture(tmp_path: Path) -> tuple[BackupReaperManager, _RecordingReaperCli, WorkspaceRecordStore]:
    cli = _RecordingReaperCli(connector_url=AnyUrl(FAKE_CONNECTOR_URL))
    session_store = make_session_store_for_test(tmp_path, cli=cli)
    record_store = session_store.record_store
    assert record_store is not None
    manager = BackupReaperManager(
        paths=record_store.paths, record_store=record_store, imbue_cloud_cli=cli, connector_url=""
    )
    return manager, cli, record_store


def _tombstone(
    record_store: WorkspaceRecordStore, user_id: str, host_id: str, agent_id: str, destroyed_days_ago: int
) -> None:
    destroyed_at = (datetime.now(timezone.utc) - timedelta(days=destroyed_days_ago)).isoformat()
    record = ReplicaRecord(
        host_id=host_id,
        agent_id=agent_id,
        display_name=f"ws-{agent_id}",
        state=RECORD_STATE_DESTROYED,
        destroyed_at=destroyed_at,
    )
    record_store.upsert_local_record(user_id, "a@b.com", record)


def test_parse_destroyed_at_handles_iso_z_and_garbage() -> None:
    parsed = parse_destroyed_at("2026-07-01T00:00:00Z")
    assert parsed is not None and parsed.tzinfo is not None
    assert parse_destroyed_at("2026-07-01 00:00:00+00:00") is not None
    assert parse_destroyed_at("not-a-date") is None
    assert parse_destroyed_at(None) is None
    assert parse_destroyed_at("") is None


def test_bucket_name_parsing_from_env_identifies_imbue_cloud_and_byo() -> None:
    env = _R2_ENV.format(prefix="abc123", host_id="host-9c35")
    assert bucket_short_name_from_env(env) == "host-9c35"
    assert bucket_owner_prefix_from_env(env) == "abc123"
    assert bucket_short_name_from_env(_BYO_ENV) is None
    assert bucket_owner_prefix_from_env(_BYO_ENV) is None


def test_user_id_prefix_matches_connector_derivation() -> None:
    assert user_id_prefix_for("12345678-1234-5678-1234-567812345678") == "1234567812345678"


def test_collect_destroyed_candidates_sorts_oldest_first_and_skips_unstamped(tmp_path: Path) -> None:
    _manager, _cli, record_store = _make_reaper_fixture(tmp_path)
    _tombstone(record_store, "user-1", "host-new1", _AGENT_NEW, destroyed_days_ago=1)
    _tombstone(record_store, "user-1", "host-old1", _AGENT_OLD, destroyed_days_ago=40)
    # A tombstone with no stamp is not a candidate yet.
    record_store.upsert_local_record(
        "user-1",
        "a@b.com",
        ReplicaRecord(host_id="host-nostamp", agent_id=_AGENT_NOSTAMP, state=RECORD_STATE_DESTROYED),
    )

    candidates = collect_destroyed_candidates(record_store, {"user-1": "a@b.com"})

    assert [candidate.host_id for candidate in candidates] == ["host-old1", "host-new1"]


def test_reap_candidate_deletes_bucket_record_and_env(tmp_path: Path) -> None:
    manager, cli, record_store = _make_reaper_fixture(tmp_path)
    _tombstone(record_store, "user-1", "host-old1", _AGENT_OLD, destroyed_days_ago=40)
    agent_id = AgentId(_AGENT_OLD)
    write_canonical_env(record_store.paths, agent_id, _R2_ENV.format(prefix="user1", host_id="host-old1"))

    is_reaped = manager.reap_candidate(
        ReapCandidate(
            user_id="user-1",
            account_email="a@b.com",
            agent_id=_AGENT_OLD,
            host_id="host-old1",
            display_name="ws",
            destroyed_at=datetime.now(timezone.utc) - timedelta(days=40),
        ),
        reason="retention_expired",
    )

    assert is_reaped is True
    assert cli.destroyed_names == ["host-old1"]
    assert record_store.list_records("user-1") == []
    assert not canonical_env_path(record_store.paths, agent_id).exists()


def test_reap_candidate_tolerates_already_missing_bucket(tmp_path: Path) -> None:
    manager, cli, record_store = _make_reaper_fixture(tmp_path)
    _tombstone(record_store, "user-1", "host-old1", _AGENT_OLD, destroyed_days_ago=40)
    cli.missing_bucket_names = {"host-old1"}

    is_reaped = manager.reap_candidate(
        ReapCandidate(
            user_id="user-1",
            account_email="a@b.com",
            agent_id=_AGENT_OLD,
            host_id="host-old1",
            display_name="ws",
            destroyed_at=datetime.now(timezone.utc) - timedelta(days=40),
        ),
        reason="retention_expired",
    )

    assert is_reaped is True
    assert record_store.list_records("user-1") == []


def test_reap_candidate_keeps_record_when_bucket_destroy_fails(tmp_path: Path) -> None:
    manager, cli, record_store = _make_reaper_fixture(tmp_path)
    _tombstone(record_store, "user-1", "host-old1", _AGENT_OLD, destroyed_days_ago=40)
    agent_id = AgentId(_AGENT_OLD)
    write_canonical_env(record_store.paths, agent_id, _R2_ENV.format(prefix="user1", host_id="host-old1"))
    cli.failing_bucket_names = {"host-old1"}

    is_reaped = manager.reap_candidate(
        ReapCandidate(
            user_id="user-1",
            account_email="a@b.com",
            agent_id=_AGENT_OLD,
            host_id="host-old1",
            display_name="ws",
            destroyed_at=datetime.now(timezone.utc) - timedelta(days=40),
        ),
        reason="retention_expired",
    )

    # Strict order: a failed bucket destroy leaves the record and env for retry.
    assert is_reaped is False
    assert len(record_store.list_records("user-1")) == 1
    assert canonical_env_path(record_store.paths, agent_id).exists()


def test_reap_candidate_skips_bucket_for_byo_backend(tmp_path: Path) -> None:
    manager, cli, record_store = _make_reaper_fixture(tmp_path)
    _tombstone(record_store, "user-1", "host-old1", _AGENT_OLD, destroyed_days_ago=40)
    agent_id = AgentId(_AGENT_OLD)
    write_canonical_env(record_store.paths, agent_id, _BYO_ENV)

    is_reaped = manager.reap_candidate(
        ReapCandidate(
            user_id="user-1",
            account_email="a@b.com",
            agent_id=_AGENT_OLD,
            host_id="host-old1",
            display_name="ws",
            destroyed_at=datetime.now(timezone.utc) - timedelta(days=40),
        ),
        reason="retention_expired",
    )

    # BYO repositories are never ours to delete: only the record + env go.
    assert is_reaped is True
    assert cli.destroyed_names == []
    assert record_store.list_records("user-1") == []
    assert not canonical_env_path(record_store.paths, agent_id).exists()


def test_list_orphan_env_agent_ids_excludes_recorded_agents(tmp_path: Path) -> None:
    _manager, _cli, record_store = _make_reaper_fixture(tmp_path)
    _tombstone(record_store, "user-1", "host-a", _AGENT_RECORDED, destroyed_days_ago=1)
    write_canonical_env(record_store.paths, AgentId(_AGENT_RECORDED), _BYO_ENV)
    write_canonical_env(record_store.paths, AgentId(_AGENT_ORPHAN), _BYO_ENV)

    orphans = list_orphan_env_agent_ids(record_store.paths, record_store)

    assert [str(agent_id) for agent_id in orphans] == [_AGENT_ORPHAN]


def test_evict_oldest_reapable_backup_destroys_oldest_first(tmp_path: Path) -> None:
    _manager, cli, record_store = _make_reaper_fixture(tmp_path)
    _tombstone(record_store, "user-1", "host-newer", _AGENT_NEWER, destroyed_days_ago=2)
    _tombstone(record_store, "user-1", "host-older", _AGENT_OLDER, destroyed_days_ago=10)

    is_evicted = evict_oldest_reapable_backup(
        record_store=record_store,
        paths=record_store.paths,
        imbue_cloud_cli=cli,
        user_id="user-1",
        account_email="a@b.com",
    )

    assert is_evicted is True
    assert cli.destroyed_names == ["host-older"]
    remaining = record_store.list_records("user-1")
    assert [record.host_id for record in remaining] == ["host-newer"]


def test_evict_oldest_reapable_backup_returns_false_when_nothing_left(tmp_path: Path) -> None:
    _manager, cli, record_store = _make_reaper_fixture(tmp_path)
    assert (
        evict_oldest_reapable_backup(
            record_store=record_store,
            paths=record_store.paths,
            imbue_cloud_cli=cli,
            user_id="user-1",
            account_email="a@b.com",
        )
        is False
    )


def test_evict_skips_missing_buckets_and_keeps_looking(tmp_path: Path) -> None:
    _manager, cli, record_store = _make_reaper_fixture(tmp_path)
    _tombstone(record_store, "user-1", "host-gone01", _AGENT_GONE, destroyed_days_ago=10)
    _tombstone(record_store, "user-1", "host-live01", _AGENT_LIVE, destroyed_days_ago=5)
    cli.missing_bucket_names = {"host-gone01"}

    is_evicted = evict_oldest_reapable_backup(
        record_store=record_store,
        paths=record_store.paths,
        imbue_cloud_cli=cli,
        user_id="user-1",
        account_email="a@b.com",
    )

    # The already-gone bucket frees no quota, so its leftovers are cleaned and
    # the next candidate's bucket is what gets evicted.
    assert is_evicted is True
    assert cli.destroyed_names == ["host-live01"]
    assert [record.host_id for record in record_store.list_records("user-1")] == []


def test_evict_skips_candidates_with_export_in_flight(tmp_path: Path) -> None:
    _manager, cli, record_store = _make_reaper_fixture(tmp_path)
    _tombstone(record_store, "user-1", "host-older", _AGENT_OLDER, destroyed_days_ago=10)
    _tombstone(record_store, "user-1", "host-newer", _AGENT_NEWER, destroyed_days_ago=2)

    # The oldest candidate's backup is mid-download: eviction must not destroy
    # its bucket out from under the export, so the next-oldest is evicted.
    with _export_in_flight(AgentId(_AGENT_OLDER)):
        is_evicted = evict_oldest_reapable_backup(
            record_store=record_store,
            paths=record_store.paths,
            imbue_cloud_cli=cli,
            user_id="user-1",
            account_email="a@b.com",
        )

    assert is_evicted is True
    assert cli.destroyed_names == ["host-newer"]
    assert [record.host_id for record in record_store.list_records("user-1")] == ["host-older"]


def test_manager_retention_falls_back_without_connector(tmp_path: Path) -> None:
    manager, _cli, _record_store = _make_reaper_fixture(tmp_path)
    assert manager.get_retention_seconds() == 60.0 * 60.0 * 24.0 * 30.0


def test_cached_retention_never_fetches_and_falls_back(tmp_path: Path) -> None:
    """The no-network accessor returns the fallback without a connector fetch.

    Even with a connector configured, an empty cache yields the 30-day fallback
    rather than triggering a fetch -- the destroyed-workspaces page shell relies
    on this to paint without blocking on a (up to 10-second) connector round-trip.
    """
    _manager, cli, record_store = _make_reaper_fixture(tmp_path)
    manager_with_connector = BackupReaperManager(
        paths=record_store.paths,
        record_store=record_store,
        imbue_cloud_cli=cli,
        connector_url=str(FAKE_CONNECTOR_URL),
    )
    assert manager_with_connector.get_cached_retention_seconds() == 60.0 * 60.0 * 24.0 * 30.0
