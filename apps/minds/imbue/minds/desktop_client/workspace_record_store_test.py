import json
from pathlib import Path
from uuid import uuid4

import pytest

from imbue.imbue_common.model_update import to_update
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.backup_env_store import write_canonical_env
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import make_agents_json
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.desktop_client.dek_store import ensure_dek
from imbue.minds.desktop_client.workspace_record_store import RECORD_STATE_ACTIVE
from imbue.minds.desktop_client.workspace_record_store import RECORD_STATE_DESTROYED
from imbue.minds.desktop_client.workspace_record_store import ReplicaRecord
from imbue.minds.desktop_client.workspace_record_store import WorkspaceRecordStore
from imbue.minds.desktop_client.workspace_record_store import collect_ssh_key_material
from imbue.minds.errors import WorkspaceSyncError
from imbue.mngr.primitives import AgentId

_EMAIL = "alice@example.com"


@pytest.fixture
def paths(tmp_path: Path) -> WorkspacePaths:
    return WorkspacePaths(data_dir=tmp_path)


def _make_store(paths: WorkspacePaths, cli: FakeImbueCloudCli | None = None) -> WorkspaceRecordStore:
    return WorkspaceRecordStore(
        paths=paths,
        cli=cli if cli is not None else make_fake_imbue_cloud_cli(),
        device_id="device-test-1",
        device_label="test-laptop",
    )


def _agent_id() -> str:
    return f"agent-{uuid4().hex}"


def _user_id() -> str:
    return uuid4().hex


def test_upsert_local_record_pushes_and_acknowledges(paths: WorkspacePaths) -> None:
    cli = make_fake_imbue_cloud_cli()
    store = _make_store(paths, cli)
    user_id = _user_id()
    record = ReplicaRecord(host_id="host-1", agent_id=_agent_id(), display_name="ws", provider_kind="lima")

    store.upsert_local_record(user_id, _EMAIL, record)

    stored = store.list_records(user_id)
    assert len(stored) == 1
    assert stored[0].revision == 1
    assert not stored[0].is_dirty
    assert cli.sync_records_by_email[_EMAIL]["host-1"]["display_name"] == "ws"


def test_upsert_local_record_queues_when_offline(paths: WorkspacePaths) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.is_sync_offline = True
    store = _make_store(paths, cli)
    user_id = _user_id()
    record = ReplicaRecord(host_id="host-1", agent_id=_agent_id(), display_name="ws", provider_kind="lima")

    store.upsert_local_record(user_id, _EMAIL, record)

    stored = store.list_records(user_id)
    assert stored[0].is_dirty
    assert stored[0].revision == 0

    # Connectivity returns; push_dirty flushes the queue.
    cli.is_sync_offline = False
    store.push_dirty(user_id, _EMAIL)
    assert not store.list_records(user_id)[0].is_dirty
    assert "host-1" in cli.sync_records_by_email[_EMAIL]


def test_push_rebases_once_on_revision_conflict(paths: WorkspacePaths) -> None:
    cli = make_fake_imbue_cloud_cli()
    store = _make_store(paths, cli)
    user_id = _user_id()
    agent_id = _agent_id()
    # Server already at revision 5 (e.g. pushed by another device).
    cli.sync_records_by_email[_EMAIL] = {
        "host-1": ReplicaRecord(host_id="host-1", agent_id=agent_id, display_name="old", provider_kind="lima").to_wire(
            5
        )
    }
    record = ReplicaRecord(host_id="host-1", agent_id=agent_id, display_name="new", provider_kind="lima")

    store.upsert_local_record(user_id, _EMAIL, record)

    assert cli.sync_records_by_email[_EMAIL]["host-1"]["display_name"] == "new"
    assert cli.sync_records_by_email[_EMAIL]["host-1"]["revision"] == 6
    assert store.list_records(user_id)[0].revision == 6


def test_replica_persists_across_store_instances(paths: WorkspacePaths) -> None:
    cli = make_fake_imbue_cloud_cli()
    store = _make_store(paths, cli)
    user_id = _user_id()
    record = ReplicaRecord(host_id="host-1", agent_id=_agent_id(), display_name="ws", provider_kind="lima")
    store.upsert_local_record(user_id, _EMAIL, record)

    reloaded = _make_store(paths, cli)
    assert reloaded.list_records(user_id)[0].display_name == "ws"


