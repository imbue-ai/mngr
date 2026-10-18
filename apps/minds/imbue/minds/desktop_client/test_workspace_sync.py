"""End-to-end workspace-sync flows across two simulated devices.

Two Imbue Studio data dirs share one (fake, in-memory) connector backend: device A
provisions and pushes; device B pulls, sees the remote workspace, unlocks
with the master password, decrypts the synced secrets, and materializes the
backup env. This is the whole cross-device story minus live HTTP -- the wire
halves are covered by the connector endpoint tests and the plugin client
tests.
"""

import json
from datetime import datetime
from datetime import timezone
from pathlib import Path
from uuid import uuid4

from pydantic import SecretStr

from imbue.imbue_common.model_update import to_update
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backup_env_store import read_canonical_env
from imbue.minds.desktop_client.backup_env_store import write_canonical_env
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import add_modal_connection_for_test
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_profiled_device_for_test
from imbue.minds.desktop_client.conftest import make_profiled_device_with_connections_for_test
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.desktop_client.connections.data_types import ConnectionAvailability
from imbue.minds.desktop_client.connections.primitives import ConnectionBackend
from imbue.minds.desktop_client.connections.primitives import LOCAL_PROFILE_USER_ID
from imbue.minds.desktop_client.connections.primitives import imbue_cloud_provider_name_for_user
from imbue.minds.desktop_client.connections.primitives import provider_instance_name_for_connection
from imbue.minds.desktop_client.dek_store import is_account_unlocked
from imbue.minds.desktop_client.dek_store import is_master_password_set_for_account
from imbue.minds.desktop_client.dek_store import load_dek
from imbue.minds.desktop_client.dek_store import read_bundle_mirror
from imbue.minds.desktop_client.dek_store import set_master_password_for_account
from imbue.minds.desktop_client.host_access_material import host_access_paths
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.sync_scheduler import WorkspaceSyncScheduler
from imbue.minds.desktop_client.testing import device_id_for_test
from imbue.minds.desktop_client.testing import generate_test_ssh_private_key
from imbue.minds.desktop_client.workspace_record_store import RECORD_STATE_ACTIVE
from imbue.minds.desktop_client.workspace_record_store import ReplicaRecord
from imbue.minds.desktop_client.workspace_record_store import SUPPORTED_PAYLOAD_FORMAT
from imbue.minds.desktop_client.workspace_record_store import WorkspaceRecordStore
from imbue.minds.desktop_client.workspace_record_store import encode_encrypted_secrets
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.host_key_store import HostKeyOrigin
from imbue.mngr.providers.host_key_store import host_key_store_path
from imbue.mngr.providers.host_key_store import load_current_host_key_pins
from imbue.mngr.providers.host_key_store import load_host_key_record
from imbue.mngr.providers.host_key_store import parse_known_hosts_address
from imbue.mngr.providers.host_key_store import pin_host_key

_USER_ID = "11111111-2222-3333-4444-555555555555"
_EMAIL = "sync-user@example.com"
_PASSWORD = "correct horse battery staple"


def _make_device(
    base: Path, name: str, cli: FakeImbueCloudCli
) -> tuple[InstallationPaths, WorkspaceRecordStore, MultiAccountSessionStore]:
    paths = InstallationPaths(data_dir=base / name)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    record_store = WorkspaceRecordStore(
        paths=paths,
        cli=cli,
        device_id=device_id_for_test(name),
        device_label=name,
    )
    session_store = MultiAccountSessionStore(data_dir=paths.data_dir, cli=cli, record_store=record_store)
    return paths, record_store, session_store


def _resolver_with_workspace(agent_id: AgentId, host_id: HostId, name: str) -> MngrCliBackendResolver:
    agents = [{"id": str(agent_id), "labels": {"is_primary": "true"}, "host": {"id": str(host_id), "name": name}}]
    return make_resolver_with_data(agents_json=json.dumps({"agents": agents}))


def test_two_device_sync_round_trip_with_unlock_and_env_materialization(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)

    # -- Device A: set a master password, provision a backed-up workspace, sync it.
    paths_a, store_a, session_a = _make_device(tmp_path, "laptop", cli)
    bundle = set_master_password_for_account(paths_a, _USER_ID, SecretStr(_PASSWORD))
    assert bundle is not None
    cli.sync_bundle_push(_EMAIL, bundle)

    agent_id = AgentId.generate()
    host_id = HostId.generate()
    env_text = "RESTIC_REPOSITORY=s3:https://r2.example/bucket\nRESTIC_PASSWORD=ws-random-pass\n"
    write_canonical_env(paths_a, agent_id, env_text)
    resolver_a = _resolver_with_workspace(agent_id, host_id, "my-workspace")
    session_a.associate_workspace(_USER_ID, str(agent_id), resolver_a)

    pushed = cli.sync_records_by_email[_EMAIL][str(agent_id)]
    assert pushed["encrypted_secrets"] is not None
    # The secrets on the wire are ciphertext, never the plaintext env.
    assert "ws-random-pass" not in str(pushed["encrypted_secrets"])

    # -- Device B: fresh install, same account. Pull sees the workspace as remote.
    paths_b, store_b, session_b = _make_device(tmp_path, "desktop", cli)
    resolver_b = make_resolver_with_data(agents_json=json.dumps({"agents": []}))
    scheduler_b = WorkspaceSyncScheduler(record_store=store_b, session_store=session_b, resolver=resolver_b)
    scheduler_b.run_one_pass()

    records_b = store_b.list_records(_USER_ID)
    assert len(records_b) == 1
    assert records_b[0].display_name == "my-workspace"
    assert records_b[0].device_label == "laptop"
    assert records_b[0].hosting_device_id == device_id_for_test("laptop")

    # Metadata is readable without any password; secrets are not (locked).
    assert not is_account_unlocked(paths_b, _USER_ID)
    assert store_b.locked_account_user_ids([_USER_ID]) == [_USER_ID]
    assert store_b.decrypt_record_secrets(_USER_ID, records_b[0]) is None
    assert store_b.materialize_env_from_record(str(agent_id)) is False

    # A wrong password does not unlock; the right one installs the DEK.
    assert store_b.unlock_account(_USER_ID, _EMAIL, SecretStr("wrong")) is False
    assert store_b.unlock_account(_USER_ID, _EMAIL, SecretStr(_PASSWORD)) is True
    assert is_account_unlocked(paths_b, _USER_ID)
    assert store_b.locked_account_user_ids([_USER_ID]) == []

    # Unlocked: the synced secrets decrypt and the backup env materializes.
    payload = store_b.decrypt_record_secrets(_USER_ID, records_b[0])
    assert payload is not None
    assert payload.restic_env == env_text
    assert store_b.materialize_env_from_record(str(agent_id)) is True
    assert read_canonical_env(paths_b, agent_id) == env_text


