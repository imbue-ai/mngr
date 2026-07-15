"""Unit tests for the landing page's derived cloud-tile access states.

States are fully derived (key-file presence/mtime vs the provider's latest
snapshot, plus the in-memory materialization error) -- these tests drive each
input directly and assert the derived state.
"""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from uuid import uuid4

from imbue.minds.bootstrap import imbue_cloud_provider_name_for_account
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.app import _compute_cloud_tile_state
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.desktop_client.dek_store import ensure_dek
from imbue.minds.desktop_client.workspace_record_store import RECORD_STATE_ACTIVE
from imbue.minds.desktop_client.workspace_record_store import ReplicaRecord
from imbue.minds.desktop_client.workspace_record_store import WorkspaceRecordStore
from imbue.mngr.api.discovery_events import DiscoveryError
from imbue.mngr.primitives import ProviderInstanceName


def _make_profiled_store(tmp_path: Path, cli: FakeImbueCloudCli) -> WorkspaceRecordStore:
    paths = WorkspacePaths(data_dir=tmp_path / "minds")
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    mngr_host_dir = tmp_path / "mngr"
    profile_id = uuid4().hex
    (mngr_host_dir / "profiles" / profile_id).mkdir(parents=True)
    (mngr_host_dir / "config.toml").write_text(f'profile = "{profile_id}"\n')
    return WorkspaceRecordStore(
        paths=paths,
        mngr_host_dir=mngr_host_dir,
        cli=cli,
        device_id="device-tilestate",
        device_label="tilestate",
    )


def _cloud_record(email: str, host_id: str, agent_id: str) -> ReplicaRecord:
    return ReplicaRecord(
        host_id=host_id,
        agent_id=agent_id,
        display_name="cloud-ws",
        provider_kind=imbue_cloud_provider_name_for_account(email),
        state=RECORD_STATE_ACTIVE,
    )


def test_cloud_tile_state_is_plain_before_any_key_is_materialized(tmp_path: Path) -> None:
    email = f"tile-{uuid4().hex}@example.com"
    store = _make_profiled_store(tmp_path, make_fake_imbue_cloud_cli())
    record = _cloud_record(email, f"host-{uuid4().hex}", f"agent-{uuid4().hex}")
    resolver = make_resolver_with_data()

    assert _compute_cloud_tile_state(resolver, store, email, record) == ("", None)


def test_cloud_tile_state_is_connecting_until_a_snapshot_newer_than_the_key(tmp_path: Path) -> None:
    email = f"tile-{uuid4().hex}@example.com"
    store = _make_profiled_store(tmp_path, make_fake_imbue_cloud_cli())
    record = _cloud_record(email, f"host-{uuid4().hex}", f"agent-{uuid4().hex}")
    key_path = store.imbue_cloud_host_ssh_key_path(email, record.host_id)
    assert key_path is not None
    key_path.parent.mkdir(parents=True)
    key_path.write_text("materialized-key")
    resolver = make_resolver_with_data()
    provider_name = ProviderInstanceName(imbue_cloud_provider_name_for_account(email))

    # No snapshot at all yet: connecting.
    assert _compute_cloud_tile_state(resolver, store, email, record) == ("connecting", None)

    # A snapshot from before the key appeared does not resolve it.
    resolver.update_providers(
        provider_name=provider_name,
        provider=None,
        error=None,
        last_snapshot_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    assert _compute_cloud_tile_state(resolver, store, email, record) == ("connecting", None)


def test_cloud_tile_state_is_unreachable_once_a_newer_healthy_snapshot_lacks_the_host(tmp_path: Path) -> None:
    email = f"tile-{uuid4().hex}@example.com"
    store = _make_profiled_store(tmp_path, make_fake_imbue_cloud_cli())
    record = _cloud_record(email, f"host-{uuid4().hex}", f"agent-{uuid4().hex}")
    key_path = store.imbue_cloud_host_ssh_key_path(email, record.host_id)
    assert key_path is not None
    key_path.parent.mkdir(parents=True)
    key_path.write_text("materialized-key")
    resolver = make_resolver_with_data()
    provider_name = ProviderInstanceName(imbue_cloud_provider_name_for_account(email))
    resolver.update_providers(
        provider_name=provider_name,
        provider=None,
        error=None,
        last_snapshot_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    assert _compute_cloud_tile_state(resolver, store, email, record) == ("unreachable", None)

    # An errored provider poll downgrades the verdict back to connecting: an
    # unreachable claim needs a healthy snapshot behind it.
    resolver.update_providers(
        provider_name=provider_name,
        provider=None,
        error=DiscoveryError(type_name="RuntimeError", message="boom", provider_name=provider_name),
        last_snapshot_at=datetime.now(timezone.utc) + timedelta(minutes=6),
    )
    assert _compute_cloud_tile_state(resolver, store, email, record) == ("connecting", None)


def test_cloud_tile_state_reports_materialization_errors(tmp_path: Path) -> None:
    email = f"tile-{uuid4().hex}@example.com"
    user_id = uuid4().hex
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=user_id, email=email)
    store = _make_profiled_store(tmp_path, cli)
    ensure_dek(store.paths, user_id)
    host_id = f"host-{uuid4().hex}"
    agent_id = f"agent-{uuid4().hex}"
    # A record whose secrets blob cannot be decrypted (corrupt/foreign blob).
    cli.sync_records_by_email[email] = {
        host_id: {
            "host_id": host_id,
            "agent_id": agent_id,
            "display_name": "cloud-ws",
            "provider_kind": imbue_cloud_provider_name_for_account(email),
            "hosting_device_id": None,
            "device_label": "elsewhere",
            "state": RECORD_STATE_ACTIVE,
            "encrypted_secrets": "!!!not-a-valid-blob",
            "revision": 1,
        }
    }
    resolver = make_resolver_with_data()
    store.reconcile({user_id: email}, resolver)

    assert store.materialize_account_synced_secrets(user_id, email) is False

    record = store.list_records(user_id)[0]
    state, detail = _compute_cloud_tile_state(resolver, store, email, record)
    assert state == "error"
    assert detail is not None and "decrypt" in detail
