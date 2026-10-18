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

import pytest

from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.app import _compute_cloud_tile_state
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import make_connection_store_for_test
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.desktop_client.connections.connection_store import ConnectionStore
from imbue.minds.desktop_client.connections.primitives import ConnectionId
from imbue.minds.desktop_client.connections.primitives import imbue_cloud_provider_name_for_user
from imbue.minds.desktop_client.dek_store import ensure_dek
from imbue.minds.desktop_client.testing import device_id_for_test
from imbue.minds.desktop_client.workspace_record_store import RECORD_STATE_ACTIVE
from imbue.minds.desktop_client.workspace_record_store import ReplicaRecord
from imbue.minds.desktop_client.workspace_record_store import WorkspaceRecordStore
from imbue.mngr.api.discovery_events import DiscoveryError
from imbue.mngr.primitives import ProviderInstanceName


def _make_profiled_store(
    tmp_path: Path, cli: FakeImbueCloudCli, connection_store: ConnectionStore | None = None
) -> WorkspaceRecordStore:
    paths = InstallationPaths(data_dir=tmp_path / "minds")
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    mngr_host_dir = tmp_path / "mngr"
    profile_id = uuid4().hex
    (mngr_host_dir / "profiles" / profile_id).mkdir(parents=True)
    (mngr_host_dir / "config.toml").write_text(f'profile = "{profile_id}"\n')
    return WorkspaceRecordStore(
        paths=paths,
        mngr_host_dir=mngr_host_dir,
        cli=cli,
        device_id=device_id_for_test("tilestate"),
        device_label="tilestate",
        connection_store=connection_store,
    )


def _cloud_record(user_id: str, host_id: str, agent_id: str) -> ReplicaRecord:
    return ReplicaRecord(
        host_id=host_id,
        agent_id=agent_id,
        display_name="cloud-ws",
        provider_kind=imbue_cloud_provider_name_for_user(user_id),
        state=RECORD_STATE_ACTIVE,
    )


def test_cloud_tile_state_is_plain_before_any_key_is_materialized(tmp_path: Path) -> None:
    user_id = str(uuid4())
    store = _make_profiled_store(tmp_path, make_fake_imbue_cloud_cli())
    ensure_dek(store.paths, user_id)
    record = _cloud_record(user_id, f"host-{uuid4().hex}", f"agent-{uuid4().hex}")
    resolver = make_resolver_with_data()

    assert _compute_cloud_tile_state(resolver, store, user_id, record) == ("", None)


def test_cloud_tile_state_is_locked_when_no_key_can_be_materialized_without_the_dek(tmp_path: Path) -> None:
    """Without the account's DEK the synced key can never land here, so the tile says so instead of staying plain."""
    user_id = str(uuid4())
    store = _make_profiled_store(tmp_path, make_fake_imbue_cloud_cli())
    record = _cloud_record(user_id, f"host-{uuid4().hex}", f"agent-{uuid4().hex}")
    resolver = make_resolver_with_data()

    assert _compute_cloud_tile_state(resolver, store, user_id, record) == ("locked", None)


def test_cloud_tile_state_reports_a_connection_row_whose_connection_is_gone(tmp_path: Path) -> None:
    """A row on a connection the account no longer holds can never be reached from here, DEK or not, and
    whether or not its key was materialized before the connection went away (no block will read it)."""
    user_id = str(uuid4())
    store = _make_profiled_store(tmp_path, make_fake_imbue_cloud_cli())
    ensure_dek(store.paths, user_id)
    record = ReplicaRecord(
        host_id=f"host-{uuid4().hex}",
        agent_id=f"agent-{uuid4().hex}",
        display_name="aws-ws",
        provider_kind=f"aws_{uuid4().hex}",
        state=RECORD_STATE_ACTIVE,
    )
    resolver = make_resolver_with_data()

    assert _compute_cloud_tile_state(resolver, store, user_id, record) == ("missing_connection", None)

    key_path = store.host_access_key_path(user_id, record)
    assert key_path is not None
    key_path.parent.mkdir(parents=True)
    key_path.write_text("materialized-key")

    assert _compute_cloud_tile_state(resolver, store, user_id, record) == ("missing_connection", None)


@pytest.mark.parametrize(
    ("metadata", "expected_state"),
    [
        ({"backend": "aws", "alias": "scrubbed", "connection_format": 1}, "no_credentials"),
        ({"backend": "aws", "alias": "from the future", "connection_format": 99}, "read_only"),
    ],
    ids=["no-credentials", "read-only"],
)
def test_cloud_tile_state_reports_a_held_connection_whose_credentials_are_unusable_here(
    tmp_path: Path, metadata: dict[str, object], expected_state: str
) -> None:
    """A held connection without usable credentials never gets a key or a block, so the tile says why."""
    user_id = str(uuid4())
    email = "alice@example.com"
    cli = make_fake_imbue_cloud_cli()
    connection_store = make_connection_store_for_test(tmp_path / "minds", cli)
    store = _make_profiled_store(tmp_path, cli, connection_store)
    ensure_dek(store.paths, user_id)
    connection_id = ConnectionId.generate()
    # Synced from another device without a payload (scrubbed), or in a newer format.
    cli.seed_connection_document(email, str(connection_id), metadata)
    connection_store.pull(user_id, email)
    record = ReplicaRecord(
        host_id=f"host-{uuid4().hex}",
        agent_id=f"agent-{uuid4().hex}",
        display_name="aws-ws",
        provider_kind=f"aws_{connection_id.get_uuid().hex}",
        state=RECORD_STATE_ACTIVE,
    )
    resolver = make_resolver_with_data()

    assert _compute_cloud_tile_state(resolver, store, user_id, record) == (expected_state, None)