def test_empty_password_account_syncs_metadata_but_never_secrets(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    paths_a, _store_a, session_a = _make_device(tmp_path, "laptop", cli)

    agent_id = AgentId.generate()
    host_id = HostId.generate()
    write_canonical_env(paths_a, agent_id, "RESTIC_REPOSITORY=s3:x\nRESTIC_PASSWORD=y\n")
    resolver = _resolver_with_workspace(agent_id, host_id, "metadata-only")
    session_a.associate_workspace(_USER_ID, str(agent_id), resolver)

    pushed = cli.sync_records_by_email[_EMAIL][str(agent_id)]
    assert pushed["display_name"] == "metadata-only"
    # No master password -> the metadata-only tier: nothing secret on the wire.
    assert pushed["encrypted_secrets"] is None
    assert _EMAIL not in cli.sync_bundle_by_email


def test_setting_a_password_later_pushes_the_pending_secrets(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    paths_a, store_a, session_a = _make_device(tmp_path, "laptop", cli)

    agent_id = AgentId.generate()
    host_id = HostId.generate()
    write_canonical_env(paths_a, agent_id, "RESTIC_REPOSITORY=s3:x\nRESTIC_PASSWORD=y\n")
    resolver = _resolver_with_workspace(agent_id, host_id, "upgraded")
    session_a.associate_workspace(_USER_ID, str(agent_id), resolver)
    assert cli.sync_records_by_email[_EMAIL][str(agent_id)]["encrypted_secrets"] is None

    # The empty -> non-empty transition (the settings-page flow): wrap, push
    # the bundle, then push all pending secrets.
    bundle = set_master_password_for_account(paths_a, _USER_ID, SecretStr(_PASSWORD))
    assert bundle is not None
    cli.sync_bundle_push(_EMAIL, bundle)
    store_a.push_all_secrets(_USER_ID, _EMAIL, resolver)

    assert cli.sync_records_by_email[_EMAIL][str(agent_id)]["encrypted_secrets"] is not None


def test_password_change_does_not_degrade_other_device_secrets(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)

    # Device A hosts a backed-up workspace with a password set and syncs it.
    paths_a, _store_a, session_a = _make_device(tmp_path, "laptop", cli)
    bundle = set_master_password_for_account(paths_a, _USER_ID, SecretStr(_PASSWORD))
    assert bundle is not None
    cli.sync_bundle_push(_EMAIL, bundle)
    agent_id = AgentId.generate()
    host_id = HostId.generate()
    write_canonical_env(paths_a, agent_id, "RESTIC_REPOSITORY=s3:x\nRESTIC_PASSWORD=y\n")
    resolver_a = _resolver_with_workspace(agent_id, host_id, "hosted-on-laptop")
    session_a.associate_workspace(_USER_ID, str(agent_id), resolver_a)
    original_blob = cli.sync_records_by_email[_EMAIL][str(agent_id)]["encrypted_secrets"]
    assert original_blob is not None

    # Device B pulls, unlocks, and materializes the env, so partial secret
    # material (the env, but not the laptop's SSH key) now exists on B.
    paths_b, store_b, session_b = _make_device(tmp_path, "desktop", cli)
    empty_resolver = make_resolver_with_data(agents_json=json.dumps({"agents": []}))
    WorkspaceSyncScheduler(record_store=store_b, session_store=session_b, resolver=empty_resolver).run_one_pass()
    assert store_b.unlock_account(_USER_ID, _EMAIL, SecretStr(_PASSWORD)) is True
    assert store_b.materialize_env_from_record(str(agent_id)) is True

    # A password change on B is rewrap-only: push_all_secrets must not rebuild
    # the laptop-hosted record from B's partial material and overwrite the
    # laptop's full blob -- even when B's discovery can see the workspace.
    new_bundle = set_master_password_for_account(paths_b, _USER_ID, SecretStr("a different passphrase"))
    assert new_bundle is not None
    cli.sync_bundle_push(_EMAIL, new_bundle)
    resolver_b = _resolver_with_workspace(agent_id, host_id, "hosted-on-laptop")
    store_b.push_all_secrets(_USER_ID, _EMAIL, resolver_b)

    assert cli.sync_records_by_email[_EMAIL][str(agent_id)]["encrypted_secrets"] == original_blob


def test_scheduler_pass_converts_legacy_state_and_tombstones_absent_rows(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    paths, store, session = _make_device(tmp_path, "laptop", cli)

    # Legacy install state: an associations file naming a live workspace and
    # a saved plaintext master password with its hash.
    agent_id = AgentId.generate()
    host_id = HostId.generate()
    (paths.data_dir / "workspace_associations.json").write_text(json.dumps({_USER_ID: [str(agent_id)]}))
    (paths.data_dir / "backup_password").write_text("legacy-pass\n")
    resolver = _resolver_with_workspace(agent_id, host_id, "legacy-ws")
    scheduler = WorkspaceSyncScheduler(record_store=store, session_store=session, resolver=resolver)

    scheduler.run_one_pass()

    # The association migrated into a pushed record and the legacy file retired.
    assert store.associations_view() == {_USER_ID: [str(agent_id)]}
    assert not (paths.data_dir / "workspace_associations.json").exists()
    assert str(agent_id) in cli.sync_records_by_email[_EMAIL]
    # The carried-over legacy password's bundle must reach the connector too:
    # without it no other device can ever unlock the synced secrets.
    assert _EMAIL in cli.sync_bundle_by_email

    # The workspace disappears locally (definitively absent) -> tombstoned.
    # Absence only counts once the record's provider has produced a snapshot
    # (a provider that never reported proves nothing), so seed one.
    empty_resolver = make_resolver_with_data(agents_json=json.dumps({"agents": []}))
    empty_resolver.update_providers(
        provider_name=ProviderInstanceName("local"),
        provider=None,
        error=None,
        last_snapshot_at=datetime.now(timezone.utc),
    )
    scheduler_after = WorkspaceSyncScheduler(record_store=store, session_store=session, resolver=empty_resolver)
    scheduler_after.run_one_pass()
    assert cli.sync_records_by_email[_EMAIL][str(agent_id)]["state"] == "destroyed"


# SSH material materialization (cloud rows accessible from any install)


def _cloud_host_key_dir(profile_dir: Path, host_id: HostId) -> Path:
    instance_name = imbue_cloud_provider_name_for_user(_USER_ID)
    return profile_dir / "providers" / "imbue_cloud" / instance_name / "hosts" / str(host_id)


def _pin_at_line_endpoint(
    known_hosts_path: Path, known_hosts_line: str, public_key: str, host_id: HostId, origin: HostKeyOrigin
) -> None:
    """Pin ``public_key`` at the endpoint named by a known_hosts line's leading field."""
    endpoint = parse_known_hosts_address(known_hosts_line.split()[0])
    assert endpoint is not None
    pin_host_key(known_hosts_path, endpoint[0], endpoint[1], public_key, host_id=host_id, origin=origin)


def _resolver_with_workspace_on(
    provider_kind: str, agent_id: AgentId, host_id: HostId, name: str
) -> MngrCliBackendResolver:
    agents = [
        {
            "id": str(agent_id),
            "labels": {"is_primary": "true"},
            "host": {"id": str(host_id), "name": name},
            "provider": provider_kind,
        }
    ]
    return make_resolver_with_data(agents_json=json.dumps({"agents": agents}))


def _cloud_resolver_with_workspace(agent_id: AgentId, host_id: HostId, name: str) -> MngrCliBackendResolver:
    return _resolver_with_workspace_on(imbue_cloud_provider_name_for_user(_USER_ID), agent_id, host_id, name)


def _provision_cloud_workspace(
    paths: InstallationPaths, session: MultiAccountSessionStore, profile_dir: Path, cli: FakeImbueCloudCli
) -> tuple[AgentId, HostId, str, str]:
    """Lease a cloud machine on the given device: per-host key on disk, record pushed with full secrets."""
    bundle = set_master_password_for_account(paths, _USER_ID, SecretStr(_PASSWORD))
    assert bundle is not None
    cli.sync_bundle_push(_EMAIL, bundle)

    agent_id = AgentId.generate()
    host_id = HostId.generate()
    private_key = generate_test_ssh_private_key()
    known_hosts_line = f"[198.51.100.7]:22001 ssh-ed25519 AAAATESTPIN{uuid4().hex}"
    key_dir = _cloud_host_key_dir(profile_dir, host_id)
    key_dir.mkdir(parents=True)
    (key_dir / "ssh_key").write_text(private_key)
    (key_dir / "known_hosts").write_text(known_hosts_line + "\n")

    resolver = _cloud_resolver_with_workspace(agent_id, host_id, "cloud-ws")
    session.associate_workspace(_USER_ID, str(agent_id), resolver)
    pushed = cli.sync_records_by_email[_EMAIL][str(agent_id)]
    assert pushed["encrypted_secrets"] is not None
    assert pushed["hosting_device_id"] is None
    return agent_id, host_id, private_key, known_hosts_line


def _provision_cloud_workspace_on_device_a(tmp_path: Path, cli: FakeImbueCloudCli) -> tuple[AgentId, HostId, str, str]:
    """Device A leases a cloud machine: per-host key on disk, record pushed with full secrets."""
    paths_a, _, session_a, profile_a = make_profiled_device_for_test(tmp_path, "laptop", cli)
    return _provision_cloud_workspace(paths_a, session_a, profile_a, cli)


def test_unlock_materializes_cloud_row_ssh_material_on_a_fresh_install(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    _, host_id, private_key, known_hosts_line = _provision_cloud_workspace_on_device_a(tmp_path, cli)

    # Device B: fresh install, pulls the record, unlocks, materializes.
    _, store_b, _, profile_b = make_profiled_device_for_test(tmp_path, "desktop", cli)
    store_b.reconcile({_USER_ID: _EMAIL}, make_resolver_with_data(agents_json=json.dumps({"agents": []})))
    # Still locked: materialization is a no-op.
    assert store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL) is False
    assert store_b.unlock_account(_USER_ID, _EMAIL, SecretStr(_PASSWORD)) is True

    assert store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL) is True

    key_dir_b = _cloud_host_key_dir(profile_b, host_id)
    key_path = key_dir_b / "ssh_key"
    assert key_path.read_text() == private_key
    assert (key_path.stat().st_mode & 0o777) == 0o600
    # The derived public half exists (mngr regenerates the pair when it is missing).
    public_text = (key_dir_b / "ssh_key.pub").read_text()
    assert public_text.startswith("ssh-ed25519 ")
    assert known_hosts_line in (key_dir_b / "known_hosts").read_text()
    # Idempotent: unchanged material reports nothing written.
    assert store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL) is False


def test_materializer_applies_advanced_synced_material_to_a_host_this_install_leased(tmp_path: Path) -> None:
    """A lease held here grants no standing authority once the record advanced past this device.

    Another install can legitimately adopt the host (rotating its sshd keys and
    per-host client key) and push the rotated material; the leasing install
    must converge on it or the host becomes permanently unreachable from here.
    """
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    _, host_id, synced_key, known_hosts_line = _provision_cloud_workspace_on_device_a(tmp_path, cli)

    _, store_b, _, profile_b = make_profiled_device_for_test(tmp_path, "desktop", cli)
    store_b.reconcile({_USER_ID: _EMAIL}, make_resolver_with_data(agents_json=json.dumps({"agents": []})))
    assert store_b.unlock_account(_USER_ID, _EMAIL, SecretStr(_PASSWORD)) is True

    # B holds its own lease for this host (lease.json + its own keypair), but
    # the record carries different, never-applied-here material.
    key_dir_b = _cloud_host_key_dir(profile_b, host_id)
    key_dir_b.mkdir(parents=True)
    (key_dir_b / "ssh_key").write_text(generate_test_ssh_private_key())
    (key_dir_b / "lease.json").write_text("{}")

    assert store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL) is True
    assert (key_dir_b / "ssh_key").read_text() == synced_key
    assert known_hosts_line in (key_dir_b / "known_hosts").read_text()


