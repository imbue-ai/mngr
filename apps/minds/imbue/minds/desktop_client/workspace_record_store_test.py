import json
from base64 import b64decode
from base64 import b64encode
from datetime import datetime
from datetime import timezone
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.asymmetric import rsa

from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.secret_wrapping import decrypt_secrets
from imbue.imbue_common.secret_wrapping import encrypt_secrets
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backup_env_store import write_canonical_env
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import make_agents_json
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.desktop_client.conftest import seed_provider_snapshots
from imbue.minds.desktop_client.dek_store import ensure_dek
from imbue.minds.desktop_client.dek_store import load_dek
from imbue.minds.desktop_client.testing import device_id_for_test
from imbue.minds.desktop_client.workspace_record_store import RECORD_STATE_ACTIVE
from imbue.minds.desktop_client.workspace_record_store import RECORD_STATE_DESTROYED
from imbue.minds.desktop_client.workspace_record_store import ReplicaRecord
from imbue.minds.desktop_client.workspace_record_store import WorkspaceRecordStore
from imbue.minds.desktop_client.workspace_record_store import WorkspaceSecretsPayload
from imbue.minds.desktop_client.workspace_record_store import collect_ssh_key_material
from imbue.minds.desktop_client.workspace_record_store import derive_openssh_public_key_line
from imbue.minds.desktop_client.workspace_record_store import replica_record_from_wire
from imbue.minds.errors import WorkspaceRecordTooNewError
from imbue.minds.errors import WorkspaceSyncError
from imbue.mngr.api.discovery_events import DiscoveryError
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.host_key_store import HostKeyOrigin
from imbue.mngr.providers.host_key_store import pin_host_key

_EMAIL = "alice@example.com"


@pytest.fixture
def paths(tmp_path: Path) -> InstallationPaths:
    return InstallationPaths(data_dir=tmp_path)


def _make_store(paths: InstallationPaths, cli: FakeImbueCloudCli | None = None) -> WorkspaceRecordStore:
    return WorkspaceRecordStore(
        paths=paths,
        cli=cli if cli is not None else make_fake_imbue_cloud_cli(),
        device_id=device_id_for_test("store-test-1"),
        device_label="test-laptop",
    )


def _agent_id() -> str:
    return f"agent-{uuid4().hex}"


def _user_id() -> str:
    return uuid4().hex


def test_upsert_local_record_pushes_and_acknowledges(paths: InstallationPaths) -> None:
    cli = make_fake_imbue_cloud_cli()
    store = _make_store(paths, cli)
    user_id = _user_id()
    record = ReplicaRecord(host_id="host-1", agent_id=_agent_id(), display_name="ws", provider_kind="lima")

    store.upsert_local_record(user_id, _EMAIL, record)

    stored = store.list_records(user_id)
    assert len(stored) == 1
    assert stored[0].revision == 1
    assert not stored[0].is_dirty
    assert cli.sync_records_by_email[_EMAIL][record.agent_id]["display_name"] == "ws"


def test_upsert_local_record_queues_when_offline(paths: InstallationPaths) -> None:
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
    assert record.agent_id in cli.sync_records_by_email[_EMAIL]


def test_push_rebases_once_on_revision_conflict(paths: InstallationPaths) -> None:
    cli = make_fake_imbue_cloud_cli()
    store = _make_store(paths, cli)
    user_id = _user_id()
    agent_id = _agent_id()
    # Server already at revision 5 (e.g. pushed by another device).
    cli.sync_records_by_email[_EMAIL] = {
        agent_id: ReplicaRecord(host_id="host-1", agent_id=agent_id, display_name="old", provider_kind="lima").to_wire(
            5
        )
    }
    record = ReplicaRecord(host_id="host-1", agent_id=agent_id, display_name="new", provider_kind="lima")

    store.upsert_local_record(user_id, _EMAIL, record)

    assert cli.sync_records_by_email[_EMAIL][agent_id]["display_name"] == "new"
    assert cli.sync_records_by_email[_EMAIL][agent_id]["revision"] == 6
    assert store.list_records(user_id)[0].revision == 6


def test_replica_persists_across_store_instances(paths: InstallationPaths) -> None:
    cli = make_fake_imbue_cloud_cli()
    store = _make_store(paths, cli)
    user_id = _user_id()
    record = ReplicaRecord(host_id="host-1", agent_id=_agent_id(), display_name="ws", provider_kind="lima")
    store.upsert_local_record(user_id, _EMAIL, record)

    reloaded = _make_store(paths, cli)
    assert reloaded.list_records(user_id)[0].display_name == "ws"