def test_pull_merges_server_rows_and_drops_deleted_clean_rows(paths: WorkspacePaths) -> None:
    cli = make_fake_imbue_cloud_cli()
    store = _make_store(paths, cli)
    user_id = _user_id()
    kept = ReplicaRecord(host_id="host-1", agent_id=_agent_id(), display_name="kept", provider_kind="lima")
    dropped = ReplicaRecord(host_id="host-2", agent_id=_agent_id(), display_name="dropped", provider_kind="lima")
    store.upsert_local_record(user_id, _EMAIL, kept)
    store.upsert_local_record(user_id, _EMAIL, dropped)

    # The server loses host-2 (deleted from another device) and gains host-3.
    del cli.sync_records_by_email[_EMAIL]["host-2"]
    remote = ReplicaRecord(
        host_id="host-3", agent_id=_agent_id(), display_name="remote", provider_kind="lima", device_label="desktop"
    )
    cli.sync_records_by_email[_EMAIL]["host-3"] = remote.to_wire(1)

    store.pull(user_id, _EMAIL)

    by_host = {record.host_id: record for record in store.list_records(user_id)}
    assert set(by_host.keys()) == {"host-1", "host-3"}
    assert by_host["host-3"].device_label == "desktop"
    assert not by_host["host-3"].is_dirty


def test_pull_keeps_dirty_local_rows(paths: WorkspacePaths) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.is_sync_offline = True
    store = _make_store(paths, cli)
    user_id = _user_id()
    record = ReplicaRecord(host_id="host-1", agent_id=_agent_id(), display_name="queued", provider_kind="lima")
    store.upsert_local_record(user_id, _EMAIL, record)

    cli.is_sync_offline = False
    store.pull(user_id, _EMAIL)

    assert store.list_records(user_id)[0].display_name == "queued"
    assert store.list_records(user_id)[0].is_dirty


def test_associations_view_reflects_active_records_only(paths: WorkspacePaths) -> None:
    cli = make_fake_imbue_cloud_cli()
    store = _make_store(paths, cli)
    user_id = _user_id()
    active_agent = _agent_id()
    destroyed_agent = _agent_id()
    store.upsert_local_record(
        user_id, _EMAIL, ReplicaRecord(host_id="host-1", agent_id=active_agent, provider_kind="lima")
    )
    store.upsert_local_record(
        user_id,
        _EMAIL,
        ReplicaRecord(host_id="host-2", agent_id=destroyed_agent, provider_kind="lima", state=RECORD_STATE_DESTROYED),
    )

    assert store.associations_view() == {user_id: [active_agent]}
    assert store.find_active_record(active_agent) is not None
    assert store.find_active_record(destroyed_agent) is None


def test_associate_and_disassociate_via_resolver(paths: WorkspacePaths) -> None:
    cli = make_fake_imbue_cloud_cli()
    store = _make_store(paths, cli)
    user_id = _user_id()
    agent_id = AgentId.generate()
    resolver = make_resolver_with_data(agents_json=make_agents_json(agent_id, host_name="my-ws"))

    store.associate_workspace_or_raise(user_id, _EMAIL, str(agent_id), resolver)

    assert store.associations_view() == {user_id: [str(agent_id)]}
    server_rows = cli.sync_records_by_email[_EMAIL]
    assert len(server_rows) == 1

    store.disassociate_workspace_or_raise(user_id, _EMAIL, str(agent_id))
    assert store.associations_view() == {}
    assert cli.sync_records_by_email[_EMAIL] == {}


def test_associate_offline_raises_and_leaves_no_record(paths: WorkspacePaths) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.is_sync_offline = True
    store = _make_store(paths, cli)
    user_id = _user_id()
    agent_id = AgentId.generate()
    resolver = make_resolver_with_data(agents_json=make_agents_json(agent_id, host_name="my-ws"))

    with pytest.raises(WorkspaceSyncError):
        store.associate_workspace_or_raise(user_id, _EMAIL, str(agent_id), resolver)


def test_associate_unknown_workspace_raises(paths: WorkspacePaths) -> None:
    store = _make_store(paths)
    resolver = make_resolver_with_data(agents_json=json.dumps({"agents": []}))

    with pytest.raises(WorkspaceSyncError):
        store.associate_workspace_or_raise(_user_id(), _EMAIL, str(AgentId.generate()), resolver)


def test_associate_while_owned_by_other_account_raises(paths: WorkspacePaths) -> None:
    cli = make_fake_imbue_cloud_cli()
    store = _make_store(paths, cli)
    owner = _user_id()
    other = _user_id()
    agent_id = AgentId.generate()
    resolver = make_resolver_with_data(agents_json=make_agents_json(agent_id, host_name="my-ws"))
    store.associate_workspace_or_raise(owner, _EMAIL, str(agent_id), resolver)

    with pytest.raises(WorkspaceSyncError):
        store.associate_workspace_or_raise(other, "bob@example.com", str(agent_id), resolver)