def test_materializer_keeps_leaseholder_material_the_record_has_not_advanced_past(tmp_path: Path) -> None:
    """The leasing install's local rotation, not yet pushed, is never clobbered by the older record.

    The revision + content-hash gate is what protects it: the record's payload
    is the one this device contributed, so re-applying is not due -- and the
    rotated pin is user-origin, so the bootstrap-drift hatch stays closed too.
    """
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    paths_b, store_b, session_b, profile_b = make_profiled_device_for_test(tmp_path, "desktop", cli)
    _, host_id, _, known_hosts_line = _provision_cloud_workspace(paths_b, session_b, profile_b, cli)

    # B is the leaseholder and contributed the record's current material; it
    # then rotates its key material locally, ahead of any push.
    key_dir_b = _cloud_host_key_dir(profile_b, host_id)
    (key_dir_b / "lease.json").write_text("{}")
    rotated_key = generate_test_ssh_private_key()
    (key_dir_b / "ssh_key").write_text(rotated_key)
    (key_dir_b / "ssh_key.pub").write_text("ssh-ed25519 AAAAROTATEDPUB rotated\n")
    rotated_pin = f"ssh-ed25519 AAAAROTATEDPIN{uuid4().hex}"
    _pin_at_line_endpoint(
        key_dir_b / "known_hosts", known_hosts_line, rotated_pin, host_id=host_id, origin=HostKeyOrigin.USER
    )

    assert store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL) is False
    assert (key_dir_b / "ssh_key").read_text() == rotated_key
    pins = load_current_host_key_pins(key_dir_b / "known_hosts")
    assert [pin.public_key for pin in pins] == [rotated_pin]


def test_materializer_reapplies_record_pins_over_stale_bootstrap_pins_despite_a_stamped_revision(
    tmp_path: Path,
) -> None:
    """Regression: a leased-here host whose apply was skipped by an older client converges anyway.

    Older clients stamped ``last_applied_secrets_revision`` while skipping the
    SSH half for leased-here hosts, leaving only stale bootstrap (bake-time)
    pins behind -- so the revision gate alone would never reopen. The
    bootstrap-drift hatch must detect that the record's pins were never
    absorbed and re-apply them.
    """
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    agent_id, host_id, synced_key, known_hosts_line = _provision_cloud_workspace_on_device_a(tmp_path, cli)

    paths_b, store_b, _, profile_b = make_profiled_device_for_test(tmp_path, "desktop", cli)
    store_b.reconcile({_USER_ID: _EMAIL}, make_resolver_with_data(agents_json=json.dumps({"agents": []})))
    assert store_b.unlock_account(_USER_ID, _EMAIL, SecretStr(_PASSWORD)) is True

    # B leased the host long ago: lease.json, an old keypair, and a stale
    # BOOTSTRAP pin (the connector's bake-time key) at the record's endpoint.
    key_dir_b = _cloud_host_key_dir(profile_b, host_id)
    key_dir_b.mkdir(parents=True)
    (key_dir_b / "ssh_key").write_text(generate_test_ssh_private_key())
    (key_dir_b / "ssh_key.pub").write_text("ssh-ed25519 AAAAOLDPUB old\n")
    (key_dir_b / "lease.json").write_text("{}")
    _pin_at_line_endpoint(
        key_dir_b / "known_hosts",
        known_hosts_line,
        f"ssh-ed25519 AAAABAKETIME{uuid4().hex}",
        host_id=host_id,
        origin=HostKeyOrigin.BOOTSTRAP,
    )

    # Simulate the older client's stamp: revision marked applied with a
    # non-matching parity hash, persisted in the on-disk replica.
    replica_path = paths_b.data_dir / "workspace_records" / f"{_USER_ID}.json"
    replica = json.loads(replica_path.read_text())
    for row in replica["records"]:
        if row["agent_id"] == str(agent_id):
            row["last_applied_secrets_revision"] = row["revision"]
            row["secrets_content_hash"] = "0" * 64
    replica_path.write_text(json.dumps(replica))
    store_fresh = WorkspaceRecordStore(
        paths=paths_b,
        mngr_host_dir=profile_b.parent.parent,
        cli=cli,
        device_id=device_id_for_test("desktop"),
        device_label="desktop",
    )

    assert store_fresh.materialize_account_synced_secrets(_USER_ID, _EMAIL) is True
    assert (key_dir_b / "ssh_key").read_text() == synced_key
    pins = load_current_host_key_pins(key_dir_b / "known_hosts")
    assert [pin.public_key for pin in pins] == [" ".join(known_hosts_line.split()[1:])]
    assert pins[0].origin is HostKeyOrigin.USER


def test_materializer_replaces_a_placeholder_keypair(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    _, host_id, synced_key, _ = _provision_cloud_workspace_on_device_a(tmp_path, cli)

    _, store_b, _, profile_b = make_profiled_device_for_test(tmp_path, "desktop", cli)
    store_b.reconcile({_USER_ID: _EMAIL}, make_resolver_with_data(agents_json=json.dumps({"agents": []})))
    assert store_b.unlock_account(_USER_ID, _EMAIL, SecretStr(_PASSWORD)) is True

    # The provider generated a placeholder pair when it discovered the lease
    # without a local key; no lease.json exists, so the synced key must win.
    key_dir_b = _cloud_host_key_dir(profile_b, host_id)
    key_dir_b.mkdir(parents=True)
    (key_dir_b / "ssh_key").write_text(generate_test_ssh_private_key())

    assert store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL) is True
    assert (key_dir_b / "ssh_key").read_text() == synced_key


def test_sweep_removes_key_dirs_for_tombstoned_records_but_keeps_owned_leases_and_unrecorded_dirs(
    tmp_path: Path,
) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    agent_id, host_id, _, _ = _provision_cloud_workspace_on_device_a(tmp_path, cli)

    _, store_b, _, profile_b = make_profiled_device_for_test(tmp_path, "desktop", cli)
    store_b.reconcile({_USER_ID: _EMAIL}, make_resolver_with_data(agents_json=json.dumps({"agents": []})))
    assert store_b.unlock_account(_USER_ID, _EMAIL, SecretStr(_PASSWORD)) is True
    assert store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL) is True
    key_dir_b = _cloud_host_key_dir(profile_b, host_id)
    assert key_dir_b.is_dir()

    # A dir with no record at all is never swept: absence is not evidence that the host is gone
    # (a long create may not have pushed its record yet).
    unrecorded_dir = _cloud_host_key_dir(profile_b, HostId.generate())
    unrecorded_dir.mkdir(parents=True)
    (unrecorded_dir / "ssh_key").write_text(generate_test_ssh_private_key())
    store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL)
    assert unrecorded_dir.is_dir()

    # The workspace is destroyed from another install; B pulls the tombstone and sweeps the dir at once.
    server_record = cli.sync_records_by_email[_EMAIL][str(agent_id)]
    server_record["state"] = "destroyed"
    store_b.reconcile({_USER_ID: _EMAIL}, make_resolver_with_data(agents_json=json.dumps({"agents": []})))
    store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL)
    assert not key_dir_b.exists()
    assert unrecorded_dir.is_dir()

    # A tombstoned row whose dir still holds a lease this device owns is kept.
    key_dir_b.mkdir(parents=True)
    (key_dir_b / "lease.json").write_text("{}")
    store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL)
    assert key_dir_b.is_dir()