def test_replica_load_keeps_the_active_row_when_a_legacy_file_duplicates_a_workspace(
    paths: InstallationPaths,
) -> None:
    # A legacy host-keyed replica can hold two rows for one workspace after a
    # machine move: the ACTIVE new-host row and a tombstoned old-host row. The
    # ACTIVE row must win regardless of file order.
    user_id = _user_id()
    agent_id = _agent_id()
    active = ReplicaRecord(host_id="host-new", agent_id=agent_id, display_name="ws", provider_kind="lima")
    tombstone = ReplicaRecord(
        host_id="host-old", agent_id=agent_id, display_name="ws", provider_kind="lima", state=RECORD_STATE_DESTROYED
    )
    records_dir = paths.data_dir / "workspace_records"
    records_dir.mkdir(parents=True)
    for ordering in ([active, tombstone], [tombstone, active]):
        (records_dir / f"{user_id}.json").write_text(
            json.dumps({"records": [record.model_dump(mode="json") for record in ordering]})
        )
        store = _make_store(paths)
        loaded = store.list_records(user_id)
        assert len(loaded) == 1
        assert loaded[0].state == RECORD_STATE_ACTIVE
        assert loaded[0].host_id == "host-new"


def test_pull_merges_server_rows_and_drops_deleted_clean_rows(paths: InstallationPaths) -> None:
    cli = make_fake_imbue_cloud_cli()
    store = _make_store(paths, cli)
    user_id = _user_id()
    kept = ReplicaRecord(host_id="host-1", agent_id=_agent_id(), display_name="kept", provider_kind="lima")
    dropped = ReplicaRecord(host_id="host-2", agent_id=_agent_id(), display_name="dropped", provider_kind="lima")
    store.upsert_local_record(user_id, _EMAIL, kept)
    store.upsert_local_record(user_id, _EMAIL, dropped)

    # The server loses host-2's workspace (deleted from another device) and gains host-3's.
    del cli.sync_records_by_email[_EMAIL][dropped.agent_id]
    remote = ReplicaRecord(
        host_id="host-3", agent_id=_agent_id(), display_name="remote", provider_kind="lima", device_label="desktop"
    )
    cli.sync_records_by_email[_EMAIL][remote.agent_id] = remote.to_wire(1)

    store.pull(user_id, _EMAIL)

    by_host = {record.host_id: record for record in store.list_records(user_id)}
    assert set(by_host.keys()) == {"host-1", "host-3"}
    assert by_host["host-3"].device_label == "desktop"
    assert not by_host["host-3"].is_dirty


def test_pull_keeps_dirty_local_rows(paths: InstallationPaths) -> None:
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


@pytest.mark.witnesses(
    "remote-compatibility.newer-records-read-only", partial="covers pull adoption of newer rows only"
)
def test_pull_adopts_a_newer_format_server_row_over_dirty_local_changes(paths: InstallationPaths) -> None:
    """A dirty row whose server counterpart is newer-format must not wedge: the server row wins.

    This app can never push the pending local change (the connector's
    terminal record_format_too_new refusal), so keeping the row dirty would
    block every future pull of it and the read-only gates would never engage.
    """
    cli = make_fake_imbue_cloud_cli()
    cli.is_sync_offline = True
    store = _make_store(paths, cli)
    user_id = _user_id()
    agent_id = _agent_id()
    record = ReplicaRecord(host_id="host-1", agent_id=agent_id, display_name="queued", provider_kind="lima")
    store.upsert_local_record(user_id, _EMAIL, record)
    assert store.list_records(user_id)[0].is_dirty

    cli.is_sync_offline = False
    cli.sync_records_by_email[_EMAIL] = {
        agent_id: {
            "host_id": "host-1",
            "agent_id": agent_id,
            "display_name": "renamed by a newer client",
            "provider_kind": "lima",
            "state": RECORD_STATE_ACTIVE,
            "revision": 7,
            "record_format": 2,
        }
    }

    assert store.pull(user_id, _EMAIL) is True

    (pulled,) = store.list_records(user_id)
    assert pulled.record_format == 2
    assert pulled.display_name == "renamed by a newer client"
    assert not pulled.is_dirty


def test_associations_view_reflects_active_records_only(paths: InstallationPaths) -> None:
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


def test_associate_and_disassociate_via_resolver(paths: InstallationPaths) -> None:
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


def test_associate_offline_raises_and_leaves_no_record(paths: InstallationPaths) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.is_sync_offline = True
    store = _make_store(paths, cli)
    user_id = _user_id()
    agent_id = AgentId.generate()
    resolver = make_resolver_with_data(agents_json=make_agents_json(agent_id, host_name="my-ws"))

    with pytest.raises(WorkspaceSyncError):
        store.associate_workspace_or_raise(user_id, _EMAIL, str(agent_id), resolver)


def test_associate_unknown_workspace_raises(paths: InstallationPaths) -> None:
    store = _make_store(paths)
    resolver = make_resolver_with_data(agents_json=json.dumps({"agents": []}))

    with pytest.raises(WorkspaceSyncError):
        store.associate_workspace_or_raise(_user_id(), _EMAIL, str(AgentId.generate()), resolver)


