import json
from base64 import b64encode
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import SecretStr

from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import make_connection_store_for_test
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import unlock_account_with_master_password_for_test
from imbue.minds.desktop_client.connections.connection_store import ConnectionStore
from imbue.minds.desktop_client.connections.connection_store import _encode_encrypted_payload
from imbue.minds.desktop_client.connections.data_types import CONNECTION_STATE_DELETED
from imbue.minds.desktop_client.connections.data_types import ConnectionAvailability
from imbue.minds.desktop_client.connections.data_types import ConnectionCredentialSource
from imbue.minds.desktop_client.connections.data_types import ConnectionCredentials
from imbue.minds.desktop_client.connections.primitives import CONNECTION_DOCUMENT_KIND
from imbue.minds.desktop_client.connections.primitives import ConnectionBackend
from imbue.minds.desktop_client.connections.primitives import ConnectionId
from imbue.minds.desktop_client.connections.primitives import LOCAL_PROFILE_USER_ID
from imbue.minds.desktop_client.connections.primitives import provider_instance_name_for_connection
from imbue.minds.desktop_client.dek_store import delete_dek
from imbue.minds.desktop_client.dek_store import ensure_dek
from imbue.minds.desktop_client.dek_store import load_dek
from imbue.minds.errors import ConnectionNotFoundError
from imbue.minds.errors import ConnectionRemovedElsewhereError
from imbue.minds.errors import ConnectionRequiresMasterPasswordError
from imbue.minds.errors import ConnectionTooNewError
from imbue.minds.errors import ConnectionUpdateInProgressError
from imbue.minds.errors import WorkspaceSyncError

_AWS_CREDENTIALS = ConnectionCredentials(
    aws_access_key_id="AKIA1234EXAMPLE9", aws_secret_access_key=SecretStr("very-secret")
)
_AWS_PLACEMENT = {"default_region": "us-west-2", "default_instance_type": "t3.large"}


def _unlocked_account(paths: InstallationPaths) -> tuple[str, str]:
    """A signed-in account that is unlocked here with a master password set: ``(user_id, email)``."""
    user_id = str(uuid4())
    unlock_account_with_master_password_for_test(paths, user_id)
    return user_id, f"{uuid4().hex}@example.com"


def _add_aws(store: ConnectionStore, owner: str) -> ConnectionId:
    connection_id = ConnectionId.generate()
    store.add_connection(
        owner, connection_id, ConnectionBackend.AWS, "Work AWS", "AKIA…PLE9", _AWS_PLACEMENT, _AWS_CREDENTIALS
    )
    return connection_id


def test_add_to_account_encrypts_and_round_trips_the_credentials(tmp_path: Path) -> None:
    store = make_connection_store_for_test(tmp_path)
    user_id, _email = _unlocked_account(store.paths)

    connection_id = _add_aws(store, user_id)

    (record,) = store.list_connections(user_id)
    assert record.doc_id == str(connection_id)
    assert record.is_dirty is True
    assert record.payload is None
    assert record.encrypted_payload is not None
    # The replica keeps the payload encrypted at rest: no secret in the file.
    replica_text = (store.paths.data_dir / "connections" / f"{user_id}.json").read_text()
    assert "very-secret" not in replica_text
    decrypted = store.decrypt_credentials(user_id, record)
    assert decrypted is not None
    assert decrypted.aws_secret_access_key is not None
    assert decrypted.aws_secret_access_key.get_secret_value() == "very-secret"
    assert store.availability(user_id, record) is ConnectionAvailability.AVAILABLE
    (view,) = store.list_views(user_id)
    assert view.instance_name == provider_instance_name_for_connection(ConnectionBackend.AWS, connection_id)
    assert view.region == "us-west-2"


def test_add_to_account_requires_an_unlocked_account_with_a_master_password(tmp_path: Path) -> None:
    store = make_connection_store_for_test(tmp_path)
    unlocked_no_password = str(uuid4())
    ensure_dek(store.paths, unlocked_no_password)
    with pytest.raises(ConnectionRequiresMasterPasswordError):
        _add_aws(store, unlocked_no_password)
    with pytest.raises(ConnectionRequiresMasterPasswordError):
        _add_aws(store, str(uuid4()))
    assert store.list_all_connections() == {}