def test_sweep_removes_a_tombstoned_connection_workspaces_key_dir_in_its_own_layout(tmp_path: Path) -> None:
    """A Modal workspace destroyed on device A loses its per-host dir under the connection instance on device B."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    paths_a, store_a, session_a, connections_a, profile_a = make_profiled_device_with_connections_for_test(
        tmp_path, "laptop", cli
    )
    bundle = set_master_password_for_account(paths_a, _USER_ID, SecretStr(_PASSWORD))
    assert bundle is not None
    cli.sync_bundle_push(_EMAIL, bundle)
    connection_id = add_modal_connection_for_test(connections_a, _USER_ID, "team modal")
    connections_a.push_connection(_USER_ID, _EMAIL, str(connection_id))
    instance = provider_instance_name_for_connection(ConnectionBackend.MODAL, connection_id)
    agent_id = AgentId.generate()
    host_id = HostId.generate()
    paths_on_a = host_access_paths(profile_a, instance, host_id)
    assert paths_on_a is not None
    paths_on_a.per_host_dir.mkdir(parents=True)
    (paths_on_a.per_host_dir / "modal_ssh_key").write_text("MODAL-CLIENT-KEY")
    (paths_on_a.per_host_dir / "host_key").write_text("MODAL-HOST-KEY")
    session_a.associate_workspace(
        _USER_ID, str(agent_id), _resolver_with_workspace_on(instance, agent_id, host_id, "ws")
    )

    _, store_b, _, connections_b, profile_b = make_profiled_device_with_connections_for_test(tmp_path, "desktop", cli)
    assert connections_b.pull(_USER_ID, _EMAIL).is_changed
    store_b.reconcile({_USER_ID: _EMAIL}, make_resolver_with_data(agents_json=json.dumps({"agents": []})))
    assert store_b.unlock_account(_USER_ID, _EMAIL, SecretStr(_PASSWORD)) is True
    assert store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL) is True
    paths_on_b = host_access_paths(profile_b, instance, host_id)
    assert paths_on_b is not None
    assert (paths_on_b.per_host_dir / "modal_ssh_key").is_file()

    # Device A destroys the workspace; the next sync on B tombstones the row and sweeps the dir.
    store_a.tombstone_record(_USER_ID, _EMAIL, str(agent_id))
    store_b.reconcile({_USER_ID: _EMAIL}, make_resolver_with_data(agents_json=json.dumps({"agents": []})))
    store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL)
    assert not paths_on_b.per_host_dir.exists()


def test_sweep_leaves_a_device_hosted_rows_key_dir_alone_when_its_row_is_tombstoned(tmp_path: Path) -> None:
    """A lima dir lives on its hosting device alone and is never re-laid from the record, so a tombstone
    (which local discovery may still overturn) must not cost the live host its only client key."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    paths_a, store_a, session_a, _, profile_a = make_profiled_device_with_connections_for_test(tmp_path, "laptop", cli)
    bundle = set_master_password_for_account(paths_a, _USER_ID, SecretStr(_PASSWORD))
    assert bundle is not None
    cli.sync_bundle_push(_EMAIL, bundle)
    agent_id = AgentId.generate()
    host_id = HostId.generate()
    lima_paths = host_access_paths(profile_a, "lima", host_id)
    assert lima_paths is not None
    lima_paths.per_host_dir.mkdir(parents=True)
    (lima_paths.per_host_dir / "root_ssh_key").write_text("LIMA-ROOT-KEY")
    session_a.associate_workspace(
        _USER_ID, str(agent_id), _resolver_with_workspace_on("lima", agent_id, host_id, "ws")
    )

    store_a.tombstone_record(_USER_ID, _EMAIL, str(agent_id))
    store_a.materialize_account_synced_secrets(_USER_ID, _EMAIL)

    assert (lima_paths.per_host_dir / "root_ssh_key").is_file()


def test_sweep_leaves_a_device_hosted_connection_rows_key_dir_alone_when_its_row_is_tombstoned(
    tmp_path: Path,
) -> None:
    """A row on a local-profile connection is hosted by this device: local discovery alone tombstones it
    and may still resurrect it, so its per-host dir (which the provider's own destroy removes) is left."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    paths_a, store_a, session_a, connections_a, profile_a = make_profiled_device_with_connections_for_test(
        tmp_path, "laptop", cli
    )
    bundle = set_master_password_for_account(paths_a, _USER_ID, SecretStr(_PASSWORD))
    assert bundle is not None
    cli.sync_bundle_push(_EMAIL, bundle)
    connection_id = add_modal_connection_for_test(connections_a, LOCAL_PROFILE_USER_ID, "this device only")
    instance = provider_instance_name_for_connection(ConnectionBackend.MODAL, connection_id)
    agent_id = AgentId.generate()
    host_id = HostId.generate()
    modal_paths = host_access_paths(profile_a, instance, host_id)
    assert modal_paths is not None
    modal_paths.per_host_dir.mkdir(parents=True)
    (modal_paths.per_host_dir / "modal_ssh_key").write_text("MODAL-CLIENT-KEY")
    session_a.associate_workspace(
        _USER_ID, str(agent_id), _resolver_with_workspace_on(instance, agent_id, host_id, "ws")
    )
    found = store_a.find_record_any_state(str(agent_id))
    assert found is not None and found[1].hosting_device_id == store_a.device_id

    store_a.tombstone_record(_USER_ID, _EMAIL, str(agent_id))
    store_a.materialize_account_synced_secrets(_USER_ID, _EMAIL)

    assert (modal_paths.per_host_dir / "modal_ssh_key").is_file()


def test_producer_repushes_secrets_when_the_material_changes(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    paths_a, store_a, session_a, profile_a = make_profiled_device_for_test(tmp_path, "laptop", cli)
    bundle = set_master_password_for_account(paths_a, _USER_ID, SecretStr(_PASSWORD))
    assert bundle is not None
    cli.sync_bundle_push(_EMAIL, bundle)

    agent_id = AgentId.generate()
    host_id = HostId.generate()
    write_canonical_env(paths_a, agent_id, "RESTIC_REPOSITORY=s3:v1\n")
    resolver_a = _cloud_resolver_with_workspace(agent_id, host_id, "cloud-ws")
    session_a.associate_workspace(_USER_ID, str(agent_id), resolver_a)
    revision_before = int(str(cli.sync_records_by_email[_EMAIL][str(agent_id)]["revision"]))

    # Unchanged material: a reconcile pushes nothing new.
    store_a.reconcile({_USER_ID: _EMAIL}, resolver_a)
    assert int(str(cli.sync_records_by_email[_EMAIL][str(agent_id)]["revision"])) == revision_before

    # The backup env rotates; the next reconcile re-pushes the secrets.
    write_canonical_env(paths_a, agent_id, "RESTIC_REPOSITORY=s3:v2-rotated\n")
    store_a.reconcile({_USER_ID: _EMAIL}, resolver_a)
    assert int(str(cli.sync_records_by_email[_EMAIL][str(agent_id)]["revision"])) > revision_before

    # A fresh install decrypts the rotated env.
    paths_b, store_b, _, _ = make_profiled_device_for_test(tmp_path, "desktop", cli)
    store_b.reconcile({_USER_ID: _EMAIL}, make_resolver_with_data(agents_json=json.dumps({"agents": []})))
    assert store_b.unlock_account(_USER_ID, _EMAIL, SecretStr(_PASSWORD)) is True
    records_b = store_b.list_records(_USER_ID)
    assert len(records_b) == 1
    payload = store_b.decrypt_record_secrets(_USER_ID, records_b[0])
    assert payload is not None
    assert payload.restic_env == "RESTIC_REPOSITORY=s3:v2-rotated\n"


def test_non_contributor_never_clobbers_anothers_secrets_with_partial_material(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    agent_id, host_id, _, _ = _provision_cloud_workspace_on_device_a(tmp_path, cli)
    blob_before = cli.sync_records_by_email[_EMAIL][str(agent_id)]["encrypted_secrets"]

    # Device B is unlocked, sees the cloud workspace in its own discovery, and
    # holds only PARTIAL local material (a backup env, no SSH key). Its
    # reconcile must not replace the record's full secrets with that view.
    paths_b, store_b, _, _ = make_profiled_device_for_test(tmp_path, "desktop", cli)
    store_b.reconcile({_USER_ID: _EMAIL}, make_resolver_with_data(agents_json=json.dumps({"agents": []})))
    assert store_b.unlock_account(_USER_ID, _EMAIL, SecretStr(_PASSWORD)) is True
    write_canonical_env(paths_b, agent_id, "RESTIC_REPOSITORY=s3:partial-view\n")

    resolver_b = _cloud_resolver_with_workspace(agent_id, host_id, "cloud-ws")
    store_b.reconcile({_USER_ID: _EMAIL}, resolver_b)

    assert cli.sync_records_by_email[_EMAIL][str(agent_id)]["encrypted_secrets"] == blob_before


def test_desktop_push_in_a_cas_window_rebases_once_and_converges(tmp_path: Path) -> None:
    """Desktop and web editing one record inside one CAS window converges.

    The web client edited the record (bumping the server revision) between the
    desktop's read and its push. The desktop's push conflicts, rebases once
    onto the stored revision, and lands -- last actor wins outright for the
    desktop (the documented ``_push_record`` semantics: its pushes come from
    synchronous user actions), so the desktop's field values replace the
    web's, and the replica converges on the server revision with no dirty row
    left behind.
    """
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    paths_a, store_a, session_a = _make_device(tmp_path, "laptop", cli)

    agent_id = AgentId.generate()
    host_id = HostId.generate()
    resolver = _resolver_with_workspace(agent_id, host_id, "crossed-edit-ws")
    session_a.associate_workspace(_USER_ID, str(agent_id), resolver)
    server_rows = cli.sync_records_by_email[_EMAIL]
    assert int(str(server_rows[str(agent_id)]["revision"])) == 1

    # The web chrome edits the record concurrently: revision 2 with a color
    # the desktop's replica has never seen.
    server_rows[str(agent_id)] = {
        **server_rows[str(agent_id)],
        "revision": 2,
        "color": "#ff0000",
        "display_name": "web-name",
    }

    # The desktop renames from its stale replica (still at revision 1): the
    # push conflicts, rebases onto the stored revision, and wins.
    stale_record = next(record for record in store_a.list_records(_USER_ID) if record.host_id == str(host_id))
    assert stale_record.revision == 1
    renamed = stale_record.model_copy_update(
        to_update(stale_record.field_ref().display_name, "desktop-name"),
    )
    store_a.upsert_local_record(_USER_ID, _EMAIL, renamed)

    stored_row = server_rows[str(agent_id)]
    assert int(str(stored_row["revision"])) == 3
    assert stored_row["display_name"] == "desktop-name"
    # Last-actor-wins is deliberate: the desktop's full local content replaced
    # the web's edit (its color went with it). The web side is the merging
    # side -- pushRecordWithCas re-applies its edit over the stored row.
    assert stored_row["color"] is None

    # The replica converged: server revision acknowledged, nothing dirty.
    converged = next(record for record in store_a.list_records(_USER_ID) if record.host_id == str(host_id))
    assert converged.revision == 3
    assert converged.is_dirty is False


def test_import_applies_synced_pins_as_user_origin_through_the_store(tmp_path: Path) -> None:
    """Materialized pins are store-backed user-origin material: a later local
    bootstrap write (e.g. a connector-fed lease-time pin) can never displace them."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    _, host_id, _, known_hosts_line = _provision_cloud_workspace_on_device_a(tmp_path, cli)

    _, store_b, _, profile_b = make_profiled_device_for_test(tmp_path, "desktop", cli)
    store_b.reconcile({_USER_ID: _EMAIL}, make_resolver_with_data(agents_json=json.dumps({"agents": []})))
    assert store_b.unlock_account(_USER_ID, _EMAIL, SecretStr(_PASSWORD)) is True
    assert store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL) is True

    known_hosts_path = _cloud_host_key_dir(profile_b, host_id) / "known_hosts"
    assert known_hosts_line in known_hosts_path.read_text()
    record = load_host_key_record(known_hosts_path, host_id)
    assert record is not None
    assert [pin.origin for pin in record.pins] == [HostKeyOrigin.USER]

    # A bootstrap-origin write for the same endpoint bounces off the user pin.
    pin_host_key(known_hosts_path, "198.51.100.7", 22001, "ssh-ed25519 AAAABAKEKEY", host_id, HostKeyOrigin.BOOTSTRAP)
    assert known_hosts_line in known_hosts_path.read_text()
    assert "AAAABAKEKEY" not in known_hosts_path.read_text()