def test_tombstone_record_keeps_row_and_secrets(paths: WorkspacePaths) -> None:
    cli = make_fake_imbue_cloud_cli()
    store = _make_store(paths, cli)
    user_id = _user_id()
    agent_id = _agent_id()
    store.upsert_local_record(
        user_id,
        _EMAIL,
        ReplicaRecord(host_id="host-1", agent_id=agent_id, provider_kind="lima", encrypted_secrets="c2VjcmV0"),
    )

    store.tombstone_record(user_id, _EMAIL, agent_id)

    records = store.list_records(user_id)
    assert records[0].state == RECORD_STATE_DESTROYED
    assert records[0].encrypted_secrets == "c2VjcmV0"
    assert cli.sync_records_by_email[_EMAIL]["host-1"]["state"] == "destroyed"


def test_build_record_includes_encrypted_restic_env(paths: WorkspacePaths) -> None:
    store = _make_store(paths)
    user_id = _user_id()
    agent_id = AgentId.generate()
    resolver = make_resolver_with_data(agents_json=make_agents_json(agent_id, host_name="my-ws"))
    ensure_dek(paths, user_id)
    env_text = "RESTIC_REPOSITORY=s3:https://r2.example/bucket\nRESTIC_PASSWORD=abc123\n"
    write_canonical_env(paths, agent_id, env_text)

    record = store.build_record_from_resolver(user_id, str(agent_id), resolver)

    assert record is not None
    assert record.backup_kind == "imbue_r2"
    assert record.encrypted_secrets is not None
    payload = store.decrypt_record_secrets(user_id, record)
    assert payload is not None
    assert payload.restic_env == env_text


def test_build_record_without_dek_has_no_secrets(paths: WorkspacePaths) -> None:
    store = _make_store(paths)
    agent_id = AgentId.generate()
    resolver = make_resolver_with_data(agents_json=make_agents_json(agent_id, host_name="my-ws"))
    write_canonical_env(paths, agent_id, "RESTIC_REPOSITORY=x\nRESTIC_PASSWORD=y\n")

    record = store.build_record_from_resolver(_user_id(), str(agent_id), resolver)

    assert record is not None
    assert record.encrypted_secrets is None
    assert record.backup_kind == "imbue_r2"


def test_reconcile_migrates_legacy_associations_and_retires_the_file(paths: WorkspacePaths) -> None:
    cli = make_fake_imbue_cloud_cli()
    store = _make_store(paths, cli)
    user_id = _user_id()
    agent_id = AgentId.generate()
    resolver = make_resolver_with_data(agents_json=make_agents_json(agent_id, host_name="legacy-ws"))
    (paths.data_dir / "workspace_associations.json").write_text(json.dumps({user_id: [str(agent_id)]}))

    store.reconcile({user_id: _EMAIL}, resolver)

    assert store.associations_view() == {user_id: [str(agent_id)]}
    assert not (paths.data_dir / "workspace_associations.json").exists()
    assert (paths.data_dir / "workspace_associations.json.pre-sync").exists()
    assert str(agent_id) in {row["agent_id"] for row in cli.sync_records_by_email[_EMAIL].values()}

    # A second reconcile is a no-op (idempotent).
    store.reconcile({user_id: _EMAIL}, resolver)
    assert len(cli.sync_records_by_email[_EMAIL]) == 1


def test_reconcile_does_not_churn_revisions_without_a_master_password(paths: WorkspacePaths) -> None:
    """Metadata-only tier: pushes strip secrets from the wire, so repeated
    reconciles must not keep 're-adding' them (dirty-pushing a new revision
    every pass without ever converging)."""
    cli = make_fake_imbue_cloud_cli()
    store = _make_store(paths, cli)
    user_id = _user_id()
    agent_id = AgentId.generate()
    resolver = make_resolver_with_data(agents_json=make_agents_json(agent_id, host_name="churn-ws"))
    # Unlocked (DEK exists) but no master password: secrets stay local-only.
    ensure_dek(paths, user_id)
    write_canonical_env(paths, agent_id, "RESTIC_REPOSITORY=s3:x\nRESTIC_PASSWORD=y\n")
    store.associate_workspace_or_raise(user_id, _EMAIL, str(agent_id), resolver)
    host_id = next(iter(cli.sync_records_by_email[_EMAIL]))
    revision_after_associate = cli.sync_records_by_email[_EMAIL][host_id]["revision"]

    store.reconcile({user_id: _EMAIL}, resolver)
    store.reconcile({user_id: _EMAIL}, resolver)

    assert cli.sync_records_by_email[_EMAIL][host_id]["revision"] == revision_after_associate