def test_associate_while_owned_by_other_account_raises(paths: InstallationPaths) -> None:
    cli = make_fake_imbue_cloud_cli()
    store = _make_store(paths, cli)
    owner = _user_id()
    other = _user_id()
    agent_id = AgentId.generate()
    resolver = make_resolver_with_data(agents_json=make_agents_json(agent_id, host_name="my-ws"))
    store.associate_workspace_or_raise(owner, _EMAIL, str(agent_id), resolver)

    with pytest.raises(WorkspaceSyncError):
        store.associate_workspace_or_raise(other, "bob@example.com", str(agent_id), resolver)


def test_tombstone_record_keeps_row_and_secrets(paths: InstallationPaths) -> None:
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
    assert cli.sync_records_by_email[_EMAIL][agent_id]["state"] == "destroyed"


def test_build_record_includes_encrypted_restic_env(paths: InstallationPaths) -> None:
    store = _make_store(paths)
    user_id = _user_id()
    agent_id = AgentId.generate()
    resolver = make_resolver_with_data(agents_json=make_agents_json(agent_id, host_name="my-ws"))
    ensure_dek(paths, user_id)
    env_text = "RESTIC_REPOSITORY=s3:https://r2.example/bucket\nRESTIC_PASSWORD=abc123\n"
    write_canonical_env(paths, agent_id, env_text)

    record = store.build_record_from_resolver(user_id, str(agent_id), resolver)

    assert record is not None
    assert record.encrypted_secrets is not None
    payload = store.decrypt_record_secrets(user_id, record)
    assert payload is not None
    assert payload.restic_env == env_text


def test_build_record_stamps_backup_bucket_from_canonical_env(paths: InstallationPaths) -> None:
    store = _make_store(paths)
    user_id = _user_id()
    agent_id = AgentId.generate()
    resolver = make_resolver_with_data(agents_json=make_agents_json(agent_id, host_name="my-ws"))
    write_canonical_env(
        paths,
        agent_id,
        f"RESTIC_REPOSITORY=s3:https://acct.r2.cloudflarestorage.com/abc123--{agent_id}\nRESTIC_PASSWORD=pw\n",
    )

    record = store.build_record_from_resolver(user_id, str(agent_id), resolver)

    assert record is not None
    assert record.backup_bucket == f"abc123--{agent_id}"


def test_build_record_without_canonical_env_has_no_backup_bucket(paths: InstallationPaths) -> None:
    store = _make_store(paths)
    agent_id = AgentId.generate()
    resolver = make_resolver_with_data(agents_json=make_agents_json(agent_id, host_name="my-ws"))

    record = store.build_record_from_resolver(_user_id(), str(agent_id), resolver)

    assert record is not None
    assert record.backup_bucket is None


def test_build_record_without_dek_has_no_secrets(paths: InstallationPaths) -> None:
    store = _make_store(paths)
    agent_id = AgentId.generate()
    resolver = make_resolver_with_data(agents_json=make_agents_json(agent_id, host_name="my-ws"))
    write_canonical_env(paths, agent_id, "RESTIC_REPOSITORY=x\nRESTIC_PASSWORD=y\n")

    record = store.build_record_from_resolver(_user_id(), str(agent_id), resolver)

    assert record is not None
    assert record.encrypted_secrets is None


def test_reconcile_migrates_legacy_associations_and_retires_the_file(paths: InstallationPaths) -> None:
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


def test_reconcile_migrates_sessions_json_era_associations(paths: InstallationPaths) -> None:
    """The pre-associations-file layout (sessions.json identity records) converts too.

    An install whose associations were only ever written in the sessions.json
    era has no workspace_associations.json; its workspace_ids must still
    become records, and the file must retire so later passes cannot re-create
    deliberately disassociated records from it.
    """
    cli = make_fake_imbue_cloud_cli()
    store = _make_store(paths, cli)
    user_id = _user_id()
    agent_id = AgentId.generate()
    resolver = make_resolver_with_data(agents_json=make_agents_json(agent_id, host_name="old-ws"))
    (paths.data_dir / "sessions.json").write_text(
        json.dumps({user_id: {"user_id": user_id, "email": _EMAIL, "workspace_ids": [str(agent_id)]}})
    )

    store.reconcile({user_id: _EMAIL}, resolver)

    assert store.associations_view() == {user_id: [str(agent_id)]}
    assert not (paths.data_dir / "sessions.json").exists()
    assert (paths.data_dir / "sessions.json.pre-sync").exists()
    assert str(agent_id) in {row["agent_id"] for row in cli.sync_records_by_email[_EMAIL].values()}


def test_read_legacy_associations_prefers_the_newer_file_over_sessions_json(paths: InstallationPaths) -> None:
    store = _make_store(paths)
    user_id = _user_id()
    (paths.data_dir / "workspace_associations.json").write_text(json.dumps({user_id: ["agent-new"]}))
    (paths.data_dir / "sessions.json").write_text(json.dumps({user_id: {"workspace_ids": ["agent-old"]}}))

    assert store.read_legacy_associations() == {user_id: ["agent-new"]}