def test_import_is_revision_gated_so_an_unchanged_record_never_clobbers_newer_local_pins(tmp_path: Path) -> None:
    """After a record's pins are applied once, re-materializing the same revision is a
    no-op -- so a locally-rotated (newer user-origin) pin survives every later pass
    until the record actually advances."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    _, host_id, _, known_hosts_line = _provision_cloud_workspace_on_device_a(tmp_path, cli)

    _, store_b, _, profile_b = make_profiled_device_for_test(tmp_path, "desktop", cli)
    store_b.reconcile({_USER_ID: _EMAIL}, make_resolver_with_data(agents_json=json.dumps({"agents": []})))
    assert store_b.unlock_account(_USER_ID, _EMAIL, SecretStr(_PASSWORD)) is True
    assert store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL) is True

    # B rotates the endpoint's key locally (newer user-origin material).
    known_hosts_path = _cloud_host_key_dir(profile_b, host_id) / "known_hosts"
    rotated_key = f"ssh-ed25519 AAAAROTATED{uuid4().hex}"
    pin_host_key(known_hosts_path, "198.51.100.7", 22001, rotated_key, host_id, HostKeyOrigin.USER)

    # The record has not advanced: another pass must not re-apply its old pin.
    assert store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL) is False
    content = known_hosts_path.read_text()
    assert rotated_key in content
    assert known_hosts_line not in content


def test_a_metadata_only_revision_advance_does_not_clobber_a_local_rotation(tmp_path: Path) -> None:
    """A rename pushed from another device advances the revision while carrying the
    unchanged secrets blob -- re-applying that payload must not displace a rotation
    this device ran that the record has not caught up to (the old key would brick
    access: the host already serves the rotated one)."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    agent_id, host_id, _, old_line = _provision_cloud_workspace_on_device_a(tmp_path, cli)

    _, store_b, _, profile_b = make_profiled_device_for_test(tmp_path, "desktop", cli)
    store_b.reconcile({_USER_ID: _EMAIL}, make_resolver_with_data(agents_json=json.dumps({"agents": []})))
    assert store_b.unlock_account(_USER_ID, _EMAIL, SecretStr(_PASSWORD)) is True
    assert store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL) is True

    # B rotates locally: new client key on disk, new user-origin endpoint pin.
    key_dir_b = _cloud_host_key_dir(profile_b, host_id)
    rotated_private_key = generate_test_ssh_private_key()
    (key_dir_b / "ssh_key").write_text(rotated_private_key)
    rotated_pin = f"ssh-ed25519 AAAAROTATED{uuid4().hex}"
    pin_host_key(key_dir_b / "known_hosts", "198.51.100.7", 22001, rotated_pin, host_id, HostKeyOrigin.USER)

    # Another device pushes a rename: the revision advances, the secrets do not.
    wire = cli.sync_records_by_email[_EMAIL][str(agent_id)]
    wire["display_name"] = "renamed-elsewhere"
    wire["revision"] = int(str(wire["revision"])) + 1

    # B's next pull + materialize must skip the unchanged payload outright.
    store_b.reconcile({_USER_ID: _EMAIL}, make_resolver_with_data(agents_json=json.dumps({"agents": []})))
    assert store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL) is False
    assert (key_dir_b / "ssh_key").read_text() == rotated_private_key
    content = (key_dir_b / "known_hosts").read_text()
    assert rotated_pin in content
    assert old_line not in content


def test_a_drifted_local_env_is_converged_to_the_record_and_never_pushed(tmp_path: Path) -> None:
    """A device holding an env that differs from the record's converges to the
    record on its first gated apply (record-wins for material this device did
    not produce), and its reconcile pushes nothing -- the stale view can
    neither linger locally nor clobber the record."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)

    # Device A provisions the cloud workspace, then adds a backup env to the record.
    paths_a, store_a, session_a, profile_a = make_profiled_device_for_test(tmp_path, "laptop", cli)
    agent_id, host_id, _, _ = _provision_cloud_workspace(paths_a, session_a, profile_a, cli)
    resolver_a = _cloud_resolver_with_workspace(agent_id, host_id, "cloud-ws")
    record_env = "RESTIC_REPOSITORY=s3:https://r2.example/bucket\nRESTIC_PASSWORD=record-pass\n"
    write_canonical_env(paths_a, agent_id, record_env)
    store_a.reconcile({_USER_ID: _EMAIL}, resolver_a)

    # Device B already holds a different local env for this workspace.
    paths_b, store_b, _, _ = make_profiled_device_for_test(tmp_path, "desktop", cli)
    resolver_b = _cloud_resolver_with_workspace(agent_id, host_id, "cloud-ws")
    store_b.reconcile({_USER_ID: _EMAIL}, resolver_b)
    assert store_b.unlock_account(_USER_ID, _EMAIL, SecretStr(_PASSWORD)) is True
    write_canonical_env(paths_b, agent_id, "RESTIC_REPOSITORY=s3:elsewhere\nRESTIC_PASSWORD=drifted\n")
    assert store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL) is True

    # B's drifted env was replaced by the record's.
    assert read_canonical_env(paths_b, agent_id) == record_env

    # And B's reconcile pushes nothing: local now equals the record.
    revision_before = int(str(cli.sync_records_by_email[_EMAIL][str(agent_id)]["revision"]))
    store_b.reconcile({_USER_ID: _EMAIL}, resolver_b)
    assert int(str(cli.sync_records_by_email[_EMAIL][str(agent_id)]["revision"])) == revision_before
    synced = next(record for record in store_b.list_records(_USER_ID) if record.host_id == str(host_id))
    payload = store_b.decrypt_record_secrets(_USER_ID, synced)
    assert payload is not None
    assert payload.restic_env == record_env


def test_a_locally_newer_env_is_never_clobbered_and_propagates_outward(tmp_path: Path) -> None:
    """The env producer's protection: after a device re-provisions backups
    locally, the (older) record payload matches its parity stamp, so the gate
    stays closed and materialization cannot overwrite the fresh env -- and the
    device's next reconcile pushes it, converging the OTHER devices instead."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)

    # Device A provisions the workspace with env v1; device B reaches parity.
    paths_a, store_a, session_a, profile_a = make_profiled_device_for_test(tmp_path, "laptop", cli)
    agent_id, host_id, _, _ = _provision_cloud_workspace(paths_a, session_a, profile_a, cli)
    resolver_a = _cloud_resolver_with_workspace(agent_id, host_id, "cloud-ws")
    write_canonical_env(paths_a, agent_id, "RESTIC_REPOSITORY=s3:v1\nRESTIC_PASSWORD=one\n")
    store_a.reconcile({_USER_ID: _EMAIL}, resolver_a)
    paths_b, store_b, _, _ = make_profiled_device_for_test(tmp_path, "desktop", cli)
    resolver_b = _cloud_resolver_with_workspace(agent_id, host_id, "cloud-ws")
    store_b.reconcile({_USER_ID: _EMAIL}, resolver_b)
    assert store_b.unlock_account(_USER_ID, _EMAIL, SecretStr(_PASSWORD)) is True
    assert store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL) is True

    # B re-provisions backups locally (env v2). A materialize pass running
    # before any reconcile must not clobber it with the record's v1.
    new_env = "RESTIC_REPOSITORY=s3:v2-reprovisioned\nRESTIC_PASSWORD=two\n"
    write_canonical_env(paths_b, agent_id, new_env)
    store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL)
    assert read_canonical_env(paths_b, agent_id) == new_env

    # B's reconcile re-pushes the record; A converges on its next pass.
    store_b.reconcile({_USER_ID: _EMAIL}, resolver_b)
    store_a.reconcile({_USER_ID: _EMAIL}, resolver_a)
    store_a.materialize_account_synced_secrets(_USER_ID, _EMAIL)
    assert read_canonical_env(paths_a, agent_id) == new_env