def test_cloud_tile_state_is_connecting_until_a_snapshot_newer_than_the_key(tmp_path: Path) -> None:
    user_id = str(uuid4())
    store = _make_profiled_store(tmp_path, make_fake_imbue_cloud_cli())
    record = _cloud_record(user_id, f"host-{uuid4().hex}", f"agent-{uuid4().hex}")
    key_path = store.host_access_key_path(user_id, record)
    assert key_path is not None
    key_path.parent.mkdir(parents=True)
    key_path.write_text("materialized-key")
    resolver = make_resolver_with_data()
    provider_name = ProviderInstanceName(imbue_cloud_provider_name_for_user(user_id))

    # No snapshot at all yet: connecting.
    assert _compute_cloud_tile_state(resolver, store, user_id, record) == ("connecting", None)

    # A snapshot from before the key appeared does not resolve it.
    resolver.update_providers(
        provider_name=provider_name,
        provider=None,
        error=None,
        last_snapshot_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    assert _compute_cloud_tile_state(resolver, store, user_id, record) == ("connecting", None)


def test_cloud_tile_state_is_unreachable_once_a_newer_healthy_snapshot_lacks_the_host(tmp_path: Path) -> None:
    user_id = str(uuid4())
    store = _make_profiled_store(tmp_path, make_fake_imbue_cloud_cli())
    record = _cloud_record(user_id, f"host-{uuid4().hex}", f"agent-{uuid4().hex}")
    key_path = store.host_access_key_path(user_id, record)
    assert key_path is not None
    key_path.parent.mkdir(parents=True)
    key_path.write_text("materialized-key")
    resolver = make_resolver_with_data()
    provider_name = ProviderInstanceName(imbue_cloud_provider_name_for_user(user_id))
    resolver.update_providers(
        provider_name=provider_name,
        provider=None,
        error=None,
        last_snapshot_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    assert _compute_cloud_tile_state(resolver, store, user_id, record) == ("unreachable", None)

    # An errored provider poll downgrades the verdict back to connecting: an
    # unreachable claim needs a healthy snapshot behind it.
    resolver.update_providers(
        provider_name=provider_name,
        provider=None,
        error=DiscoveryError(type_name="RuntimeError", message="boom", provider_name=provider_name),
        last_snapshot_at=datetime.now(timezone.utc) + timedelta(minutes=6),
    )
    assert _compute_cloud_tile_state(resolver, store, user_id, record) == ("connecting", None)


def test_cloud_tile_state_reports_materialization_errors(tmp_path: Path) -> None:
    user_id = uuid4().hex
    email = f"tile-{uuid4().hex}@example.com"
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
            "provider_kind": imbue_cloud_provider_name_for_user(user_id),
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
    state, detail = _compute_cloud_tile_state(resolver, store, user_id, record)
    assert state == "error"
    assert detail is not None and "decrypt" in detail


def test_cloud_tile_state_stays_connecting_when_a_dropped_pre_start_error_set_no_freshness(
    tmp_path: Path,
) -> None:
    """A pre-start snapshot whose error was dropped must not make the host look unreachable.

    Regression: the backlog replay dropped a provider's pre-start error but still
    recorded that snapshot's time, so the provider read as healthy-with-zero-hosts and
    a leased, perfectly reachable workspace rendered "unreachable" until a fresh
    snapshot landed. Freshness withheld -> the verdict stays "connecting".
    """
    user_id = str(uuid4())
    store = _make_profiled_store(tmp_path, make_fake_imbue_cloud_cli())
    record = _cloud_record(user_id, f"host-{uuid4().hex}", f"agent-{uuid4().hex}")
    key_path = store.host_access_key_path(user_id, record)
    assert key_path is not None
    key_path.parent.mkdir(parents=True)
    key_path.write_text("materialized-key")
    resolver = make_resolver_with_data()
    provider_name = ProviderInstanceName(imbue_cloud_provider_name_for_user(user_id))

    # The replay shape: error dropped to None, but the snapshot carried no usable state.
    resolver.update_providers(
        provider_name=provider_name,
        provider=None,
        error=None,
        last_snapshot_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        is_snapshot_state_current=False,
    )
    assert _compute_cloud_tile_state(resolver, store, user_id, record) == ("connecting", None)

    # A genuine post-start snapshot does record freshness, so the verdict can advance.
    resolver.update_providers(
        provider_name=provider_name,
        provider=None,
        error=None,
        last_snapshot_at=datetime.now(timezone.utc) + timedelta(minutes=6),
    )
    assert _compute_cloud_tile_state(resolver, store, user_id, record) == ("unreachable", None)
