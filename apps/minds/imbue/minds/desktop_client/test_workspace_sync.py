"""End-to-end workspace-sync flows across two simulated devices.

Two minds data dirs share one (fake, in-memory) connector backend: device A
provisions and pushes; device B pulls, sees the remote workspace, unlocks
with the master password, decrypts the synced secrets, and materializes the
backup env. This is the whole cross-device story minus live HTTP -- the wire
halves are covered by the connector endpoint tests and the plugin client
tests.
"""

import json
from pathlib import Path

from pydantic import SecretStr

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