def test_reconcile_keeps_legacy_file_until_every_entry_converts(paths: InstallationPaths) -> None:
    """A failed poll proves nothing: a legacy association whose machine was
    not discoverable (its provider errored) must survive for a later pass
    instead of being dropped when the file retires."""
    cli = make_fake_imbue_cloud_cli()
    store = _make_store(paths, cli)
    user_id = _user_id()
    agent_id = AgentId.generate()
    legacy_path = paths.data_dir / "workspace_associations.json"
    legacy_path.write_text(json.dumps({user_id: [str(agent_id)]}))
    # Discovery completed, but a provider errored this poll, so absence from
    # the known ids proves nothing about the workspace.
    blocked_resolver = make_resolver_with_data(agents_json=json.dumps({"agents": []}))
    errored_name = ProviderInstanceName("lima")
    seed_provider_snapshots(
        blocked_resolver,
        error_by_provider_name={
            errored_name: DiscoveryError(type_name="RuntimeError", message="poll failed", provider_name=errored_name)
        },
    )

    store.reconcile({user_id: _EMAIL}, blocked_resolver)

    assert store.associations_view() == {}
    assert legacy_path.exists()

    # The next clean pass discovers the workspace: it converts and the file retires.
    healthy_resolver = make_resolver_with_data(agents_json=make_agents_json(agent_id, host_name="legacy-ws"))
    store.reconcile({user_id: _EMAIL}, healthy_resolver)
    assert store.associations_view() == {user_id: [str(agent_id)]}
    assert not legacy_path.exists()
    assert legacy_path.with_name(legacy_path.name + ".pre-sync").exists()


def test_reconcile_drops_legacy_association_when_only_unauthorized_providers_error(paths: InstallationPaths) -> None:
    """Providers without credentials error on every poll, so treating them as
    failed polls would keep a gone machine's legacy association in limbo
    forever; only a genuinely failed poll may block the drop."""
    cli = make_fake_imbue_cloud_cli()
    store = _make_store(paths, cli)
    user_id = _user_id()
    gone_agent_id = AgentId.generate()
    legacy_path = paths.data_dir / "workspace_associations.json"
    legacy_path.write_text(json.dumps({user_id: [str(gone_agent_id)]}))
    # Discovery completed and knows about a different workspace only; the sole
    # errored provider is unconfigured (not-authorized), not a failed poll.
    resolver = make_resolver_with_data(agents_json=make_agents_json(AgentId.generate(), host_name="other-ws"))
    unauthorized_name = ProviderInstanceName("aws-us-east-1")
    seed_provider_snapshots(
        resolver,
        error_by_provider_name={
            unauthorized_name: DiscoveryError(
                type_name="ProviderNotAuthorizedError",
                message="AWS credentials not configured",
                provider_name=unauthorized_name,
            )
        },
    )

    store.reconcile({user_id: _EMAIL}, resolver)

    assert store.associations_view() == {}
    assert not legacy_path.exists()
    assert legacy_path.with_name(legacy_path.name + ".pre-sync").exists()


def test_reconcile_keeps_legacy_file_for_signed_out_accounts(paths: InstallationPaths) -> None:
    cli = make_fake_imbue_cloud_cli()
    store = _make_store(paths, cli)
    signed_in_user_id = _user_id()
    signed_out_user_id = _user_id()
    agent_id = AgentId.generate()
    legacy_path = paths.data_dir / "workspace_associations.json"
    legacy_path.write_text(json.dumps({signed_in_user_id: [str(agent_id)], signed_out_user_id: [_agent_id()]}))
    resolver = make_resolver_with_data(agents_json=make_agents_json(agent_id, host_name="legacy-ws"))

    store.reconcile({signed_in_user_id: _EMAIL}, resolver)

    # The signed-in account's entry converted; the other account's entry
    # waits (retiring now would drop it before that account can sign in).
    assert store.associations_view() == {signed_in_user_id: [str(agent_id)]}
    assert legacy_path.exists()


def test_reconcile_does_not_churn_revisions_without_a_master_password(paths: InstallationPaths) -> None:
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
    workspace_id = next(iter(cli.sync_records_by_email[_EMAIL]))
    revision_after_associate = cli.sync_records_by_email[_EMAIL][workspace_id]["revision"]

    store.reconcile({user_id: _EMAIL}, resolver)
    store.reconcile({user_id: _EMAIL}, resolver)

    assert cli.sync_records_by_email[_EMAIL][workspace_id]["revision"] == revision_after_associate


def test_reconcile_tombstones_definitively_absent_local_rows(paths: InstallationPaths) -> None:
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
            hosting_device_id=device_id_for_test("store-test-1"),
        ),
    )
    # Discovery completed and knows about a different workspace only.
    # make_resolver_with_data runs update_agents, which marks initial discovery complete.
    resolver = make_resolver_with_data(agents_json=make_agents_json(AgentId.generate(), host_name="other"))

    # Before the record's provider has produced a single snapshot, absence is
    # not evidence: the row must survive (the slow-first-poll startup race).
    store.reconcile({user_id: _EMAIL}, resolver)
    assert store.list_records(user_id)[0].state == RECORD_STATE_ACTIVE

    # Once the provider has reported a snapshot that lacks the host, the
    # absence is definitive and the row tombstones.
    resolver.update_providers(
        provider_name=ProviderInstanceName("local"),
        provider=None,
        error=None,
        last_snapshot_at=datetime.now(timezone.utc),
    )
    store.reconcile({user_id: _EMAIL}, resolver)
    assert store.list_records(user_id)[0].state == RECORD_STATE_DESTROYED


