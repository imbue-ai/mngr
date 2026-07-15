"""End-to-end workspace-sync flows across two simulated devices.

Two minds data dirs share one (fake, in-memory) connector backend: device A
provisions and pushes; device B pulls, sees the remote workspace, unlocks
with the master password, decrypts the synced secrets, and materializes the
backup env. This is the whole cross-device story minus live HTTP -- the wire
halves are covered by the connector endpoint tests and the plugin client
tests.
"""

import json
import os
import time
from pathlib import Path
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

from imbue.minds.bootstrap import imbue_cloud_provider_name_for_account
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backup_env_store import read_canonical_env
from imbue.minds.desktop_client.backup_env_store import write_canonical_env
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.desktop_client.dek_store import is_account_unlocked
from imbue.minds.desktop_client.dek_store import set_master_password_for_account
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.sync_scheduler import WorkspaceSyncScheduler
from imbue.minds.desktop_client.workspace_record_store import WorkspaceRecordStore
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId

_USER_ID = "11111111-2222-3333-4444-555555555555"
_EMAIL = "sync-user@example.com"
_PASSWORD = "correct horse battery staple"


def _make_device(
    base: Path, name: str, cli: FakeImbueCloudCli
) -> tuple[WorkspacePaths, WorkspaceRecordStore, MultiAccountSessionStore]:
    paths = WorkspacePaths(data_dir=base / name)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    record_store = WorkspaceRecordStore(
        paths=paths,
        cli=cli,
        device_id=f"device-{name}",
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

    pushed = cli.sync_records_by_email[_EMAIL][str(host_id)]
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
    assert records_b[0].hosting_device_id == "device-laptop"

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

    pushed = cli.sync_records_by_email[_EMAIL][str(host_id)]
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
    assert cli.sync_records_by_email[_EMAIL][str(host_id)]["encrypted_secrets"] is None

    # The empty -> non-empty transition (the settings-page flow): wrap, push
    # the bundle, then push all pending secrets.
    bundle = set_master_password_for_account(paths_a, _USER_ID, SecretStr(_PASSWORD))
    assert bundle is not None
    cli.sync_bundle_push(_EMAIL, bundle)
    store_a.push_all_secrets(_USER_ID, _EMAIL, resolver)

    assert cli.sync_records_by_email[_EMAIL][str(host_id)]["encrypted_secrets"] is not None


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
    original_blob = cli.sync_records_by_email[_EMAIL][str(host_id)]["encrypted_secrets"]
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

    assert cli.sync_records_by_email[_EMAIL][str(host_id)]["encrypted_secrets"] == original_blob


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
    assert str(host_id) in cli.sync_records_by_email[_EMAIL]
    # The carried-over legacy password's bundle must reach the connector too:
    # without it no other device can ever unlock the synced secrets.
    assert _EMAIL in cli.sync_bundle_by_email

    # The workspace disappears locally (definitively absent) -> tombstoned.
    empty_resolver = make_resolver_with_data(agents_json=json.dumps({"agents": []}))
    scheduler_after = WorkspaceSyncScheduler(record_store=store, session_store=session, resolver=empty_resolver)
    scheduler_after.run_one_pass()
    assert cli.sync_records_by_email[_EMAIL][str(host_id)]["state"] == "destroyed"


# -- SSH material materialization (cloud rows accessible from any install) ----


def _make_profiled_device(
    base: Path, name: str, cli: FakeImbueCloudCli
) -> tuple[WorkspacePaths, WorkspaceRecordStore, MultiAccountSessionStore, Path]:
    """A device whose mngr profile dir exists (SSH material collection + materialization need it)."""
    paths = WorkspacePaths(data_dir=base / name)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    mngr_host_dir = base / name / "mngr"
    profile_id = uuid4().hex
    profile_dir = mngr_host_dir / "profiles" / profile_id
    profile_dir.mkdir(parents=True)
    (mngr_host_dir / "config.toml").write_text(f'profile = "{profile_id}"\n')
    record_store = WorkspaceRecordStore(
        paths=paths,
        mngr_host_dir=mngr_host_dir,
        cli=cli,
        device_id=f"device-{name}",
        device_label=name,
    )
    session_store = MultiAccountSessionStore(data_dir=paths.data_dir, cli=cli, record_store=record_store)
    return paths, record_store, session_store, profile_dir


def _generate_test_ssh_private_key() -> str:
    """A traditional-PEM RSA key, the exact flavor mngr's ``generate_ssh_keypair`` produces.

    2048 bits (vs mngr's 4096) keeps test key generation fast; the container
    format is what the materializer's parser must handle.
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


def _cloud_host_key_dir(profile_dir: Path, host_id: HostId) -> Path:
    instance_name = imbue_cloud_provider_name_for_account(_EMAIL)
    return profile_dir / "providers" / "imbue_cloud" / instance_name / "hosts" / str(host_id)


def _cloud_resolver_with_workspace(agent_id: AgentId, host_id: HostId, name: str) -> MngrCliBackendResolver:
    instance_name = imbue_cloud_provider_name_for_account(_EMAIL)
    agents = [
        {
            "id": str(agent_id),
            "labels": {"is_primary": "true"},
            "host": {"id": str(host_id), "name": name},
            "provider": instance_name,
        }
    ]
    return make_resolver_with_data(agents_json=json.dumps({"agents": agents}))


def _provision_cloud_workspace_on_device_a(tmp_path: Path, cli: FakeImbueCloudCli) -> tuple[AgentId, HostId, str, str]:
    """Device A leases a cloud workspace: per-host key on disk, record pushed with full secrets."""
    paths_a, _, session_a, profile_a = _make_profiled_device(tmp_path, "laptop", cli)
    bundle = set_master_password_for_account(paths_a, _USER_ID, SecretStr(_PASSWORD))
    assert bundle is not None
    cli.sync_bundle_push(_EMAIL, bundle)

    agent_id = AgentId.generate()
    host_id = HostId.generate()
    private_key = _generate_test_ssh_private_key()
    known_hosts_line = f"[198.51.100.7]:22001 ssh-ed25519 AAAATESTPIN{uuid4().hex}"
    key_dir = _cloud_host_key_dir(profile_a, host_id)
    key_dir.mkdir(parents=True)
    (key_dir / "ssh_key").write_text(private_key)
    (key_dir / "known_hosts").write_text(known_hosts_line + "\n")

    resolver_a = _cloud_resolver_with_workspace(agent_id, host_id, "cloud-ws")
    session_a.associate_workspace(_USER_ID, str(agent_id), resolver_a)
    pushed = cli.sync_records_by_email[_EMAIL][str(host_id)]
    assert pushed["encrypted_secrets"] is not None
    assert pushed["hosting_device_id"] is None
    return agent_id, host_id, private_key, known_hosts_line


def test_unlock_materializes_cloud_row_ssh_material_on_a_fresh_install(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    _, host_id, private_key, known_hosts_line = _provision_cloud_workspace_on_device_a(tmp_path, cli)

    # Device B: fresh install, pulls the record, unlocks, materializes.
    _, store_b, _, profile_b = _make_profiled_device(tmp_path, "desktop", cli)
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
    assert public_text.startswith("ssh-rsa ")
    assert known_hosts_line in (key_dir_b / "known_hosts").read_text()
    # Idempotent: unchanged material reports nothing written.
    assert store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL) is False


def test_materializer_never_touches_a_host_this_install_leased(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    _, host_id, _, _ = _provision_cloud_workspace_on_device_a(tmp_path, cli)

    _, store_b, _, profile_b = _make_profiled_device(tmp_path, "desktop", cli)
    store_b.reconcile({_USER_ID: _EMAIL}, make_resolver_with_data(agents_json=json.dumps({"agents": []})))
    assert store_b.unlock_account(_USER_ID, _EMAIL, SecretStr(_PASSWORD)) is True

    # B holds its own lease for this host: lease.json + its own keypair.
    key_dir_b = _cloud_host_key_dir(profile_b, host_id)
    key_dir_b.mkdir(parents=True)
    local_key = _generate_test_ssh_private_key()
    (key_dir_b / "ssh_key").write_text(local_key)
    (key_dir_b / "lease.json").write_text("{}")

    assert store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL) is False
    assert (key_dir_b / "ssh_key").read_text() == local_key


def test_materializer_replaces_a_placeholder_keypair(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    _, host_id, synced_key, _ = _provision_cloud_workspace_on_device_a(tmp_path, cli)

    _, store_b, _, profile_b = _make_profiled_device(tmp_path, "desktop", cli)
    store_b.reconcile({_USER_ID: _EMAIL}, make_resolver_with_data(agents_json=json.dumps({"agents": []})))
    assert store_b.unlock_account(_USER_ID, _EMAIL, SecretStr(_PASSWORD)) is True

    # The provider generated a placeholder pair when it discovered the lease
    # without a local key; no lease.json exists, so the synced key must win.
    key_dir_b = _cloud_host_key_dir(profile_b, host_id)
    key_dir_b.mkdir(parents=True)
    (key_dir_b / "ssh_key").write_text(_generate_test_ssh_private_key())

    assert store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL) is True
    assert (key_dir_b / "ssh_key").read_text() == synced_key


def test_sweep_removes_key_dirs_for_tombstoned_records_but_keeps_owned_leases(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    _, host_id, _, _ = _provision_cloud_workspace_on_device_a(tmp_path, cli)

    _, store_b, _, profile_b = _make_profiled_device(tmp_path, "desktop", cli)
    store_b.reconcile({_USER_ID: _EMAIL}, make_resolver_with_data(agents_json=json.dumps({"agents": []})))
    assert store_b.unlock_account(_USER_ID, _EMAIL, SecretStr(_PASSWORD)) is True
    assert store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL) is True
    key_dir_b = _cloud_host_key_dir(profile_b, host_id)
    assert key_dir_b.is_dir()

    # The workspace is destroyed from another install; B pulls the tombstone.
    server_record = cli.sync_records_by_email[_EMAIL][str(host_id)]
    server_record["state"] = "destroyed"
    store_b.reconcile({_USER_ID: _EMAIL}, make_resolver_with_data(agents_json=json.dumps({"agents": []})))

    # Fresh dirs are protected by the in-flight-lease grace; age it out.
    old_timestamp = time.time() - 7200
    os.utime(key_dir_b, (old_timestamp, old_timestamp))
    store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL)
    assert not key_dir_b.exists()

    # An owned lease dir (lease.json) is never swept, even without a record.
    owned_dir = _cloud_host_key_dir(profile_b, HostId.generate())
    owned_dir.mkdir(parents=True)
    (owned_dir / "lease.json").write_text("{}")
    os.utime(owned_dir, (old_timestamp, old_timestamp))
    store_b.materialize_account_synced_secrets(_USER_ID, _EMAIL)
    assert owned_dir.is_dir()


def test_producer_repushes_secrets_when_the_material_changes(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    paths_a, store_a, session_a, profile_a = _make_profiled_device(tmp_path, "laptop", cli)
    bundle = set_master_password_for_account(paths_a, _USER_ID, SecretStr(_PASSWORD))
    assert bundle is not None
    cli.sync_bundle_push(_EMAIL, bundle)

    agent_id = AgentId.generate()
    host_id = HostId.generate()
    write_canonical_env(paths_a, agent_id, "RESTIC_REPOSITORY=s3:v1\n")
    resolver_a = _cloud_resolver_with_workspace(agent_id, host_id, "cloud-ws")
    session_a.associate_workspace(_USER_ID, str(agent_id), resolver_a)
    revision_before = int(str(cli.sync_records_by_email[_EMAIL][str(host_id)]["revision"]))

    # Unchanged material: a reconcile pushes nothing new.
    store_a.reconcile({_USER_ID: _EMAIL}, resolver_a)
    assert int(str(cli.sync_records_by_email[_EMAIL][str(host_id)]["revision"])) == revision_before

    # The backup env rotates; the next reconcile re-pushes the secrets.
    write_canonical_env(paths_a, agent_id, "RESTIC_REPOSITORY=s3:v2-rotated\n")
    store_a.reconcile({_USER_ID: _EMAIL}, resolver_a)
    assert int(str(cli.sync_records_by_email[_EMAIL][str(host_id)]["revision"])) > revision_before

    # A fresh install decrypts the rotated env.
    paths_b, store_b, _, _ = _make_profiled_device(tmp_path, "desktop", cli)
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
    blob_before = cli.sync_records_by_email[_EMAIL][str(host_id)]["encrypted_secrets"]

    # Device B is unlocked, sees the cloud workspace in its own discovery, and
    # holds only PARTIAL local material (a backup env, no SSH key). Its
    # reconcile must not replace the record's full secrets with that view.
    paths_b, store_b, _, _ = _make_profiled_device(tmp_path, "desktop", cli)
    store_b.reconcile({_USER_ID: _EMAIL}, make_resolver_with_data(agents_json=json.dumps({"agents": []})))
    assert store_b.unlock_account(_USER_ID, _EMAIL, SecretStr(_PASSWORD)) is True
    write_canonical_env(paths_b, agent_id, "RESTIC_REPOSITORY=s3:partial-view\n")

    resolver_b = _cloud_resolver_with_workspace(agent_id, host_id, "cloud-ws")
    store_b.reconcile({_USER_ID: _EMAIL}, resolver_b)

    assert cli.sync_records_by_email[_EMAIL][str(host_id)]["encrypted_secrets"] == blob_before