def test_reconcile_tombstones_definitively_absent_local_rows(paths: WorkspacePaths) -> None:
    cli = make_fake_imbue_cloud_cli()
    store = _make_store(paths, cli)
    user_id = _user_id()
    gone_agent = _agent_id()
    store.upsert_local_record(
        user_id,
        _EMAIL,
        ReplicaRecord(
            host_id="host-gone",
            agent_id=gone_agent,
            provider_kind="local",
            hosting_device_id="device-test-1",
        ),
    )
    # Discovery completed and knows about a different workspace only.
    # make_resolver_with_data runs update_agents, which marks initial discovery complete.
    resolver = make_resolver_with_data(agents_json=make_agents_json(AgentId.generate(), host_name="other"))

    store.reconcile({user_id: _EMAIL}, resolver)

    assert store.list_records(user_id)[0].state == RECORD_STATE_DESTROYED


def test_locked_device_push_preserves_server_secrets(paths: WorkspacePaths) -> None:
    """A locked device (no DEK, no bundle mirror) must pass pulled secrets
    through verbatim when it pushes a metadata change -- stripping them there
    would scrub secrets another device synced."""
    cli = make_fake_imbue_cloud_cli()
    store = _make_store(paths, cli)
    user_id = _user_id()
    remote = ReplicaRecord(
        host_id="host-cloud",
        agent_id=_agent_id(),
        display_name="old-name",
        provider_kind="imbue_cloud_alice",
        hosting_device_id=None,
        device_label="laptop",
        encrypted_secrets="b3BhcXVl",
    )
    cli.sync_records_by_email[_EMAIL] = {"host-cloud": remote.to_wire(1)}
    store.pull(user_id, _EMAIL)

    pulled = store.list_records(user_id)[0]
    renamed = pulled.model_copy_update(to_update(pulled.field_ref().display_name, "new-name"))
    store.upsert_local_record(user_id, _EMAIL, renamed)

    server_row = cli.sync_records_by_email[_EMAIL]["host-cloud"]
    assert server_row["display_name"] == "new-name"
    assert server_row["encrypted_secrets"] == "b3BhcXVl"


def test_reconcile_does_not_tombstone_unenriched_create_seed_rows(paths: WorkspacePaths) -> None:
    """A create-path seed row (empty provider_kind) must survive a reconcile
    that runs before discovery has seen the new workspace -- 'absent from
    discovery' says nothing about a host discovery never enumerated."""
    cli = make_fake_imbue_cloud_cli()
    store = _make_store(paths, cli)
    user_id = _user_id()
    seed = ReplicaRecord(
        host_id="host-just-created",
        agent_id=_agent_id(),
        display_name="brand new",
        provider_kind="",
        hosting_device_id="device-test-1",
        device_label="test-laptop",
    )
    store.upsert_local_record(user_id, _EMAIL, seed)
    # Discovery completed but has not caught up to the new workspace yet.
    resolver = make_resolver_with_data(agents_json=json.dumps({"agents": []}))

    store.reconcile({user_id: _EMAIL}, resolver)

    records = store.list_records(user_id)
    assert len(records) == 1
    assert records[0].state == RECORD_STATE_ACTIVE


def test_reconcile_does_not_tombstone_other_device_rows(paths: WorkspacePaths) -> None:
    cli = make_fake_imbue_cloud_cli()
    store = _make_store(paths, cli)
    user_id = _user_id()
    remote = ReplicaRecord(
        host_id="host-remote",
        agent_id=_agent_id(),
        provider_kind="lima",
        hosting_device_id="some-other-device",
        device_label="desktop",
    )
    cli.sync_records_by_email[_EMAIL] = {"host-remote": remote.to_wire(1)}
    resolver = make_resolver_with_data(agents_json=json.dumps({"agents": []}))

    store.reconcile({user_id: _EMAIL}, resolver)

    records = store.list_records(user_id)
    assert len(records) == 1
    assert records[0].state == RECORD_STATE_ACTIVE


def test_collect_ssh_key_material_finds_per_host_keys(tmp_path: Path) -> None:
    mngr_dir = tmp_path / "mngr"
    profile_dir = mngr_dir / "profiles" / "profile1"
    (mngr_dir / "config.toml").parent.mkdir(parents=True, exist_ok=True)
    (mngr_dir / "config.toml").write_text('profile = "profile1"\n')
    host_dir = profile_dir / "providers" / "imbue_cloud_alice" / "imbue_cloud_alice" / "hosts" / "host-abc"
    host_dir.mkdir(parents=True)
    (host_dir / "ssh_key").write_text("PRIVATE-KEY-BYTES")
    (host_dir / "known_hosts").write_text("[1.2.3.4]:2222 ssh-ed25519 AAAA")

    private_key, known_hosts = collect_ssh_key_material(mngr_dir, "imbue_cloud_alice", "host-abc")

    assert private_key == "PRIVATE-KEY-BYTES"
    assert known_hosts is not None and "ssh-ed25519" in known_hosts


def test_collect_ssh_key_material_returns_none_when_uninitialized(tmp_path: Path) -> None:
    assert collect_ssh_key_material(tmp_path / "missing", "lima", "host-x") == (None, None)