def test_local_profile_keeps_a_plaintext_payload_and_is_never_pushed(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    store = make_connection_store_for_test(tmp_path, cli)

    connection_id = _add_aws(store, LOCAL_PROFILE_USER_ID)

    (record,) = store.list_connections(LOCAL_PROFILE_USER_ID)
    assert record.is_dirty is False
    assert record.encrypted_payload is None
    assert record.payload is not None and record.payload["aws_secret_access_key"] == "very-secret"
    assert store.availability(LOCAL_PROFILE_USER_ID, record) is ConnectionAvailability.AVAILABLE
    assert cli.document_push_count == 0
    store.remove_connection(LOCAL_PROFILE_USER_ID, None, str(connection_id))
    assert store.list_connections(LOCAL_PROFILE_USER_ID) == []


def test_push_inserts_at_revision_one_and_pull_keeps_the_acknowledged_row(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    store = make_connection_store_for_test(tmp_path, cli)
    user_id, email = _unlocked_account(store.paths)
    connection_id = _add_aws(store, user_id)

    acked = store.push_connection(user_id, email, str(connection_id))

    assert acked.revision == 1 and acked.is_dirty is False
    stored = cli.sync_documents_by_email[email][CONNECTION_DOCUMENT_KIND][str(connection_id)]
    assert stored["metadata"]["alias"] == "Work AWS"
    assert stored["encrypted_payload"] == acked.encrypted_payload
    outcome = store.pull(user_id, email)
    assert outcome.is_reached is True and outcome.is_changed is False
    (record,) = store.list_connections(user_id)
    assert record.revision == 1


def test_push_dirty_leaves_a_never_acknowledged_add_to_the_add_route(tmp_path: Path) -> None:
    """A sync pass during credential validation must not push the row before the add route does."""
    cli = make_fake_imbue_cloud_cli()
    store = make_connection_store_for_test(tmp_path, cli)
    user_id, email = _unlocked_account(store.paths)
    connection_id = _add_aws(store, user_id)

    store.push_dirty(user_id, email)

    assert cli.document_push_count == 0
    (record,) = store.list_connections(user_id)
    assert record.is_dirty is True and record.revision == 0
    acked = store.push_connection(user_id, email, str(connection_id))
    assert acked.revision == 1 and cli.document_push_count == 1


def test_a_reopened_store_drops_the_add_a_dead_process_never_finished_but_keeps_the_rest(tmp_path: Path) -> None:
    """The add route's rollback never runs for a process that died mid-registration; the next start must."""
    cli = make_fake_imbue_cloud_cli()
    store = make_connection_store_for_test(tmp_path, cli)
    user_id, email = _unlocked_account(store.paths)
    unfinished_id = _add_aws(store, user_id)
    acked_id = _add_aws(store, user_id)
    store.push_connection(user_id, email, str(acked_id))
    local_id = _add_aws(store, LOCAL_PROFILE_USER_ID)

    reopened = make_connection_store_for_test(tmp_path, cli)

    assert reopened.discard_unregistered_connections() == 1
    assert [record.doc_id for record in reopened.list_connections(user_id)] == [str(acked_id)]
    assert [record.doc_id for record in reopened.list_connections(LOCAL_PROFILE_USER_ID)] == [str(local_id)]
    assert reopened.find_active_connection(str(unfinished_id)) is None
    assert reopened.discard_unregistered_connections() == 0


def test_push_rebases_once_on_a_revision_conflict_with_local_content_winning(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    store = make_connection_store_for_test(tmp_path, cli)
    user_id, email = _unlocked_account(store.paths)
    connection_id = _add_aws(store, user_id)
    store.push_connection(user_id, email, str(connection_id))
    # Another device advanced the row (a metadata edit) while this one holds revision 1.
    server_row = cli.sync_documents_by_email[email][CONNECTION_DOCUMENT_KIND][str(connection_id)]
    server_row["revision"] = 2
    server_row["metadata"] = {**server_row["metadata"], "alias": "renamed elsewhere"}

    store.remove_connection(user_id, email, str(connection_id))

    stored = cli.sync_documents_by_email[email][CONNECTION_DOCUMENT_KIND][str(connection_id)]
    assert stored["state"] == CONNECTION_STATE_DELETED
    assert stored["revision"] == 3
    assert stored["metadata"]["alias"] == "Work AWS"
    # The tombstone acknowledged by the server drops out of the replica.
    assert store.list_connections(user_id) == []


def test_push_never_rebases_over_a_row_a_newer_app_rewrote(tmp_path: Path) -> None:
    """The connection format lives in metadata, which the server does not enforce, so the client must refuse."""
    cli = make_fake_imbue_cloud_cli()
    store = make_connection_store_for_test(tmp_path, cli)
    user_id, email = _unlocked_account(store.paths)
    connection_id = _add_aws(store, user_id)
    store.push_connection(user_id, email, str(connection_id))
    server_row = cli.sync_documents_by_email[email][CONNECTION_DOCUMENT_KIND][str(connection_id)]
    server_row["revision"] = 2
    server_row["metadata"] = {**server_row["metadata"], "alias": "rewritten elsewhere", "connection_format": 99}

    store.remove_connection(user_id, email, str(connection_id))

    stored = cli.sync_documents_by_email[email][CONNECTION_DOCUMENT_KIND][str(connection_id)]
    assert stored["state"] == "active" and stored["revision"] == 2
    store.pull(user_id, email)
    (view,) = store.list_views(user_id)
    assert view.alias == "rewritten elsewhere"
    assert view.availability is ConnectionAvailability.READ_ONLY


def test_a_tombstone_whose_server_row_is_gone_is_dropped_without_reinserting_it(tmp_path: Path) -> None:
    """A conflict naming no stored row means the server has nothing to delete, so the tombstone is done."""
    cli = make_fake_imbue_cloud_cli()
    store = make_connection_store_for_test(tmp_path, cli)
    user_id, email = _unlocked_account(store.paths)
    connection_id = _add_aws(store, user_id)
    store.push_connection(user_id, email, str(connection_id))
    del cli.sync_documents_by_email[email][CONNECTION_DOCUMENT_KIND][str(connection_id)]

    store.remove_connection(user_id, email, str(connection_id))

    assert cli.sync_documents_by_email[email][CONNECTION_DOCUMENT_KIND] == {}
    assert store.list_connections(user_id) == []


def test_pull_merges_server_rows_drops_tombstones_and_keeps_dirty_rows(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    store = make_connection_store_for_test(tmp_path, cli)
    user_id, email = _unlocked_account(store.paths)
    # A row synced by another device, carrying a metadata key this app does not know.
    foreign_id = ConnectionId.generate()
    cli.seed_connection_document(
        email, str(foreign_id), {"backend": "modal", "alias": "elsewhere", "future_key": {"nested": True}}, revision=4
    )
    dirty_id = _add_aws(store, user_id)

    first = store.pull(user_id, email)

    assert first.is_reached is True and first.is_changed is True
    by_doc_id = {record.doc_id: record for record in store.list_connections(user_id)}
    assert by_doc_id[str(dirty_id)].is_dirty is True
    assert by_doc_id[str(foreign_id)].metadata["future_key"] == {"nested": True}
    # The unknown key rides through a rewrite (the tombstone of that row) verbatim.
    wire = by_doc_id[str(foreign_id)].to_wire(CONNECTION_DOCUMENT_KIND, 5, is_payload_included=False)
    assert wire["metadata"] == by_doc_id[str(foreign_id)].metadata
    assert "encrypted_payload" not in wire
    # The server tombstones it: the clean local row goes away.
    cli.tombstone_connection_document(email, str(foreign_id))
    second = store.pull(user_id, email)
    assert second.is_changed is True
    assert [record.doc_id for record in store.list_connections(user_id)] == [str(dirty_id)]


def test_pull_drops_clean_rows_the_server_no_longer_lists_but_keeps_dirty_ones(tmp_path: Path) -> None:
    """A tombstone the server purged is absent from the listing, not deleted in it; an unpushed edit must survive that."""
    cli = make_fake_imbue_cloud_cli()
    store = make_connection_store_for_test(tmp_path, cli)
    user_id, email = _unlocked_account(store.paths)
    purged_id = _add_aws(store, user_id)
    store.push_connection(user_id, email, str(purged_id))
    unpushed_id = _add_aws(store, user_id)
    del cli.sync_documents_by_email[email][CONNECTION_DOCUMENT_KIND][str(purged_id)]

    outcome = store.pull(user_id, email)

    assert outcome.is_reached is True and outcome.is_changed is True
    (remaining,) = store.list_connections(user_id)
    assert remaining.doc_id == str(unpushed_id) and remaining.is_dirty is True


def test_a_scrubbed_payload_leaves_the_connection_without_credentials_but_listed(tmp_path: Path) -> None:
    """Clearing the master password scrubs every payload server-side; the row stays visible and removable."""
    cli = make_fake_imbue_cloud_cli()
    store = make_connection_store_for_test(tmp_path, cli)
    user_id, email = _unlocked_account(store.paths)
    scrubbed_id = ConnectionId.generate()
    cli.seed_connection_document(
        email,
        str(scrubbed_id),
        {"backend": "aws", "alias": "scrubbed", "connection_format": 1},
        encrypted_payload=None,
    )
    store.pull(user_id, email)

    (view,) = store.list_views(user_id)
    assert view.availability is ConnectionAvailability.NO_CREDENTIALS
    (record,) = store.list_connections(user_id)
    assert store.decrypt_credentials(user_id, record) is None
    assert store.credentials_by_connection_id([user_id]) == {}


def test_pull_reports_an_unreachable_connector_without_touching_the_replica(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.is_sync_offline = True
    store = make_connection_store_for_test(tmp_path, cli)
    user_id, email = _unlocked_account(store.paths)
    _add_aws(store, user_id)

    outcome = store.pull(user_id, email)

    assert outcome.is_reached is False and outcome.is_changed is False
    assert len(store.list_connections(user_id)) == 1
    with pytest.raises(WorkspaceSyncError):
        store.push_connection(user_id, email, store.list_connections(user_id)[0].doc_id)


def test_locked_account_lists_its_connections_but_cannot_decrypt_them(tmp_path: Path) -> None:
    store = make_connection_store_for_test(tmp_path)
    user_id, _email = _unlocked_account(store.paths)
    _add_aws(store, user_id)
    delete_dek(store.paths, user_id)

    (view,) = store.list_views(user_id)

    assert view.availability is ConnectionAvailability.LOCKED
    assert view.alias == "Work AWS"
    assert store.credentials_by_connection_id([user_id]) == {}


@pytest.mark.parametrize(
    "metadata",
    [
        {"backend": "aws", "alias": "from the future", "connection_format": 99},
        # A newer app's backend without a format bump (the additive case).
        {"backend": "ovh", "alias": "from the future"},
    ],
    ids=["newer-format", "unknown-backend"],
)
def test_connections_a_newer_app_wrote_are_shown_read_only(tmp_path: Path, metadata: dict[str, object]) -> None:
    cli = make_fake_imbue_cloud_cli()
    store = make_connection_store_for_test(tmp_path, cli)
    user_id, email = _unlocked_account(store.paths)
    newer_id = ConnectionId.generate()
    cli.seed_connection_document(email, str(newer_id), metadata)
    store.pull(user_id, email)

    (view,) = store.list_views(user_id)
    assert view.availability is ConnectionAvailability.READ_ONLY
    assert view.instance_name == f"{metadata['backend']}_{newer_id.get_uuid().hex}"
    assert store.credentials_by_connection_id([user_id]) == {}


def test_an_undecryptable_payload_leaves_the_connection_without_credentials_but_removable(tmp_path: Path) -> None:
    """A blob this device cannot decrypt is not a newer app's work, so the row is NO_CREDENTIALS, not READ_ONLY."""
    cli = make_fake_imbue_cloud_cli()
    store = make_connection_store_for_test(tmp_path, cli)
    user_id, email = _unlocked_account(store.paths)
    garbled_id = ConnectionId.generate()
    cli.seed_connection_document(
        email,
        str(garbled_id),
        {"backend": "aws", "alias": "garbled", "connection_format": 1},
        encrypted_payload=b64encode(b"not a sealed payload").decode("ascii"),
    )
    store.pull(user_id, email)

    (view,) = store.list_views(user_id)
    assert view.availability is ConnectionAvailability.NO_CREDENTIALS
    assert store.credentials_by_connection_id([user_id]) == {}


def test_local_profile_connection_with_a_newer_format_payload_is_read_only(tmp_path: Path) -> None:
    """A local row whose payload a newer app wrote must not be offered as usable: no block is written for it."""
    _add_aws(make_connection_store_for_test(tmp_path), LOCAL_PROFILE_USER_ID)
    replica_path = make_connection_store_for_test(tmp_path)._replica_path(LOCAL_PROFILE_USER_ID)
    replica = json.loads(replica_path.read_text())
    replica["connections"][0]["payload"]["payload_format"] = 99
    replica_path.write_text(json.dumps(replica))
    store = make_connection_store_for_test(tmp_path)

    (view,) = store.list_views(LOCAL_PROFILE_USER_ID)

    assert view.availability is ConnectionAvailability.READ_ONLY
    assert store.credentials_by_connection_id([]) == {}


def test_pull_lets_a_newer_format_server_row_supersede_a_dirty_local_row(tmp_path: Path) -> None:
    """An unpushed change can never land over a row a newer app wrote, so the server copy replaces it."""
    cli = make_fake_imbue_cloud_cli()
    store = make_connection_store_for_test(tmp_path, cli)
    user_id, email = _unlocked_account(store.paths)
    dirty_id = _add_aws(store, user_id)
    cli.seed_connection_document(
        email, str(dirty_id), {"backend": "aws", "alias": "rewritten elsewhere", "connection_format": 99}, revision=2
    )

    assert store.pull(user_id, email).is_changed is True

    (view,) = store.list_views(user_id)
    assert view.alias == "rewritten elsewhere"
    assert view.availability is ConnectionAvailability.READ_ONLY
    assert store.list_connections(user_id)[0].is_dirty is False


def test_remove_raises_for_an_unknown_connection_and_drops_a_never_pushed_row_without_a_push(
    tmp_path: Path,
) -> None:
    cli = make_fake_imbue_cloud_cli()
    store = make_connection_store_for_test(tmp_path, cli)
    user_id, email = _unlocked_account(store.paths)
    with pytest.raises(ConnectionNotFoundError):
        store.remove_connection(user_id, email, str(ConnectionId.generate()))
    connection_id = _add_aws(store, user_id)

    store.remove_connection(user_id, email, str(connection_id))

    assert store.list_connections(user_id) == []
    assert cli.document_push_count == 0


def test_remove_after_a_failed_push_tombstones_the_row_the_connector_may_hold(tmp_path: Path) -> None:
    """A push that lost its response leaves the document on the server; the rollback must not let it come back."""
    cli = make_fake_imbue_cloud_cli()
    store = make_connection_store_for_test(tmp_path, cli)
    user_id, email = _unlocked_account(store.paths)
    connection_id = _add_aws(store, user_id)
    (record,) = store.list_connections(user_id)
    cli.seed_connection_document(email, str(connection_id), record.metadata, record.encrypted_payload)

    store.remove_connection(user_id, email, str(connection_id), is_push_attempted=True)

    stored = cli.sync_documents_by_email[email][CONNECTION_DOCUMENT_KIND][str(connection_id)]
    assert stored["state"] == CONNECTION_STATE_DELETED and stored["encrypted_payload"] is None
    assert store.list_connections(user_id) == []
    assert store.pull(user_id, email).is_changed is False

    # With the connector unreachable the tombstone waits, dirty, for a later pass; it is no active view meanwhile.
    cli.is_sync_offline = True
    offline_connection_id = _add_aws(store, user_id)
    store.remove_connection(user_id, email, str(offline_connection_id), is_push_attempted=True)
    (tombstone,) = store.list_connections(user_id)
    assert tombstone.state == CONNECTION_STATE_DELETED and tombstone.is_dirty is True
    assert store.list_views(user_id) == []


_ROTATED_AWS_CREDENTIALS = ConnectionCredentials(
    aws_access_key_id="AKIA9999ROTATED1", aws_secret_access_key=SecretStr("rotated-secret")
)


@pytest.mark.parametrize("is_local_profile", [True, False])
def test_update_rewrites_the_credentials_in_place_and_keeps_the_previous_row_until_settled(
    tmp_path: Path, is_local_profile: bool
) -> None:
    cli = make_fake_imbue_cloud_cli()
    store = make_connection_store_for_test(tmp_path, cli)
    user_id, email = _unlocked_account(store.paths)
    owner = LOCAL_PROFILE_USER_ID if is_local_profile else user_id
    connection_id = _add_aws(store, owner)
    if not is_local_profile:
        store.push_connection(owner, email, str(connection_id))
        # A key a newer app added to the metadata rides through the rewrite.
        stored = cli.sync_documents_by_email[email][CONNECTION_DOCUMENT_KIND][str(connection_id)]
        stored["metadata"]["future_key"] = "kept"
        stored["revision"] = 2
        store.pull(owner, email)
    (before,) = store.list_connections(owner)

    previous = store.update_connection(owner, str(connection_id), "Rotated AWS", "AKIA…TED1", _ROTATED_AWS_CREDENTIALS)

    assert previous == before
    (updated,) = store.list_connections(owner)
    assert updated.validation_rollback == previous
    assert updated.is_dirty is True and updated.revision == previous.revision
    assert updated.metadata["alias"] == "Rotated AWS" and updated.metadata["identifier"] == "AKIA…TED1"
    assert updated.metadata["placement"] == _AWS_PLACEMENT
    if not is_local_profile:
        assert updated.metadata["future_key"] == "kept"
    credentials = store.decrypt_credentials(owner, updated)
    assert credentials is not None and credentials.aws_access_key_id == "AKIA9999ROTATED1"
    view = store.view_for(owner, updated)
    assert view is not None
    assert view.instance_name == provider_instance_name_for_connection(ConnectionBackend.AWS, connection_id)

    store.clear_validation_rollback(owner, str(connection_id))
    (settled,) = store.list_connections(owner)
    assert settled.validation_rollback is None and settled.metadata["alias"] == "Rotated AWS"


def test_update_restores_the_previous_row_on_rollback(tmp_path: Path) -> None:
    store = make_connection_store_for_test(tmp_path)
    user_id, _email = _unlocked_account(store.paths)
    connection_id = _add_aws(store, user_id)
    (before,) = store.list_connections(user_id)

    store.update_connection(user_id, str(connection_id), "Rotated AWS", "AKIA…TED1", _ROTATED_AWS_CREDENTIALS)
    store.restore_validation_rollback(user_id, str(connection_id))

    assert store.list_connections(user_id) == [before]


def test_update_refuses_a_tombstone_a_newer_format_row_and_a_locked_account(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    store = make_connection_store_for_test(tmp_path, cli)
    user_id, email = _unlocked_account(store.paths)
    too_new_id = _add_aws(store, user_id)
    store.push_connection(user_id, email, str(too_new_id))
    cli.sync_documents_by_email[email][CONNECTION_DOCUMENT_KIND][str(too_new_id)]["metadata"]["connection_format"] = 99
    cli.sync_documents_by_email[email][CONNECTION_DOCUMENT_KIND][str(too_new_id)]["revision"] = 2
    store.pull(user_id, email)
    with pytest.raises(ConnectionTooNewError):
        store.update_connection(user_id, str(too_new_id), "x", "y", _ROTATED_AWS_CREDENTIALS)

    removed_id = _add_aws(store, user_id)
    store.push_connection(user_id, email, str(removed_id))
    cli.is_sync_offline = True
    store.remove_connection(user_id, email, str(removed_id))
    with pytest.raises(ConnectionNotFoundError):
        store.update_connection(user_id, str(removed_id), "x", "y", _ROTATED_AWS_CREDENTIALS)

    locked_id = _add_aws(store, user_id)
    delete_dek(store.paths, user_id)
    with pytest.raises(ConnectionRequiresMasterPasswordError):
        store.update_connection(user_id, str(locked_id), "x", "y", _ROTATED_AWS_CREDENTIALS)


@pytest.mark.parametrize("is_local_profile", [True, False])
def test_a_row_mid_update_refuses_a_removal_and_a_second_update(tmp_path: Path, is_local_profile: bool) -> None:
    """A tombstone must never carry the rollback (the next start would restore the removed connection), and the
    update's settlement must find the row it left."""
    cli = make_fake_imbue_cloud_cli()
    store = make_connection_store_for_test(tmp_path, cli)
    user_id, email = _unlocked_account(store.paths)
    owner = LOCAL_PROFILE_USER_ID if is_local_profile else user_id
    connection_id = _add_aws(store, owner)
    if not is_local_profile:
        store.push_connection(owner, email, str(connection_id))
    store.update_connection(owner, str(connection_id), "Rotated AWS", "AKIA…TED1", _ROTATED_AWS_CREDENTIALS)
    (mid_update,) = store.list_connections(owner)

    with pytest.raises(ConnectionUpdateInProgressError):
        store.remove_connection(owner, None if is_local_profile else email, str(connection_id))
    with pytest.raises(ConnectionUpdateInProgressError):
        store.update_connection(owner, str(connection_id), "Again", "AKIA…AIN1", _ROTATED_AWS_CREDENTIALS)

    assert store.list_connections(owner) == [mid_update]
    assert cli.document_push_count == (0 if is_local_profile else 1)
    # Settled, the row is an ordinary one again and the removal goes through.
    store.clear_validation_rollback(owner, str(connection_id))
    store.remove_connection(owner, None if is_local_profile else email, str(connection_id))
    assert store.list_active_connections(owner) == []


def test_a_row_mid_update_is_neither_pushed_by_a_sync_pass_nor_overwritten_by_a_pull(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    store = make_connection_store_for_test(tmp_path, cli)
    user_id, email = _unlocked_account(store.paths)
    connection_id = _add_aws(store, user_id)
    store.push_connection(user_id, email, str(connection_id))
    store.update_connection(user_id, str(connection_id), "Rotated AWS", "AKIA…TED1", _ROTATED_AWS_CREDENTIALS)
    cli.sync_documents_by_email[email][CONNECTION_DOCUMENT_KIND][str(connection_id)]["metadata"]["alias"] = "server"

    store.push_dirty(user_id, email)
    assert cli.document_push_count == 1
    outcome = store.pull(user_id, email)

    assert outcome.is_reached is True and outcome.is_changed is False
    (record,) = store.list_connections(user_id)
    assert record.metadata["alias"] == "Rotated AWS" and record.validation_rollback is not None
    # Settled, the row is an ordinary dirty edit the next pass pushes.
    store.clear_validation_rollback(user_id, str(connection_id))
    store.push_dirty(user_id, email)
    stored = cli.sync_documents_by_email[email][CONNECTION_DOCUMENT_KIND][str(connection_id)]
    assert stored["metadata"]["alias"] == "Rotated AWS" and cli.document_push_count == 2


def test_a_reopened_store_restores_the_previous_row_of_an_update_a_dead_process_never_finished(
    tmp_path: Path,
) -> None:
    cli = make_fake_imbue_cloud_cli()
    store = make_connection_store_for_test(tmp_path, cli)
    user_id, email = _unlocked_account(store.paths)
    synced_id = _add_aws(store, user_id)
    store.push_connection(user_id, email, str(synced_id))
    local_id = _add_aws(store, LOCAL_PROFILE_USER_ID)
    previous_rows = {
        owner: store.update_connection(owner, str(doc_id), "Rotated", "AKIA…TED1", _ROTATED_AWS_CREDENTIALS)
        for owner, doc_id in ((user_id, synced_id), (LOCAL_PROFILE_USER_ID, local_id))
    }

    reopened = make_connection_store_for_test(tmp_path, cli)

    assert reopened.discard_unregistered_connections() == 2
    assert reopened.list_connections(user_id) == [previous_rows[user_id]]
    assert reopened.list_connections(LOCAL_PROFILE_USER_ID) == [previous_rows[LOCAL_PROFILE_USER_ID]]


def test_push_refuses_to_resurrect_a_connection_removed_on_another_device(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    store = make_connection_store_for_test(tmp_path, cli)
    user_id, email = _unlocked_account(store.paths)
    connection_id = _add_aws(store, user_id)
    store.push_connection(user_id, email, str(connection_id))
    store.update_connection(user_id, str(connection_id), "Rotated AWS", "AKIA…TED1", _ROTATED_AWS_CREDENTIALS)
    cli.tombstone_connection_document(email, str(connection_id))

    with pytest.raises(ConnectionRemovedElsewhereError):
        store.push_connection(user_id, email, str(connection_id))

    stored = cli.sync_documents_by_email[email][CONNECTION_DOCUMENT_KIND][str(connection_id)]
    assert stored["state"] == CONNECTION_STATE_DELETED
    # A sync pass's push of an ordinary edit drops the local copy instead of fighting the removal.
    store.clear_validation_rollback(user_id, str(connection_id))
    store.push_dirty(user_id, email)
    assert store.list_connections(user_id) == []


def test_retain_unpushed_removals_forgets_the_account_but_not_the_local_profile(tmp_path: Path) -> None:
    store = make_connection_store_for_test(tmp_path)
    user_id, _email = _unlocked_account(store.paths)
    _add_aws(store, user_id)
    _add_aws(store, LOCAL_PROFILE_USER_ID)

    store.retain_unpushed_removals(user_id)
    store.retain_unpushed_removals(LOCAL_PROFILE_USER_ID)

    assert not (store.paths.data_dir / "connections" / f"{user_id}.json").exists()
    assert store.list_connections(user_id) == []
    assert len(store.list_connections(LOCAL_PROFILE_USER_ID)) == 1
    # A fresh store instance sees the same on-disk state.
    reloaded = make_connection_store_for_test(tmp_path)
    assert len(reloaded.list_connections(LOCAL_PROFILE_USER_ID)) == 1


def test_a_removal_queued_while_offline_survives_signout_and_lands_on_the_next_reachable_pass(
    tmp_path: Path,
) -> None:
    """The dead-session signout's push necessarily fails, so the tombstone must outlive the replica."""
    cli = make_fake_imbue_cloud_cli()
    store = make_connection_store_for_test(tmp_path, cli)
    user_id, email = _unlocked_account(store.paths)
    connection_id = _add_aws(store, user_id)
    store.push_connection(user_id, email, str(connection_id))
    cli.is_sync_offline = True
    store.remove_connection(user_id, email, str(connection_id))

    store.retain_unpushed_removals(user_id)

    (retained,) = store.list_connections(user_id)
    assert retained.state == CONNECTION_STATE_DELETED and retained.is_dirty is True
    assert retained.encrypted_payload is None
    assert store.has_encrypted_connections(user_id) is False
    assert (store.paths.data_dir / "connections" / f"{user_id}.json").exists()
    # Re-signin: the server still holds the active row; the dirty tombstone wins the pull and the push lands it.
    cli.is_sync_offline = False
    assert store.pull(user_id, email).is_changed is False
    assert store.list_connections(user_id) == [retained]
    store.push_dirty(user_id, email)
    stored = cli.sync_documents_by_email[email][CONNECTION_DOCUMENT_KIND][str(connection_id)]
    assert stored["state"] == CONNECTION_STATE_DELETED and stored["revision"] == 2
    assert store.list_connections(user_id) == []


def test_rehome_moves_decryptable_connections_into_the_local_profile_keeping_their_ids(tmp_path: Path) -> None:
    cli: FakeImbueCloudCli = make_fake_imbue_cloud_cli()
    store = make_connection_store_for_test(tmp_path, cli)
    user_id, email = _unlocked_account(store.paths)
    connection_id = _add_aws(store, user_id)
    store.push_connection(user_id, email, str(connection_id))
    # A newer client on another device rewrote the payload with a key this app
    # does not model; the pull brings it here and re-homing must carry it through.
    dek = load_dek(store.paths, user_id)
    assert dek is not None
    stored = cli.sync_documents_by_email[email][CONNECTION_DOCUMENT_KIND][str(connection_id)]
    stored["encrypted_payload"] = _encode_encrypted_payload(
        dek, {**_AWS_CREDENTIALS.to_plaintext_dict(), "future_key": "kept"}
    )
    stored["revision"] = int(stored["revision"]) + 1
    assert store.pull(user_id, email).is_changed is True

    rehomed = store.rehome_account_connections_to_local(user_id, email)

    assert rehomed == [str(connection_id)]
    assert store.list_connections(user_id) == []
    (local_record,) = store.list_connections(LOCAL_PROFILE_USER_ID)
    assert local_record.doc_id == str(connection_id)
    assert local_record.payload is not None and local_record.payload["aws_access_key_id"] == "AKIA1234EXAMPLE9"
    assert local_record.payload["future_key"] == "kept"
    stored = cli.sync_documents_by_email[email][CONNECTION_DOCUMENT_KIND][str(connection_id)]
    assert stored["state"] == CONNECTION_STATE_DELETED


def test_replica_files_are_owner_only(tmp_path: Path) -> None:
    store = make_connection_store_for_test(tmp_path)
    user_id, _email = _unlocked_account(store.paths)
    _add_aws(store, user_id)
    path = store.paths.data_dir / "connections" / f"{user_id}.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["connections"][0]["doc_id"].startswith("conn-")


def _add_aws_profile(store: ConnectionStore, owner: str, profile_name: str) -> ConnectionId:
    connection_id = ConnectionId.generate()
    store.add_connection(
        owner,
        connection_id,
        ConnectionBackend.AWS,
        "Work AWS (profile)",
        f"profile {profile_name}",
        {**_AWS_PLACEMENT, "aws_profile": profile_name},
        None,
        ConnectionCredentialSource.LOCAL_PROFILE,
    )
    return connection_id


def test_a_local_profile_connection_carries_no_payload_and_stays_available_when_the_account_locks(
    tmp_path: Path,
) -> None:
    """Nothing is encrypted for a profile connection, so no DEK is needed to use it; whether the
    login exists on this machine is discovery's verdict, not the replica's."""
    cli = make_fake_imbue_cloud_cli()
    store = make_connection_store_for_test(tmp_path, cli)
    user_id, email = _unlocked_account(store.paths)

    connection_id = _add_aws_profile(store, user_id, "work")
    store.push_connection(user_id, email, str(connection_id))

    (record,) = store.list_connections(user_id)
    assert record.encrypted_payload is None and record.payload is None
    assert record.metadata["credential_source"] == "local_profile"
    assert record.metadata["placement"]["aws_profile"] == "work"
    stored = cli.sync_documents_by_email[email][CONNECTION_DOCUMENT_KIND][str(connection_id)]
    assert stored["encrypted_payload"] is None and stored["metadata"]["placement"]["aws_profile"] == "work"
    (view,) = store.list_views(user_id)
    assert view.availability is ConnectionAvailability.AVAILABLE and view.credential_source == "local_profile"
    assert store.credentials_by_connection_id([user_id]) == {}
    # Locked: a pasted connection would read LOCKED; the profile one is still usable.
    delete_dek(store.paths, user_id)
    (view,) = store.list_views(user_id)
    assert view.availability is ConnectionAvailability.AVAILABLE
    assert store.has_encrypted_connections(user_id) is False


def test_a_connection_written_with_an_unknown_credential_source_is_read_only(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    store = make_connection_store_for_test(tmp_path, cli)
    user_id, email = _unlocked_account(store.paths)
    cli.seed_connection_document(
        email,
        str(ConnectionId.generate()),
        {"backend": "aws", "alias": "future", "connection_format": 1, "credential_source": "vault"},
        encrypted_payload=None,
    )
    store.pull(user_id, email)

    (view,) = store.list_views(user_id)
    assert view.availability is ConnectionAvailability.READ_ONLY
    # Read-only means an update cannot rewrite the newer app's source either.
    with pytest.raises(ConnectionTooNewError):
        store.update_connection(
            user_id, view.connection_id, alias="mine now", identifier="AKIA…PLE9", credentials=_AWS_CREDENTIALS
        )
    (record,) = store.list_connections(user_id)
    assert record.metadata["credential_source"] == "vault" and record.metadata["alias"] == "future"


def test_add_refuses_a_credential_source_that_disagrees_with_the_payload(tmp_path: Path) -> None:
    store = make_connection_store_for_test(tmp_path)
    with pytest.raises(WorkspaceSyncError, match="carries no credentials payload"):
        store.add_connection(
            LOCAL_PROFILE_USER_ID,
            ConnectionId.generate(),
            ConnectionBackend.AWS,
            "x",
            "x",
            _AWS_PLACEMENT,
            _AWS_CREDENTIALS,
            ConnectionCredentialSource.LOCAL_PROFILE,
        )
    with pytest.raises(WorkspaceSyncError, match="needs its credentials"):
        store.add_connection(
            LOCAL_PROFILE_USER_ID, ConnectionId.generate(), ConnectionBackend.AWS, "x", "x", _AWS_PLACEMENT, None
        )


def test_update_switches_a_pasted_connection_to_a_local_profile_and_back_keeping_its_placement(
    tmp_path: Path,
) -> None:
    store = make_connection_store_for_test(tmp_path)
    user_id, _email = _unlocked_account(store.paths)
    connection_id = _add_aws(store, user_id)

    store.update_connection(
        user_id,
        str(connection_id),
        alias="Work AWS",
        identifier="profile work",
        credentials=None,
        credential_source=ConnectionCredentialSource.LOCAL_PROFILE,
        placement={**_AWS_PLACEMENT, "aws_profile": "work"},
    )
    store.clear_validation_rollback(user_id, str(connection_id))

    (record,) = store.list_connections(user_id)
    assert record.encrypted_payload is None
    assert record.metadata["credential_source"] == "local_profile"
    assert record.metadata["placement"] == {**_AWS_PLACEMENT, "aws_profile": "work"}
    (view,) = store.list_views(user_id)
    assert view.availability is ConnectionAvailability.AVAILABLE

    store.update_connection(
        user_id,
        str(connection_id),
        alias="Work AWS",
        identifier="AKIA…PLE9",
        credentials=_AWS_CREDENTIALS,
        credential_source=ConnectionCredentialSource.PASTED,
        placement=_AWS_PLACEMENT,
    )
    store.clear_validation_rollback(user_id, str(connection_id))

    (record,) = store.list_connections(user_id)
    assert record.encrypted_payload is not None and record.metadata["credential_source"] == "pasted"
    assert record.metadata["placement"] == _AWS_PLACEMENT
    assert store.decrypt_credentials(user_id, record) == _AWS_CREDENTIALS


def test_rehome_moves_a_local_profile_connection_as_it_is(tmp_path: Path) -> None:
    cli: FakeImbueCloudCli = make_fake_imbue_cloud_cli()
    store = make_connection_store_for_test(tmp_path, cli)
    user_id, email = _unlocked_account(store.paths)
    connection_id = _add_aws_profile(store, user_id, "work")
    store.push_connection(user_id, email, str(connection_id))

    rehomed = store.rehome_account_connections_to_local(user_id, email)

    assert rehomed == [str(connection_id)]
    (local_record,) = store.list_connections(LOCAL_PROFILE_USER_ID)
    assert local_record.doc_id == str(connection_id) and local_record.payload is None
    assert local_record.metadata["placement"]["aws_profile"] == "work"
    stored = cli.sync_documents_by_email[email][CONNECTION_DOCUMENT_KIND][str(connection_id)]
    assert stored["state"] == CONNECTION_STATE_DELETED