def test_locked_device_push_preserves_server_secrets(paths: InstallationPaths) -> None:
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
    cli.sync_records_by_email[_EMAIL] = {remote.agent_id: remote.to_wire(1)}
    store.pull(user_id, _EMAIL)

    pulled = store.list_records(user_id)[0]
    renamed = pulled.model_copy_update(to_update(pulled.field_ref().display_name, "new-name"))
    store.upsert_local_record(user_id, _EMAIL, renamed)

    server_row = cli.sync_records_by_email[_EMAIL][remote.agent_id]
    assert server_row["display_name"] == "new-name"
    assert server_row["encrypted_secrets"] == "b3BhcXVl"


def test_reconcile_does_not_tombstone_unenriched_create_seed_rows(paths: InstallationPaths) -> None:
    """A create-path seed row (empty provider_kind) must survive a reconcile
    that runs before discovery has seen the new machine -- 'absent from
    discovery' says nothing about a host discovery never enumerated."""
    cli = make_fake_imbue_cloud_cli()
    store = _make_store(paths, cli)
    user_id = _user_id()
    seed = ReplicaRecord(
        host_id="host-just-created",
        agent_id=_agent_id(),
        display_name="brand new",
        provider_kind="",
        hosting_device_id=device_id_for_test("store-test-1"),
        device_label="test-laptop",
    )
    store.upsert_local_record(user_id, _EMAIL, seed)
    # Discovery completed but has not caught up to the new workspace yet.
    resolver = make_resolver_with_data(agents_json=json.dumps({"agents": []}))

    store.reconcile({user_id: _EMAIL}, resolver)

    records = store.list_records(user_id)
    assert len(records) == 1
    assert records[0].state == RECORD_STATE_ACTIVE


def test_reconcile_never_tombstones_rows_with_empty_provenance(paths: InstallationPaths) -> None:
    """Bug-era rows pushed with ``hosting_device_id=""`` (an install whose first
    session predated the minds-owned device id) attribute to no install, so
    absent-host tombstoning must leave them alone."""
    cli = make_fake_imbue_cloud_cli()
    store = _make_store(paths, cli)
    user_id = _user_id()
    remote = ReplicaRecord(
        host_id="host-idless",
        agent_id=_agent_id(),
        provider_kind="lima",
        hosting_device_id="",
        device_label="other-idless-install",
    )
    cli.sync_records_by_email[_EMAIL] = {remote.agent_id: remote.to_wire(1)}
    resolver = make_resolver_with_data(agents_json=json.dumps({"agents": []}))

    store.reconcile({user_id: _EMAIL}, resolver)

    records = store.list_records(user_id)
    assert len(records) == 1
    assert records[0].state == RECORD_STATE_ACTIVE


def test_reconcile_does_not_tombstone_other_device_rows(paths: InstallationPaths) -> None:
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
    cli.sync_records_by_email[_EMAIL] = {remote.agent_id: remote.to_wire(1)}
    resolver = make_resolver_with_data(agents_json=json.dumps({"agents": []}))

    store.reconcile({user_id: _EMAIL}, resolver)

    records = store.list_records(user_id)
    assert len(records) == 1
    assert records[0].state == RECORD_STATE_ACTIVE


def _make_mngr_profile_dir(tmp_path: Path) -> tuple[Path, Path]:
    """A minimal mngr host dir with one active profile; returns (mngr_host_dir, profile_dir)."""
    mngr_dir = tmp_path / "mngr"
    profile_dir = mngr_dir / "profiles" / "profile1"
    profile_dir.mkdir(parents=True)
    (mngr_dir / "config.toml").write_text('profile = "profile1"\n')
    return mngr_dir, profile_dir


def _make_per_host_key_dir(profile_dir: Path, host_id: str) -> Path:
    host_dir = profile_dir / "providers" / "imbue_cloud_alice" / "imbue_cloud_alice" / "hosts" / host_id
    host_dir.mkdir(parents=True)
    return host_dir


def test_collect_ssh_key_material_finds_per_host_keys(tmp_path: Path) -> None:
    mngr_dir, profile_dir = _make_mngr_profile_dir(tmp_path)
    host_dir = _make_per_host_key_dir(profile_dir, "host-abc")
    (host_dir / "ssh_key").write_text("PRIVATE-KEY-BYTES")
    (host_dir / "known_hosts").write_text("[1.2.3.4]:2222 ssh-ed25519 AAAA")

    private_key, known_hosts = collect_ssh_key_material(mngr_dir, "host-abc")

    assert private_key == "PRIVATE-KEY-BYTES"
    assert known_hosts == "[1.2.3.4]:2222 ssh-ed25519 AAAA\n"