def test_env_convergence_covers_rows_hosted_on_another_device(tmp_path: Path) -> None:
    """Env convergence is not cloud-only: a device that materialized a
    locally-hosted (e.g. lima) row's env for backup access picks up a rotated
    env from the record, even though the row has no SSH half here."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)

    # Device A hosts a local-provider workspace with a backup env and syncs it.
    paths_a, store_a, session_a = _make_device(tmp_path, "laptop", cli)
    bundle = set_master_password_for_account(paths_a, _USER_ID, SecretStr(_PASSWORD))
    assert bundle is not None
    cli.sync_bundle_push(_EMAIL, bundle)
    agent_id = AgentId.generate()
    host_id = HostId.generate()
    write_canonical_env(paths_a, agent_id, "RESTIC_REPOSITORY=s3:v1\nRESTIC_PASSWORD=one\n")
    resolver_a = _resolver_with_workspace(agent_id, host_id, "lima-ws")
    session_a.associate_workspace(_USER_ID, str(agent_id), resolver_a)

    # Device B pulls, unlocks, and materializes the env (write-if-missing path).
    paths_b, store_b, _ = _make_device(tmp_path, "desktop", cli)
    empty_resolver = make_resolver_with_data(agents_json=json.dumps({"agents": []}))
    store_b.reconcile({_USER_ID: _EMAIL}, empty_resolver)
    assert store_b.unlock_account(_USER_ID, _EMAIL, SecretStr(_PASSWORD)) is True
    store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL)
    assert read_canonical_env(paths_b, agent_id) is not None

    # A rotates the backup env and re-pushes; B converges on its next pass.
    rotated_env = "RESTIC_REPOSITORY=s3:v2-rotated\nRESTIC_PASSWORD=two\n"
    write_canonical_env(paths_a, agent_id, rotated_env)
    store_a.reconcile({_USER_ID: _EMAIL}, resolver_a)
    store_b.reconcile({_USER_ID: _EMAIL}, empty_resolver)
    store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL)
    assert read_canonical_env(paths_b, agent_id) == rotated_env


def test_import_reapplies_pins_when_the_rendered_known_hosts_file_went_missing(tmp_path: Path) -> None:
    """The revision gate has a missing-file escape hatch: a wiped known_hosts file is
    restored from the record even though the revision has not advanced."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    _, host_id, _, known_hosts_line = _provision_cloud_workspace_on_device_a(tmp_path, cli)

    _, store_b, _, profile_b = make_profiled_device_for_test(tmp_path, "desktop", cli)
    store_b.reconcile({_USER_ID: _EMAIL}, make_resolver_with_data(agents_json=json.dumps({"agents": []})))
    assert store_b.unlock_account(_USER_ID, _EMAIL, SecretStr(_PASSWORD)) is True
    assert store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL) is True

    known_hosts_path = _cloud_host_key_dir(profile_b, host_id) / "known_hosts"
    known_hosts_path.unlink()
    host_key_store_path(known_hosts_path).unlink()

    assert store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL) is True
    assert known_hosts_line in known_hosts_path.read_text()