def test_collect_ssh_key_material_renders_clean_pins_from_the_store(tmp_path: Path) -> None:
    """The record carries the store's current pins -- one per (endpoint, keytype) --
    not the file's raw lines: a stale duplicate an old append-only writer left in
    the file collapses to the live pin instead of entering the record."""
    mngr_dir, profile_dir = _make_mngr_profile_dir(tmp_path)
    host_id = HostId.generate()
    host_dir = _make_per_host_key_dir(profile_dir, str(host_id))
    (host_dir / "ssh_key").write_text("PRIVATE-KEY-BYTES")
    (host_dir / "known_hosts").write_text(
        "[1.2.3.4]:22001 ssh-ed25519 AAAA-stale\n[1.2.3.4]:22001 ssh-ed25519 AAAA-live\n"
    )
    pin_host_key(host_dir / "known_hosts", "1.2.3.4", 23001, "ssh-ed25519 AAAA-vm", host_id, HostKeyOrigin.USER)

    _, known_hosts = collect_ssh_key_material(mngr_dir, str(host_id))

    assert known_hosts == "[1.2.3.4]:22001 ssh-ed25519 AAAA-live\n[1.2.3.4]:23001 ssh-ed25519 AAAA-vm\n"


def test_collect_ssh_key_material_never_collects_provider_wide_keys(tmp_path: Path) -> None:
    """A synced record may only ever carry a key that opens its one host -- the lima
    provider-wide root key (which opens ALL of the user's lima VMs) must not be
    collected even for lima-hosted workspaces."""
    mngr_dir, profile_dir = _make_mngr_profile_dir(tmp_path)
    lima_keys_dir = profile_dir / "providers" / "lima" / "lima" / "keys"
    lima_keys_dir.mkdir(parents=True)
    (lima_keys_dir / "root_ssh_key").write_text("PROVIDER-WIDE-KEY")
    (lima_keys_dir / "hosts").write_text("[127.0.0.1]:60022 ssh-ed25519 AAAA")

    assert collect_ssh_key_material(mngr_dir, "host-lima-1") == (None, None)


def test_collect_ssh_key_material_returns_none_when_uninitialized(tmp_path: Path) -> None:
    assert collect_ssh_key_material(tmp_path / "missing", "host-x") == (None, None)


def test_workspace_secrets_payload_tolerates_unknown_fields() -> None:
    """A payload written by a future minds version must still parse here -- rejecting
    the whole blob would cost this install everything in it, including restic_env."""
    payload = WorkspaceSecretsPayload.model_validate_json(
        '{"restic_env": "RESTIC_REPOSITORY=s3:bucket", "future_field": {"nested": 1}}'
    )

    assert payload.restic_env == "RESTIC_REPOSITORY=s3:bucket"
    assert payload.ssh_private_key is None


def test_derive_openssh_public_key_line_roundtrips_an_openssh_format_key() -> None:
    private_key = ed25519.Ed25519PrivateKey.generate()
    private_text = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    expected_public = (
        private_key.public_key()
        .public_bytes(encoding=serialization.Encoding.OpenSSH, format=serialization.PublicFormat.OpenSSH)
        .decode("utf-8")
    )

    assert derive_openssh_public_key_line(private_text) == expected_public


def test_derive_openssh_public_key_line_roundtrips_mngrs_traditional_pem_rsa_key() -> None:
    # The legacy flavor older mngr installs wrote for client keys (mngr now
    # generates Ed25519): RSA in traditional PEM ("-----BEGIN RSA PRIVATE
    # KEY-----"), which needs the PEM loader, not the OpenSSH one. Records
    # synced from those installs still carry these keys.
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_text = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    expected_public = (
        private_key.public_key()
        .public_bytes(encoding=serialization.Encoding.OpenSSH, format=serialization.PublicFormat.OpenSSH)
        .decode("utf-8")
    )

    assert private_text.startswith("-----BEGIN RSA PRIVATE KEY-----")
    assert derive_openssh_public_key_line(private_text) == expected_public


def test_derive_openssh_public_key_line_returns_none_for_garbage() -> None:
    assert derive_openssh_public_key_line("not a key at all") is None


def test_reconcile_resurrects_a_locally_hosted_tombstone_whose_workspace_is_live(paths: InstallationPaths) -> None:
    """A DESTROYED row hosted here whose agent is live in discovery was tombstoned
    prematurely (e.g. by an install predating the per-provider snapshot gate) --
    the reconcile must re-activate and push it."""
    cli = make_fake_imbue_cloud_cli()
    store = _make_store(paths, cli)
    user_id = _user_id()
    live_agent = AgentId.generate()
    tombstoned = ReplicaRecord(
        host_id="host-back",
        agent_id=str(live_agent),
        display_name="docker-2",
        provider_kind="local",
        hosting_device_id=device_id_for_test("store-test-1"),
        state=RECORD_STATE_DESTROYED,
    )
    cli.sync_records_by_email[_EMAIL] = {tombstoned.agent_id: tombstoned.to_wire(4)}
    resolver = make_resolver_with_data(agents_json=make_agents_json(live_agent, host_name="docker-2"))

    store.reconcile({user_id: _EMAIL}, resolver)

    record = store.list_records(user_id)[0]
    assert record.state == RECORD_STATE_ACTIVE
    assert record.is_dirty is False
    assert cli.sync_records_by_email[_EMAIL][record.agent_id]["state"] == RECORD_STATE_ACTIVE