def test_hosting_device_pin_rotation_replaces_pins_on_other_devices_on_the_next_pull(tmp_path: Path) -> None:
    """The whole cross-device rotation story: the hosting device re-pins an endpoint
    (user-origin), its reconcile re-pushes the record (revision advances), and the
    other device's next materialize pass replaces its same-endpoint pin."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)

    # Device A provisions the cloud workspace (device made here so its store stays in hand).
    paths_a, store_a, session_a, profile_a = make_profiled_device_for_test(tmp_path, "laptop", cli)
    agent_id, host_id, _, old_line = _provision_cloud_workspace(paths_a, session_a, profile_a, cli)
    key_dir_a = _cloud_host_key_dir(profile_a, host_id)
    resolver_a = _cloud_resolver_with_workspace(agent_id, host_id, "cloud-ws")

    # Device B pulls, unlocks, and materializes the original pin.
    _, store_b, _, profile_b = make_profiled_device_for_test(tmp_path, "desktop", cli)
    store_b.reconcile({_USER_ID: _EMAIL}, make_resolver_with_data(agents_json=json.dumps({"agents": []})))
    assert store_b.unlock_account(_USER_ID, _EMAIL, SecretStr(_PASSWORD)) is True
    assert store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL) is True
    known_hosts_path_b = _cloud_host_key_dir(profile_b, host_id) / "known_hosts"
    assert old_line in known_hosts_path_b.read_text()

    # A rotates the endpoint's pin; its reconcile re-pushes the changed material.
    new_key = f"ssh-ed25519 AAAANEWKEY{uuid4().hex}"
    pin_host_key(key_dir_a / "known_hosts", "198.51.100.7", 22001, new_key, host_id, HostKeyOrigin.USER)
    store_a.reconcile({_USER_ID: _EMAIL}, resolver_a)

    # B's next pull + materialize converges on the rotated pin.
    store_b.reconcile({_USER_ID: _EMAIL}, make_resolver_with_data(agents_json=json.dumps({"agents": []})))
    assert store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL) is True
    content_b = known_hosts_path_b.read_text()
    assert new_key in content_b
    assert old_line not in content_b


def test_rotation_run_on_a_non_leasing_device_propagates_through_the_record(tmp_path: Path) -> None:
    """The lost-device healing path: a healthy device that only ever *materialized* a
    cloud workspace's material rotates its keys locally (what `mngr imbue_cloud hosts
    rotate` leaves on disk) and its next reconcile re-pushes the record -- the
    materialization parity stamp is what makes that device eligible to push. A third
    fresh device then converges on the rotated material."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    agent_id, host_id, _, old_line = _provision_cloud_workspace_on_device_a(tmp_path, cli)

    # Device B materializes the full material (and with it the parity stamp).
    _, store_b, _, profile_b = make_profiled_device_for_test(tmp_path, "desktop", cli)
    resolver_b = _cloud_resolver_with_workspace(agent_id, host_id, "cloud-ws")
    store_b.reconcile({_USER_ID: _EMAIL}, resolver_b)
    assert store_b.unlock_account(_USER_ID, _EMAIL, SecretStr(_PASSWORD)) is True
    assert store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL) is True
    revision_before = int(str(cli.sync_records_by_email[_EMAIL][str(agent_id)]["revision"]))

    # A stable pass first: parity means nothing to push.
    store_b.reconcile({_USER_ID: _EMAIL}, resolver_b)
    assert int(str(cli.sync_records_by_email[_EMAIL][str(agent_id)]["revision"])) == revision_before

    # B rotates locally: new client key on disk, new user-origin endpoint pin.
    key_dir_b = _cloud_host_key_dir(profile_b, host_id)
    rotated_private_key = generate_test_ssh_private_key()
    (key_dir_b / "ssh_key").write_text(rotated_private_key)
    rotated_pin = f"ssh-ed25519 AAAAROTATEDHOSTKEY{uuid4().hex}"
    pin_host_key(key_dir_b / "known_hosts", "198.51.100.7", 22001, rotated_pin, host_id, HostKeyOrigin.USER)

    # B's next reconcile detects the drift from its parity stamp and re-pushes.
    store_b.reconcile({_USER_ID: _EMAIL}, resolver_b)
    assert int(str(cli.sync_records_by_email[_EMAIL][str(agent_id)]["revision"])) > revision_before

    # A fresh third device materializes the rotated material, not the original.
    _, store_c, _, profile_c = make_profiled_device_for_test(tmp_path, "tablet", cli)
    store_c.reconcile({_USER_ID: _EMAIL}, make_resolver_with_data(agents_json=json.dumps({"agents": []})))
    assert store_c.unlock_account(_USER_ID, _EMAIL, SecretStr(_PASSWORD)) is True
    assert store_c.materialize_account_synced_secrets(_USER_ID, _EMAIL) is True
    key_dir_c = _cloud_host_key_dir(profile_c, host_id)
    assert (key_dir_c / "ssh_key").read_text() == rotated_private_key
    content_c = (key_dir_c / "known_hosts").read_text()
    assert rotated_pin in content_c
    assert old_line not in content_c


def test_an_account_with_only_a_synced_connection_counts_as_locked_on_a_fresh_device(tmp_path: Path) -> None:
    """A fresh device that has pulled the account's connection but no workspace record or bundle mirror
    still reports the account locked, so the unlock route installs the DEK and the connection opens."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    paths_a, _, _, connections_a, _ = make_profiled_device_with_connections_for_test(tmp_path, "laptop", cli)
    bundle = set_master_password_for_account(paths_a, _USER_ID, SecretStr(_PASSWORD))
    assert bundle is not None
    cli.sync_bundle_push(_EMAIL, bundle)
    connection_id = add_modal_connection_for_test(connections_a, _USER_ID, "team modal")
    connections_a.push_connection(_USER_ID, _EMAIL, str(connection_id))

    paths_b, store_b, _, connections_b, _ = make_profiled_device_with_connections_for_test(tmp_path, "desktop", cli)
    assert connections_b.pull(_USER_ID, _EMAIL).is_changed
    (record_b,) = connections_b.list_connections(_USER_ID)
    assert store_b.list_records(_USER_ID) == []
    assert not is_master_password_set_for_account(paths_b, _USER_ID)

    assert store_b.locked_account_user_ids([_USER_ID]) == [_USER_ID]
    assert connections_b.availability(_USER_ID, record_b) is ConnectionAvailability.LOCKED

    assert store_b.unlock_account(_USER_ID, _EMAIL, SecretStr(_PASSWORD)) is True

    assert store_b.locked_account_user_ids([_USER_ID]) == []
    assert connections_b.availability(_USER_ID, record_b) is ConnectionAvailability.AVAILABLE


# Host access material for every backend (payload format 2)


def test_a_workspace_on_an_account_owned_connection_reaches_another_device(tmp_path: Path) -> None:
    """Device A creates a Modal workspace on a synced connection; device B pulls the connection, unlocks,
    and gets the host's per-host keys and pins laid down under its own copy of the connection instance."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    paths_a, store_a, session_a, connections_a, profile_a = make_profiled_device_with_connections_for_test(
        tmp_path, "laptop", cli
    )
    bundle = set_master_password_for_account(paths_a, _USER_ID, SecretStr(_PASSWORD))
    assert bundle is not None
    cli.sync_bundle_push(_EMAIL, bundle)
    connection_id = add_modal_connection_for_test(connections_a, _USER_ID, "team modal")
    connections_a.push_connection(_USER_ID, _EMAIL, str(connection_id))
    instance = provider_instance_name_for_connection(ConnectionBackend.MODAL, connection_id)
    agent_id = AgentId.generate()
    host_id = HostId.generate()
    paths_on_a = host_access_paths(profile_a, instance, host_id)
    assert paths_on_a is not None
    paths_on_a.per_host_dir.mkdir(parents=True)
    (paths_on_a.per_host_dir / "modal_ssh_key").write_text("MODAL-CLIENT-KEY")
    (paths_on_a.per_host_dir / "modal_ssh_key.pub").write_text("ssh-ed25519 AAAA-client")
    (paths_on_a.per_host_dir / "host_key").write_text("MODAL-HOST-KEY")
    pin_host_key(
        paths_on_a.pin_store_by_name["known_hosts"],
        "sandbox.modal.host",
        2222,
        "ssh-ed25519 AAAA-sandbox",
        host_id,
        HostKeyOrigin.USER,
    )

    session_a.associate_workspace(
        _USER_ID, str(agent_id), _resolver_with_workspace_on(instance, agent_id, host_id, "ws")
    )

    pushed = cli.sync_records_by_email[_EMAIL][str(agent_id)]
    # The row is account-synced: any device that unlocks the account may reach it.
    assert pushed["hosting_device_id"] is None
    record_a = store_a.list_records(_USER_ID)[0]
    payload_a = store_a.decrypt_record_secrets(_USER_ID, record_a)
    assert payload_a is not None and payload_a.host_access is not None
    assert payload_a.payload_format == SUPPORTED_PAYLOAD_FORMAT
    assert payload_a.host_access.layout == "modal"
    assert payload_a.host_access.files["modal_ssh_key"] == "MODAL-CLIENT-KEY"
    assert payload_a.ssh_private_key is None

    # Device B: pulls the connection and the record, unlocks, materializes.
    _, store_b, _, connections_b, profile_b = make_profiled_device_with_connections_for_test(tmp_path, "desktop", cli)
    assert connections_b.pull(_USER_ID, _EMAIL).is_changed
    store_b.reconcile({_USER_ID: _EMAIL}, make_resolver_with_data(agents_json=json.dumps({"agents": []})))
    assert store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL) is False
    assert store_b.unlock_account(_USER_ID, _EMAIL, SecretStr(_PASSWORD)) is True

    assert store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL) is True

    paths_on_b = host_access_paths(profile_b, instance, host_id)
    assert paths_on_b is not None
    assert (paths_on_b.per_host_dir / "modal_ssh_key").read_text() == "MODAL-CLIENT-KEY"
    assert (paths_on_b.per_host_dir / "modal_ssh_key").stat().st_mode & 0o777 == 0o600
    assert (paths_on_b.per_host_dir / "host_key").read_text() == "MODAL-HOST-KEY"
    assert (paths_on_b.per_host_dir / "known_hosts").is_symlink()
    record_b = load_host_key_record(paths_on_b.pin_store_by_name["known_hosts"], host_id)
    assert record_b is not None and [pin.public_key for pin in record_b.pins] == ["ssh-ed25519 AAAA-sandbox"]
    assert record_b.pins[0].origin is HostKeyOrigin.USER
    assert store_b.host_access_key_path(_USER_ID, store_b.list_records(_USER_ID)[0]) == (
        paths_on_b.per_host_dir / "modal_ssh_key"
    )
    # Idempotent.
    assert store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL) is False


def test_a_device_hosted_rows_material_rides_the_record_but_is_not_laid_down_elsewhere(tmp_path: Path) -> None:
    """A lima workspace's per-host material is carried (for a later cross-device connection) while its row
    stays attributed to the hosting device, so another device materializes nothing for it."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    paths_a, _, session_a, _, profile_a = make_profiled_device_with_connections_for_test(tmp_path, "laptop", cli)
    bundle = set_master_password_for_account(paths_a, _USER_ID, SecretStr(_PASSWORD))
    assert bundle is not None
    cli.sync_bundle_push(_EMAIL, bundle)
    agent_id = AgentId.generate()
    host_id = HostId.generate()
    lima_paths = host_access_paths(profile_a, "lima", host_id)
    assert lima_paths is not None
    lima_paths.per_host_dir.mkdir(parents=True)
    (lima_paths.per_host_dir / "root_ssh_key").write_text("LIMA-ROOT-KEY")
    (lima_paths.per_host_dir / "ssh_host_ed25519_key.pub").write_text("ssh-ed25519 AAAA-vm")
    # The provider-wide legacy root key must never be collected.
    (lima_paths.per_host_dir.parent.parent / "root_ssh_key").write_text("PROVIDER-WIDE-KEY")

    session_a.associate_workspace(
        _USER_ID, str(agent_id), _resolver_with_workspace_on("lima", agent_id, host_id, "ws")
    )

    pushed = cli.sync_records_by_email[_EMAIL][str(agent_id)]
    assert pushed["hosting_device_id"] == str(device_id_for_test("laptop"))
    _, store_b, _, _, profile_b = make_profiled_device_with_connections_for_test(tmp_path, "desktop", cli)
    store_b.reconcile({_USER_ID: _EMAIL}, make_resolver_with_data(agents_json=json.dumps({"agents": []})))
    assert store_b.unlock_account(_USER_ID, _EMAIL, SecretStr(_PASSWORD)) is True
    record_b = store_b.list_records(_USER_ID)[0]
    payload_b = store_b.decrypt_record_secrets(_USER_ID, record_b)
    assert payload_b is not None and payload_b.host_access is not None
    assert payload_b.host_access.layout == "lima"
    assert payload_b.host_access.files == {
        "root_ssh_key": "LIMA-ROOT-KEY",
        "ssh_host_ed25519_key.pub": "ssh-ed25519 AAAA-vm",
    }

    assert store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL) is False

    assert not (profile_b / "providers" / "lima").exists()


def test_a_format_1_imbue_cloud_payload_still_materializes_after_the_format_bump(tmp_path: Path) -> None:
    """A record pushed by a pre-format-2 client carries only the imbue_cloud key fields; they still land."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    paths_a, store_a, _, _ = make_profiled_device_for_test(tmp_path, "laptop", cli)
    bundle = set_master_password_for_account(paths_a, _USER_ID, SecretStr(_PASSWORD))
    assert bundle is not None
    cli.sync_bundle_push(_EMAIL, bundle)
    dek = load_dek(paths_a, _USER_ID)
    assert dek is not None
    agent_id = AgentId.generate()
    host_id = HostId.generate()
    private_key = generate_test_ssh_private_key()
    known_hosts_line = f"[198.51.100.7]:22001 ssh-ed25519 AAAATESTPIN{uuid4().hex}"
    legacy_payload = {"payload_format": 1, "ssh_private_key": private_key, "ssh_known_hosts": known_hosts_line + "\n"}
    store_a.upsert_local_record(
        _USER_ID,
        _EMAIL,
        ReplicaRecord(
            host_id=str(host_id),
            agent_id=str(agent_id),
            display_name="old-client-ws",
            provider_kind=imbue_cloud_provider_name_for_user(_USER_ID),
            hosting_device_id=None,
            state=RECORD_STATE_ACTIVE,
            encrypted_secrets=encode_encrypted_secrets(dek, json.dumps(legacy_payload).encode("utf-8")),
        ),
    )

    _, store_b, _, profile_b = make_profiled_device_for_test(tmp_path, "desktop", cli)
    store_b.reconcile({_USER_ID: _EMAIL}, make_resolver_with_data(agents_json=json.dumps({"agents": []})))
    assert store_b.unlock_account(_USER_ID, _EMAIL, SecretStr(_PASSWORD)) is True

    assert store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL) is True

    key_dir_b = _cloud_host_key_dir(profile_b, host_id)
    assert (key_dir_b / "ssh_key").read_text() == private_key
    assert (key_dir_b / "ssh_key.pub").read_text().startswith("ssh-ed25519 ")
    assert known_hosts_line in (key_dir_b / "known_hosts").read_text()


def test_create_path_seed_of_a_cloud_machine_carries_its_provider_and_secrets(tmp_path: Path) -> None:
    """The very first push of a created cloud machine is complete enough for another device.

    The lease writes the per-host key before ``mngr create`` returns, and the
    provider follows from the account, so nothing has to wait for the
    reconcile's refresh: a device that pulls right away sees a secret-carrying
    record, reads the account as locked, and materializes the key on unlock.
    """
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    paths_a, _store_a, session_a, profile_a = make_profiled_device_for_test(tmp_path, "laptop", cli)
    bundle = set_master_password_for_account(paths_a, _USER_ID, SecretStr(_PASSWORD))
    assert bundle is not None
    cli.sync_bundle_push(_EMAIL, bundle)
    agent_id = AgentId.generate()
    host_id = HostId.generate()
    key_dir = _cloud_host_key_dir(profile_a, host_id)
    key_dir.mkdir(parents=True)
    private_key = generate_test_ssh_private_key()
    (key_dir / "ssh_key").write_text(private_key)

    session_a.associate_created_workspace(
        user_id=_USER_ID,
        agent_id=str(agent_id),
        host_id=str(host_id),
        display_name="fresh",
        color=None,
        is_cloud_row=True,
    )

    pushed = cli.sync_records_by_email[_EMAIL][str(agent_id)]
    assert pushed["provider_kind"] == imbue_cloud_provider_name_for_user(_USER_ID)
    assert pushed["encrypted_secrets"] is not None
    assert pushed["hosting_device_id"] is None

    paths_b, store_b, session_b, _profile_b = make_profiled_device_for_test(tmp_path, "desktop", cli)
    empty_resolver = make_resolver_with_data(agents_json=json.dumps({"agents": []}))
    scheduler_b = WorkspaceSyncScheduler(record_store=store_b, session_store=session_b, resolver=empty_resolver)
    scheduler_b.run_one_pass()
    assert store_b.locked_account_user_ids([_USER_ID]) == [_USER_ID]
    assert store_b.unlock_account(_USER_ID, _EMAIL, SecretStr(_PASSWORD)) is True
    scheduler_b.run_one_pass()
    key_path_b = store_b.host_access_key_path(_USER_ID, store_b.list_records(_USER_ID)[0])
    assert key_path_b is not None
    assert key_path_b.read_text() == private_key


def test_create_path_seed_of_a_machine_on_an_account_owned_connection_carries_the_connection(
    tmp_path: Path,
) -> None:
    """A create on a connection the account owns is seeded under that instance (not the account's Imbue
    Cloud instance), as an account-synced row, with the per-host material the create wrote."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    paths_a, store_a, session_a, connections_a, profile_a = make_profiled_device_with_connections_for_test(
        tmp_path, "laptop", cli
    )
    bundle = set_master_password_for_account(paths_a, _USER_ID, SecretStr(_PASSWORD))
    assert bundle is not None
    cli.sync_bundle_push(_EMAIL, bundle)
    connection_id = add_modal_connection_for_test(connections_a, _USER_ID, "team modal")
    instance = provider_instance_name_for_connection(ConnectionBackend.MODAL, connection_id)
    agent_id = AgentId.generate()
    host_id = HostId.generate()
    paths_on_a = host_access_paths(profile_a, instance, host_id)
    assert paths_on_a is not None
    paths_on_a.per_host_dir.mkdir(parents=True)
    (paths_on_a.per_host_dir / "modal_ssh_key").write_text("MODAL-CLIENT-KEY")

    session_a.associate_created_workspace(
        user_id=_USER_ID,
        agent_id=str(agent_id),
        host_id=str(host_id),
        display_name="fresh",
        color=None,
        is_cloud_row=True,
        provider_kind=instance,
    )

    pushed = cli.sync_records_by_email[_EMAIL][str(agent_id)]
    assert pushed["provider_kind"] == instance
    assert pushed["hosting_device_id"] is None
    payload = store_a.decrypt_record_secrets(_USER_ID, store_a.list_records(_USER_ID)[0])
    assert payload is not None and payload.host_access is not None
    assert payload.host_access.layout == "modal"
    assert payload.host_access.files["modal_ssh_key"] == "MODAL-CLIENT-KEY"


def test_create_path_seed_of_a_local_machine_leaves_the_provider_to_discovery(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    _paths_a, _store_a, session_a = _make_device(tmp_path, "laptop", cli)
    agent_id = AgentId.generate()

    session_a.associate_created_workspace(
        user_id=_USER_ID,
        agent_id=str(agent_id),
        host_id=str(HostId.generate()),
        display_name="local",
        color=None,
        is_cloud_row=False,
    )

    pushed = cli.sync_records_by_email[_EMAIL][str(agent_id)]
    assert pushed["provider_kind"] == ""
    assert pushed["encrypted_secrets"] is None
    assert pushed["hosting_device_id"] == device_id_for_test("laptop")


def test_fresh_device_mirrors_the_server_bundle_before_any_record_carries_secrets(tmp_path: Path) -> None:
    """A device signed in to an account with a master password reads as locked from its first sync.

    The locked signal used to need a secret-carrying record; a fresh device
    that pulled while every record was still secretless showed no unlock
    banner. The reconcile now mirrors the server's bundle, which is also what
    lets the unlock run without a further round trip.
    """
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    paths_a, _store_a, session_a = _make_device(tmp_path, "laptop", cli)
    bundle = set_master_password_for_account(paths_a, _USER_ID, SecretStr(_PASSWORD))
    assert bundle is not None
    cli.sync_bundle_push(_EMAIL, bundle)
    agent_id = AgentId.generate()
    resolver_a = _resolver_with_workspace(agent_id, HostId.generate(), "no-material-yet")
    session_a.associate_workspace(_USER_ID, str(agent_id), resolver_a)
    assert cli.sync_records_by_email[_EMAIL][str(agent_id)]["encrypted_secrets"] is None

    paths_b, store_b, session_b = _make_device(tmp_path, "desktop", cli)
    assert read_bundle_mirror(paths_b, _USER_ID) is None
    empty_resolver = make_resolver_with_data(agents_json=json.dumps({"agents": []}))
    WorkspaceSyncScheduler(record_store=store_b, session_store=session_b, resolver=empty_resolver).run_one_pass()

    assert read_bundle_mirror(paths_b, _USER_ID) == bundle
    assert store_b.locked_account_user_ids([_USER_ID]) == [_USER_ID]
    # The mirror is what the unlock reads, so it works even with the connector away.
    cli.is_sync_offline = True
    assert store_b.unlock_account(_USER_ID, _EMAIL, SecretStr(_PASSWORD)) is True


def test_a_device_holding_a_mirror_the_server_lacks_uploads_it(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    paths_a, store_a, session_a = _make_device(tmp_path, "laptop", cli)
    bundle = set_master_password_for_account(paths_a, _USER_ID, SecretStr(_PASSWORD))
    assert bundle is not None
    assert _EMAIL not in cli.sync_bundle_by_email

    empty_resolver = make_resolver_with_data(agents_json=json.dumps({"agents": []}))
    WorkspaceSyncScheduler(record_store=store_a, session_store=session_a, resolver=empty_resolver).run_one_pass()

    assert cli.sync_bundle_by_email[_EMAIL] == bundle