def test_reconcile_never_resurrects_while_a_destroy_is_in_flight(paths: InstallationPaths) -> None:
    """The destroy flow tombstones the record before the host actually goes down;
    a reconcile in that window must not undo the tombstone."""
    cli = make_fake_imbue_cloud_cli()
    store = _make_store(paths, cli)
    user_id = _user_id()
    doomed_agent = AgentId.generate()
    tombstoned = ReplicaRecord(
        host_id="host-doomed",
        agent_id=str(doomed_agent),
        provider_kind="local",
        hosting_device_id=device_id_for_test("store-test-1"),
        state=RECORD_STATE_DESTROYED,
    )
    cli.sync_records_by_email[_EMAIL] = {"host-doomed": tombstoned.to_wire(2)}
    (paths.data_dir / "destroying" / str(doomed_agent)).mkdir(parents=True)
    resolver = make_resolver_with_data(agents_json=make_agents_json(doomed_agent, host_name="doomed"))

    store.reconcile({user_id: _EMAIL}, resolver)

    assert store.list_records(user_id)[0].state == RECORD_STATE_DESTROYED


def test_reconcile_never_resurrects_other_device_tombstones(paths: InstallationPaths) -> None:
    cli = make_fake_imbue_cloud_cli()
    store = _make_store(paths, cli)
    user_id = _user_id()
    foreign_agent = AgentId.generate()
    tombstoned = ReplicaRecord(
        host_id="host-foreign",
        agent_id=str(foreign_agent),
        provider_kind="local",
        hosting_device_id="device-someone-else",
        state=RECORD_STATE_DESTROYED,
    )
    cli.sync_records_by_email[_EMAIL] = {"host-foreign": tombstoned.to_wire(2)}
    resolver = make_resolver_with_data(agents_json=make_agents_json(foreign_agent, host_name="foreign"))

    store.reconcile({user_id: _EMAIL}, resolver)

    assert store.list_records(user_id)[0].state == RECORD_STATE_DESTROYED


def _empty_complete_resolver(provider_names: tuple[str, ...]) -> MngrCliBackendResolver:
    """A resolver whose discovery completed cleanly and lists no workspaces at all."""
    resolver = make_resolver_with_data(agents_json=json.dumps({"agents": []}))
    seed_provider_snapshots(
        resolver,
        error_by_provider_name={},
    )
    for provider_name in provider_names:
        resolver.update_providers(
            provider_name=ProviderInstanceName(provider_name),
            provider=None,
            error=None,
            last_snapshot_at=datetime.now(timezone.utc),
        )
    return resolver


def test_cloud_rows_are_never_tombstoned_as_definitively_absent(paths: InstallationPaths) -> None:
    """The tombstone-safety invariant for web-created / cloud workspaces.

    A cloud (imbue_cloud) row may be created by a device that never
    materializes its SSH key here, and its lease can be live while a listing
    pass transiently misses it -- so "absent from this device's discovery"
    must never tombstone it. Only the lease going away (server-driven) ends a
    cloud row's life.
    """
    cli = make_fake_imbue_cloud_cli()
    user_id = _user_id()
    cli.add_account(user_id=user_id, email=_EMAIL)
    store = _make_store(paths, cli)

    cloud_host_id = f"host-{uuid4().hex}"
    cloud_record = ReplicaRecord(
        host_id=cloud_host_id,
        agent_id=_agent_id(),
        display_name="web-created",
        provider_kind="imbue_cloud_alice",
        hosting_device_id=None,
        device_label="web",
        state=RECORD_STATE_ACTIVE,
        revision=0,
        is_dirty=True,
    )
    local_host_id = f"host-{uuid4().hex}"
    local_record = ReplicaRecord(
        host_id=local_host_id,
        agent_id=_agent_id(),
        display_name="local-docker",
        provider_kind="docker",
        hosting_device_id=store.device_id,
        device_label="test-laptop",
        state=RECORD_STATE_ACTIVE,
        revision=0,
        is_dirty=True,
    )
    store.upsert_local_record(user_id, _EMAIL, cloud_record)
    store.upsert_local_record(user_id, _EMAIL, local_record)

    # Discovery completed cleanly for both providers and lists nothing.
    resolver = _empty_complete_resolver(("imbue_cloud_alice", "docker"))
    store.reconcile({user_id: _EMAIL}, resolver)

    pushed = cli.sync_records_by_email[_EMAIL]
    # The locally-hosted row IS tombstoned (the absent pass works)...
    assert pushed[local_record.agent_id]["state"] == RECORD_STATE_DESTROYED
    # ...but the cloud row survives untouched: never "definitively absent".
    assert pushed[cloud_record.agent_id]["state"] == RECORD_STATE_ACTIVE
    replica_states = {record.host_id: record.state for record in store.list_records(user_id)}
    assert replica_states[cloud_host_id] == RECORD_STATE_ACTIVE


@pytest.mark.witnesses("remote-compatibility.newer-record-push-refused")
@pytest.mark.witnesses("remote-compatibility.newer-records-read-only", partial="covers pushes only")
def test_upsert_refuses_a_record_with_a_newer_record_format(paths: InstallationPaths) -> None:
    store = _make_store(paths)
    too_new = ReplicaRecord(host_id="host-1", agent_id=_agent_id(), provider_kind="lima", record_format=2)

    with pytest.raises(WorkspaceRecordTooNewError, match="update the app"):
        store.upsert_local_record(_user_id(), _EMAIL, too_new)


@pytest.mark.witnesses(
    "remote-compatibility.newer-records-read-only", partial="covers tombstone/disassociate/remove only"
)
def test_state_changing_operations_refuse_a_newer_format_record(paths: InstallationPaths) -> None:
    cli = make_fake_imbue_cloud_cli()
    store = _make_store(paths, cli)
    user_id = _user_id()
    agent_id = _agent_id()
    # Seed via a pull of a server row written by a newer client: the pull
    # itself must tolerate the newer record_format (the record stays readable).
    cli.sync_records_by_email[_EMAIL] = {
        agent_id: {
            "host_id": "host-9",
            "agent_id": agent_id,
            "provider_kind": "lima",
            "state": RECORD_STATE_ACTIVE,
            "revision": 3,
            "record_format": 2,
        }
    }
    assert store.pull(user_id, _EMAIL) is True

    with pytest.raises(WorkspaceRecordTooNewError):
        store.tombstone_record(user_id, _EMAIL, agent_id)
    with pytest.raises(WorkspaceRecordTooNewError):
        store.disassociate_workspace_or_raise(user_id, _EMAIL, agent_id)
    with pytest.raises(WorkspaceRecordTooNewError):
        store.remove_record_or_raise(user_id, _EMAIL, agent_id)
    # The record itself is untouched (still active, still present).
    kept = store.list_records(user_id)
    assert [r.state for r in kept] == [RECORD_STATE_ACTIVE]


def test_replica_record_from_wire_defaults_and_carries_record_format() -> None:
    defaulted = replica_record_from_wire({"host_id": "host-1", "agent_id": "a", "revision": 1})
    assert defaulted.record_format == 1
    carried = replica_record_from_wire({"host_id": "host-1", "agent_id": "a", "revision": 1, "record_format": 3})
    assert carried.record_format == 3
    assert carried.to_wire(2)["record_format"] == 3


@pytest.mark.witnesses("remote-compatibility.newer-payloads-never-rewritten", partial="rewrite refusal only")
def test_build_encrypted_secrets_refuses_a_newer_payload_format(paths: InstallationPaths) -> None:
    store = _make_store(paths)
    user_id = _user_id()
    agent_id = AgentId.generate()
    ensure_dek(paths, user_id)
    write_canonical_env(paths, agent_id, "RESTIC_REPOSITORY=x\nRESTIC_PASSWORD=y\n")
    dek = load_dek(paths, user_id)
    assert dek is not None
    newer_blob = b64encode(
        encrypt_secrets(dek, json.dumps({"payload_format": 2, "from_the_future": "keep"}).encode("utf-8"))
    ).decode("ascii")

    built = store.build_encrypted_secrets(user_id, str(agent_id), "host-1", newer_blob)

    assert built is None


@pytest.mark.witnesses("remote-compatibility.newer-payloads-never-rewritten", partial="unknown-key round-trip only")
def test_build_encrypted_secrets_round_trips_unknown_payload_keys(paths: InstallationPaths) -> None:
    store = _make_store(paths)
    user_id = _user_id()
    agent_id = AgentId.generate()
    ensure_dek(paths, user_id)
    env_text = "RESTIC_REPOSITORY=x\nRESTIC_PASSWORD=y\n"
    write_canonical_env(paths, agent_id, env_text)
    dek = load_dek(paths, user_id)
    assert dek is not None
    existing_payload = {"payload_format": 1, "restic_env": "old", "added_by_a_newer_client": "must-survive"}
    existing_blob = b64encode(encrypt_secrets(dek, json.dumps(existing_payload).encode("utf-8"))).decode("ascii")

    built = store.build_encrypted_secrets(user_id, str(agent_id), "host-1", existing_blob)

    assert built is not None
    rewritten = json.loads(decrypt_secrets(dek, b64decode(built.encrypted)))
    # This client's own material overwrites the fields it owns...
    assert rewritten["restic_env"] == env_text
    # ...while a field a newer client added rides through verbatim.
    assert rewritten["added_by_a_newer_client"] == "must-survive"
    assert rewritten["payload_format"] == 1
