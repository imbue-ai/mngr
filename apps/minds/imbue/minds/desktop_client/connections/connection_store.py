"""Local replica + sync engine for per-account cloud connections.

Mirrors ``WorkspaceRecordStore``: one replica file per account under
``<data_dir>/connections/``, dirty rows that win until pushed, CAS pushes with
one rebase on conflict, and tombstones that delete the local row once it is
clean. The credentials payload stays encrypted at rest and is decrypted on
demand with the account DEK. The local profile (``local.json``) holds
unsynced connections with a plaintext payload and is never pushed.
"""

import json
import threading
from base64 import b64decode
from base64 import b64encode
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError

from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.secret_wrapping import SecretWrappingError
from imbue.imbue_common.secret_wrapping import decrypt_secrets
from imbue.imbue_common.secret_wrapping import encrypt_secrets
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client import dek_store
from imbue.minds.desktop_client.connections.data_types import CONNECTION_STATE_ACTIVE
from imbue.minds.desktop_client.connections.data_types import CONNECTION_STATE_DELETED
from imbue.minds.desktop_client.connections.data_types import ConnectionAvailability
from imbue.minds.desktop_client.connections.data_types import ConnectionCredentials
from imbue.minds.desktop_client.connections.data_types import ConnectionMetadata
from imbue.minds.desktop_client.connections.data_types import ConnectionPullOutcome
from imbue.minds.desktop_client.connections.data_types import ConnectionReplicaRecord
from imbue.minds.desktop_client.connections.data_types import ConnectionView
from imbue.minds.desktop_client.connections.data_types import connection_record_from_wire
from imbue.minds.desktop_client.connections.data_types import parse_connection_backend
from imbue.minds.desktop_client.connections.primitives import CONNECTION_DOCUMENT_KIND
from imbue.minds.desktop_client.connections.primitives import ConnectionBackend
from imbue.minds.desktop_client.connections.primitives import ConnectionId
from imbue.minds.desktop_client.connections.primitives import LOCAL_PROFILE_USER_ID
from imbue.minds.desktop_client.connections.primitives import SUPPORTED_CONNECTION_FORMAT
from imbue.minds.desktop_client.connections.primitives import SUPPORTED_CREDENTIALS_PAYLOAD_FORMAT
from imbue.minds.desktop_client.connections.primitives import connection_instance_name
from imbue.minds.desktop_client.connections.primitives import mngr_backend_name
from imbue.minds.desktop_client.connections.primitives import provider_instance_name_for_connection
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudSyncConflictCliError
from imbue.minds.errors import ConnectionNotFoundError
from imbue.minds.errors import ConnectionRemovedElsewhereError
from imbue.minds.errors import ConnectionRequiresMasterPasswordError
from imbue.minds.errors import ConnectionTooNewError
from imbue.minds.errors import ConnectionUpdateInProgressError
from imbue.minds.errors import WorkspaceSyncError
from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr_imbue_cloud.wire_types import SUPPORTED_DOCUMENT_FORMAT

_CONNECTIONS_DIRNAME: Final[str] = "connections"

CONNECTION_TOO_NEW_MESSAGE: Final[str] = (
    "This connection was added by a newer version of the app; update the app to manage it."
)


def is_connection_metadata_too_new(record: ConnectionReplicaRecord) -> bool:
    """Whether this app must treat the connection as read-only (written by a newer app version)."""
    if record.document_format > SUPPORTED_DOCUMENT_FORMAT:
        return True
    raw_format = record.metadata.get("connection_format", 1)
    return isinstance(raw_format, int) and raw_format > SUPPORTED_CONNECTION_FORMAT


def parse_connection_metadata(record: ConnectionReplicaRecord) -> ConnectionMetadata | None:
    """The typed view of a record's metadata, or None when it does not even carry a backend."""
    try:
        return ConnectionMetadata.model_validate(record.metadata)
    except ValueError as e:
        logger.warning("Skipping connection {} with malformed metadata: {}", record.doc_id, e)
        return None


def _encode_encrypted_payload(dek: bytes, plaintext: Mapping[str, object]) -> str:
    canonical = json.dumps(dict(plaintext), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return b64encode(encrypt_secrets(dek, canonical)).decode("ascii")


def _decode_encrypted_payload(dek: bytes, encrypted: str, doc_id: str) -> dict[str, Any] | None:
    """Decrypt a blob to its raw JSON dict (unknown keys intact), or None when undecryptable."""
    try:
        plaintext = decrypt_secrets(dek, b64decode(encrypted))
    except (SecretWrappingError, ValueError) as e:
        logger.warning("Could not decrypt the credentials of connection {}: {}", doc_id, e)
        return None
    try:
        raw = json.loads(plaintext)
    except ValueError as e:
        logger.warning("Malformed credentials payload for connection {}: {}", doc_id, e)
        return None
    if not isinstance(raw, dict):
        logger.warning("Credentials payload for connection {} is not a JSON object", doc_id)
        return None
    return raw


def is_credentials_payload_too_new(raw: Mapping[str, Any]) -> bool:
    """Whether a decrypted payload was written by a newer app version (its format is above the supported one)."""
    raw_format = raw.get("payload_format", 1)
    return isinstance(raw_format, int) and raw_format > SUPPORTED_CREDENTIALS_PAYLOAD_FORMAT


def _credentials_from_raw(raw: Mapping[str, Any], doc_id: str) -> ConnectionCredentials | None:
    if is_credentials_payload_too_new(raw):
        logger.warning("Leaving the credentials of connection {} unused: written by a newer app version", doc_id)
        return None
    try:
        return ConnectionCredentials.model_validate(dict(raw))
    except ValidationError as e:
        # The failing values are the credentials themselves, so only the shape of the failure is loggable.
        logger.warning("Malformed credentials payload for connection {}: {} field(s) invalid", doc_id, e.error_count())
        return None


def _check_no_update_in_progress(record: ConnectionReplicaRecord) -> None:
    """Refuse to change a row whose in-place update is still validating its new credentials."""
    if record.validation_rollback is not None:
        raise ConnectionUpdateInProgressError(
            f"Connection {record.doc_id} is being updated; wait for the update to finish"
        )


class ConnectionStore(MutableModel):
    """Owns the per-account connection replica files, the local profile, and every document push/pull.

    Thread-safe via one internal lock; CLI calls happen outside the lock so a
    slow connector round-trip never blocks readers.
    """

    paths: InstallationPaths = Field(frozen=True, description="Minds data dir (replica + keys live under it)")
    cli: ImbueCloudCli | None = Field(default=None, frozen=True, description="Transport; None disables pushes/pulls")
    device_label: str = Field(frozen=True, description="This device's human-readable name")
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _records_by_user_id: dict[str, dict[str, ConnectionReplicaRecord]] = PrivateAttr(default_factory=dict)
    _is_loaded: bool = PrivateAttr(default=False)

    # Replica persistence
    def _connections_dir(self) -> Path:
        return self.paths.data_dir / _CONNECTIONS_DIRNAME

    def _replica_path(self, user_id: str) -> Path:
        return self._connections_dir() / f"{user_id}.json"

    def _load_unlocked(self) -> None:
        if self._is_loaded:
            return
        self._records_by_user_id = {}
        connections_dir = self._connections_dir()
        if connections_dir.is_dir():
            for path in sorted(connections_dir.glob("*.json")):
                user_id = path.stem
                try:
                    raw = json.loads(path.read_text())
                except (OSError, json.JSONDecodeError) as e:
                    logger.warning("Ignoring unreadable connection replica {}: {}", path, e)
                    continue
                entries = raw.get("connections", []) if isinstance(raw, dict) else None
                if not isinstance(entries, list):
                    logger.warning("Ignoring connection replica {} without a connections list", path)
                    continue
                by_doc_id: dict[str, ConnectionReplicaRecord] = {}
                for entry in entries:
                    if not isinstance(entry, dict):
                        logger.warning(
                            "Skipping a connection replica entry in {} that is not an object: {!r}", path, entry
                        )
                        continue
                    try:
                        record = ConnectionReplicaRecord.model_validate(entry)
                    except ValueError as e:
                        logger.warning("Skipping malformed connection record in {}: {}", path, e)
                        continue
                    by_doc_id[record.doc_id] = record
                self._records_by_user_id[user_id] = by_doc_id
        self._is_loaded = True

    def _save_unlocked(self, user_id: str) -> None:
        path = self._replica_path(user_id)
        by_doc_id = self._records_by_user_id.get(user_id, {})
        payload = {"connections": [record.model_dump(mode="json") for record in by_doc_id.values()]}
        atomic_write(path, json.dumps(payload, indent=2))

    def _set_record_unlocked(self, user_id: str, record: ConnectionReplicaRecord) -> None:
        self._load_unlocked()
        self._records_by_user_id.setdefault(user_id, {})[record.doc_id] = record
        self._save_unlocked(user_id)

    def _drop_record_unlocked(self, user_id: str, doc_id: str) -> None:
        self._load_unlocked()
        by_doc_id = self._records_by_user_id.get(user_id, {})
        if doc_id in by_doc_id:
            del by_doc_id[doc_id]
            self._save_unlocked(user_id)

    def retain_unpushed_removals(self, user_id: str) -> None:
        """Forget an account's replica on signout, except the removals its session could not push.

        A dirty tombstone stays (without its payload: the push sends an explicit
        null anyway, and a retained row must not read as locked credentials),
        so the pull after a re-signin keeps it over the server's active row and
        the first reachable pass tombstones it there; the file is deleted only
        when nothing remains. The local profile is never cleared this way.
        """
        if user_id == LOCAL_PROFILE_USER_ID:
            return
        with self._lock:
            self._load_unlocked()
            dropped = self._records_by_user_id.pop(user_id, {})
            retained = {
                doc_id: record.model_copy_update(to_update(record.field_ref().encrypted_payload, None))
                for doc_id, record in dropped.items()
                if record.is_dirty and record.state == CONNECTION_STATE_DELETED
            }
            if retained:
                logger.info(
                    "Retained {} queued connection removal(s) of account {} across its signout",
                    len(retained),
                    user_id[:8],
                )
                self._records_by_user_id[user_id] = retained
                self._save_unlocked(user_id)
                return
            try:
                self._replica_path(user_id).unlink(missing_ok=True)
            except OSError as e:
                raise WorkspaceSyncError(f"Could not remove the connection replica for {user_id[:8]}: {e}") from e

    def discard_unregistered_connections(self) -> int:
        """Undo the registrations and updates this process never finished; returns how many rows changed.

        The add route writes an ACTIVE, dirty, revision-0 row before it
        validates the credentials and pushes, and discards it when anything
        stops the registration -- except the process itself dying, which no
        ``finally`` sees. At startup no registration is in flight, so such a
        row is that residue: the materializer would offer it as usable here
        while :meth:`push_dirty` (rightly) never syncs it. A push that reached
        the connector before the death comes back clean on the next pull.
        Likewise a row still carrying a ``validation_rollback`` is an update
        whose new credentials were never validated: the previous row comes
        back, for the local profile too.
        """
        dropped_count = 0
        with self._lock:
            self._load_unlocked()
            for user_id, by_doc_id in self._records_by_user_id.items():
                rolled_back_doc_ids = [
                    doc_id for doc_id, record in by_doc_id.items() if record.validation_rollback is not None
                ]
                for doc_id in rolled_back_doc_ids:
                    logger.warning(
                        "Restoring the previous credentials of connection {} of owner {}: its update did not complete",
                        doc_id,
                        user_id[:8],
                    )
                    by_doc_id[doc_id] = self._rollback_row_of(by_doc_id[doc_id])
                if rolled_back_doc_ids:
                    self._save_unlocked(user_id)
                    dropped_count += len(rolled_back_doc_ids)
                if user_id == LOCAL_PROFILE_USER_ID:
                    continue
                unregistered_doc_ids = [
                    doc_id
                    for doc_id, record in by_doc_id.items()
                    if record.state == CONNECTION_STATE_ACTIVE and record.revision == 0 and record.is_dirty
                ]
                for doc_id in unregistered_doc_ids:
                    logger.warning(
                        "Dropping connection {} of owner {}: its registration did not complete", doc_id, user_id[:8]
                    )
                    del by_doc_id[doc_id]
                if unregistered_doc_ids:
                    self._save_unlocked(user_id)
                    dropped_count += len(unregistered_doc_ids)
        return dropped_count

    # Read API
    def list_connections(self, user_id: str) -> list[ConnectionReplicaRecord]:
        with self._lock:
            self._load_unlocked()
            return list(self._records_by_user_id.get(user_id, {}).values())

    def list_active_connections(self, user_id: str) -> list[ConnectionReplicaRecord]:
        return [record for record in self.list_connections(user_id) if record.state == CONNECTION_STATE_ACTIVE]

    def has_encrypted_connections(self, user_id: str) -> bool:
        """Whether any of the account's connections carries credentials encrypted under its DEK.

        Such a record proves a master password was set for the account somewhere,
        so a device without the DEK counts the account as locked even before any
        workspace record or key-bundle mirror reaches it.
        """
        return any(record.encrypted_payload is not None for record in self.list_connections(user_id))

    def list_all_connections(self) -> dict[str, list[ConnectionReplicaRecord]]:
        """Every replica row keyed by owner user id (the local profile included)."""
        with self._lock:
            self._load_unlocked()
            return {user_id: list(by_doc_id.values()) for user_id, by_doc_id in self._records_by_user_id.items()}

    def find_active_connection(self, doc_id: str) -> tuple[str, ConnectionReplicaRecord] | None:
        """``(owner user id, record)`` for the ACTIVE connection with this id, or None."""
        for user_id, records in self.list_all_connections().items():
            for record in records:
                if record.doc_id == doc_id and record.state == CONNECTION_STATE_ACTIVE:
                    return user_id, record
        return None

    def find_active_connection_by_instance_name(
        self, instance_name: str
    ) -> tuple[str, ConnectionReplicaRecord] | None:
        for user_id, records in self.list_all_connections().items():
            for record in records:
                if record.state == CONNECTION_STATE_ACTIVE and self.instance_name_for(record) == instance_name:
                    return user_id, record
        return None

    def instance_name_for(self, record: ConnectionReplicaRecord) -> str | None:
        """The provider instance name of a record this app can materialize, or None (unknown backend, malformed row)."""
        metadata = parse_connection_metadata(record)
        if metadata is None:
            return None
        return self._instance_name_for_metadata(record, metadata)

    def _instance_name_for_metadata(self, record: ConnectionReplicaRecord, metadata: ConnectionMetadata) -> str | None:
        backend = parse_connection_backend(metadata)
        if backend is None:
            return None
        try:
            connection_id = ConnectionId(record.doc_id)
        except ValueError:
            logger.warning("Connection document id {!r} is not a connection id; ignoring it", record.doc_id)
            return None
        return provider_instance_name_for_connection(backend, connection_id)

    def _is_backend_unknown(self, record: ConnectionReplicaRecord) -> bool:
        """Whether the record names a backend this app cannot materialize (a newer app added it)."""
        metadata = parse_connection_metadata(record)
        return metadata is not None and parse_connection_backend(metadata) is None

    def _unknown_backend_instance_name(
        self, record: ConnectionReplicaRecord, metadata: ConnectionMetadata
    ) -> str | None:
        try:
            connection_id = ConnectionId(record.doc_id)
        except ValueError:
            logger.warning("Connection document id {!r} is not a connection id; ignoring it", record.doc_id)
            return None
        return connection_instance_name(metadata.backend, connection_id)

    def availability(self, user_id: str, record: ConnectionReplicaRecord) -> ConnectionAvailability:
        return self._availability_for_backend_known(
            user_id, record, is_backend_unknown=self._is_backend_unknown(record)
        )

    def _availability_for_backend_known(
        self, user_id: str, record: ConnectionReplicaRecord, *, is_backend_unknown: bool
    ) -> ConnectionAvailability:
        if is_connection_metadata_too_new(record) or is_backend_unknown:
            return ConnectionAvailability.READ_ONLY
        if user_id == LOCAL_PROFILE_USER_ID:
            if record.payload is None:
                return ConnectionAvailability.NO_CREDENTIALS
            return self._parsed_availability(user_id, record)
        if record.encrypted_payload is None:
            return ConnectionAvailability.NO_CREDENTIALS
        if not dek_store.is_account_unlocked(self.paths, user_id):
            return ConnectionAvailability.LOCKED
        return self._parsed_availability(user_id, record)

    def _parsed_availability(self, user_id: str, record: ConnectionReplicaRecord) -> ConnectionAvailability:
        """AVAILABLE when the payload parses as this app's credentials; READ_ONLY when a newer app wrote it.

        A payload this device cannot decrypt or parse at all is NO_CREDENTIALS,
        not READ_ONLY: nothing newer wrote it, so the row must stay removable.
        """
        raw = self._raw_credentials(user_id, record)
        if raw is None:
            return ConnectionAvailability.NO_CREDENTIALS
        if is_credentials_payload_too_new(raw):
            return ConnectionAvailability.READ_ONLY
        if _credentials_from_raw(raw, record.doc_id) is None:
            return ConnectionAvailability.NO_CREDENTIALS
        return ConnectionAvailability.AVAILABLE

    def view_for(self, user_id: str, record: ConnectionReplicaRecord) -> ConnectionView | None:
        """The UI view of an ACTIVE connection, or None for tombstones and rows without a usable id/backend."""
        if record.state != CONNECTION_STATE_ACTIVE:
            return None
        # Parsed once here: a malformed row would otherwise warn once per helper on every listing.
        metadata = parse_connection_metadata(record)
        if metadata is None:
            return None
        is_backend_unknown = parse_connection_backend(metadata) is None
        if is_backend_unknown:
            # Shown read-only under the name a newer app gives it, never dropped.
            instance_name = self._unknown_backend_instance_name(record, metadata)
        else:
            instance_name = self._instance_name_for_metadata(record, metadata)
        if instance_name is None:
            return None
        return ConnectionView(
            connection_id=record.doc_id,
            instance_name=instance_name,
            backend=metadata.backend,
            alias=metadata.alias,
            identifier=metadata.identifier,
            placement=dict(metadata.placement),
            owner_user_id=user_id,
            availability=self._availability_for_backend_known(user_id, record, is_backend_unknown=is_backend_unknown),
        )

    def list_views(self, user_id: str) -> list[ConnectionView]:
        views = [self.view_for(user_id, record) for record in self.list_connections(user_id)]
        return [view for view in views if view is not None]

    def list_all_views(self) -> list[ConnectionView]:
        views: list[ConnectionView] = []
        for user_id in sorted(self.list_all_connections()):
            views.extend(self.list_views(user_id))
        return views

    # Credentials
    def _raw_credentials(self, user_id: str, record: ConnectionReplicaRecord) -> dict[str, Any] | None:
        """The connection's plaintext payload with unknown keys intact, or None when locked, scrubbed, or undecryptable."""
        if user_id == LOCAL_PROFILE_USER_ID:
            return dict(record.payload) if record.payload is not None else None
        if record.encrypted_payload is None:
            return None
        dek = dek_store.load_dek(self.paths, user_id)
        if dek is None:
            return None
        return _decode_encrypted_payload(dek, record.encrypted_payload, record.doc_id)

    def decrypt_credentials(self, user_id: str, record: ConnectionReplicaRecord) -> ConnectionCredentials | None:
        """The connection's credentials, or None when locked, scrubbed, unparseable, or written by a newer app."""
        raw = self._raw_credentials(user_id, record)
        if raw is None:
            return None
        return _credentials_from_raw(raw, record.doc_id)

    def credentials_by_connection_id(self, user_ids: Sequence[str]) -> dict[str, ConnectionCredentials]:
        """The decryptable credentials of every ACTIVE connection owned by the given owners (local included)."""
        result: dict[str, ConnectionCredentials] = {}
        for user_id in [*user_ids, LOCAL_PROFILE_USER_ID]:
            for record in self.list_active_connections(user_id):
                if is_connection_metadata_too_new(record):
                    continue
                credentials = self.decrypt_credentials(user_id, record)
                if credentials is not None:
                    result[record.doc_id] = credentials
        return result

    @staticmethod
    def _rollback_row_of(record: ConnectionReplicaRecord) -> ConnectionReplicaRecord:
        """The previous row an unfinished update must restore; requires ``validation_rollback`` to be set."""
        if record.validation_rollback is None:
            raise WorkspaceSyncError(f"Connection {record.doc_id} carries no update to roll back")
        return record.validation_rollback

    # Add / update / remove
    def add_connection(
        self,
        owner_user_id: str,
        # Minted by the caller (it names the instance and, for Azure, the resource group in the placement).
        connection_id: ConnectionId,
        backend: ConnectionBackend,
        alias: str,
        identifier: str,
        placement: Mapping[str, Any],
        credentials: ConnectionCredentials,
    ) -> ConnectionReplicaRecord:
        """Store a new connection locally (dirty for an account; final for the local profile).

        The caller pushes an account-owned connection with :meth:`push_connection`
        once the credentials have been validated. Raises
        ``ConnectionRequiresMasterPasswordError`` when the owning account is
        locked here or has no master password.
        """
        metadata = ConnectionMetadata(
            connection_format=SUPPORTED_CONNECTION_FORMAT,
            backend=mngr_backend_name(backend),
            alias=alias,
            identifier=identifier,
            placement=dict(placement),
            created_on_device_label=self.device_label,
        )
        plaintext = credentials.to_plaintext_dict()
        if owner_user_id == LOCAL_PROFILE_USER_ID:
            record = ConnectionReplicaRecord(
                doc_id=str(connection_id),
                metadata=metadata.model_dump(mode="json"),
                payload=plaintext,
                is_dirty=False,
            )
        else:
            dek = dek_store.load_dek(self.paths, owner_user_id)
            if dek is None or not dek_store.is_master_password_set_for_account(self.paths, owner_user_id):
                raise ConnectionRequiresMasterPasswordError(
                    "Syncing a connection needs this account to be unlocked here with a master password set; "
                    "add it to this device only (the local profile) instead."
                )
            record = ConnectionReplicaRecord(
                doc_id=str(connection_id),
                metadata=metadata.model_dump(mode="json"),
                encrypted_payload=_encode_encrypted_payload(dek, plaintext),
                is_dirty=True,
            )
        with self._lock:
            self._set_record_unlocked(owner_user_id, record)
        logger.debug("Added {} connection {} for owner {}", backend.value, connection_id, owner_user_id[:8])
        return record

    def update_connection(
        self,
        owner_user_id: str,
        doc_id: str,
        alias: str,
        identifier: str,
        credentials: ConnectionCredentials,
    ) -> ConnectionReplicaRecord:
        """Replace a connection's credentials (and alias) in place, keeping its id, instance, backend, and placement.

        The row is stored dirty with ``validation_rollback`` set to the previous
        row, which this returns: until the caller clears it (the credentials
        validated and, for an account owner, pushed) or restores it, no sync
        pass pushes the row or overwrites it from the server. Keys this app
        does not know are carried through the metadata; the revision is kept.
        Raises ``ConnectionNotFoundError`` for an unknown id or a tombstone,
        ``ConnectionTooNewError`` for a row a newer app wrote,
        ``ConnectionUpdateInProgressError`` for a row another update is still
        validating, and ``ConnectionRequiresMasterPasswordError`` when the
        owning account cannot encrypt here.
        """
        with self._lock:
            self._load_unlocked()
            existing = self._records_by_user_id.get(owner_user_id, {}).get(doc_id)
        if existing is None or existing.state != CONNECTION_STATE_ACTIVE:
            raise ConnectionNotFoundError(f"No active connection {doc_id} for owner {owner_user_id[:8]}")
        if is_connection_metadata_too_new(existing):
            raise ConnectionTooNewError(CONNECTION_TOO_NEW_MESSAGE)
        _check_no_update_in_progress(existing)
        metadata = {**existing.metadata, "alias": alias, "identifier": identifier}
        plaintext = credentials.to_plaintext_dict()
        if owner_user_id == LOCAL_PROFILE_USER_ID:
            updated = existing.model_copy_update(
                to_update(existing.field_ref().metadata, metadata),
                to_update(existing.field_ref().payload, plaintext),
                to_update(existing.field_ref().is_dirty, True),
                to_update(existing.field_ref().validation_rollback, existing),
            )
        else:
            dek = dek_store.load_dek(self.paths, owner_user_id)
            if dek is None or not dek_store.is_master_password_set_for_account(self.paths, owner_user_id):
                raise ConnectionRequiresMasterPasswordError(
                    "Updating a synced connection needs this account to be unlocked here with a master password set."
                )
            updated = existing.model_copy_update(
                to_update(existing.field_ref().metadata, metadata),
                to_update(existing.field_ref().encrypted_payload, _encode_encrypted_payload(dek, plaintext)),
                to_update(existing.field_ref().is_dirty, True),
                to_update(existing.field_ref().validation_rollback, existing),
            )
        with self._lock:
            self._set_record_unlocked(owner_user_id, updated)
        logger.debug("Updated the credentials of connection {} for owner {}", doc_id, owner_user_id[:8])
        return existing

    def clear_validation_rollback(self, owner_user_id: str, doc_id: str) -> None:
        """The update's new credentials validated (and pushed): the previous row is no longer needed."""
        with self._lock:
            self._load_unlocked()
            record = self._records_by_user_id.get(owner_user_id, {}).get(doc_id)
            if record is None:
                raise ConnectionNotFoundError(f"No connection {doc_id} for owner {owner_user_id[:8]}")
            self._set_record_unlocked(
                owner_user_id, record.model_copy_update(to_update(record.field_ref().validation_rollback, None))
            )

    def restore_validation_rollback(self, owner_user_id: str, doc_id: str) -> None:
        """The update failed: put the previous row back as it was."""
        with self._lock:
            self._load_unlocked()
            record = self._records_by_user_id.get(owner_user_id, {}).get(doc_id)
            if record is None:
                raise ConnectionNotFoundError(f"No connection {doc_id} for owner {owner_user_id[:8]}")
            self._set_record_unlocked(owner_user_id, self._rollback_row_of(record))

    def remove_connection(
        self, owner_user_id: str, account_email: str | None, doc_id: str, *, is_push_attempted: bool = False
    ) -> None:
        """Tombstone a connection and push the tombstone (the local profile just deletes the row).

        A failed push leaves the dirty tombstone for the next sync pass; the
        row disappears from the replica once the server has acknowledged it.
        A row the server never acknowledged (revision 0) is simply dropped,
        unless ``is_push_attempted``: a push that failed may still have reached
        the connector, so the row is tombstoned and pushed like an acknowledged
        one rather than left to come back on the next pull.
        Raises ``ConnectionNotFoundError`` when the owner has no such active
        connection, ``ConnectionTooNewError`` for a newer-format row, and
        ``ConnectionUpdateInProgressError`` for a row whose new credentials
        are still being validated (a tombstone must never carry the rollback,
        and the update's settlement would undo the removal).
        """
        with self._lock:
            self._load_unlocked()
            existing = self._records_by_user_id.get(owner_user_id, {}).get(doc_id)
        if existing is None or existing.state != CONNECTION_STATE_ACTIVE:
            raise ConnectionNotFoundError(f"No active connection {doc_id} for owner {owner_user_id[:8]}")
        _check_no_update_in_progress(existing)
        # The local profile is never pushed, and a row no push was ever tried for
        # has nothing to tombstone on the server: both just drop.
        if owner_user_id == LOCAL_PROFILE_USER_ID or (existing.revision == 0 and not is_push_attempted):
            with self._lock:
                self._drop_record_unlocked(owner_user_id, doc_id)
            return
        if is_connection_metadata_too_new(existing):
            raise ConnectionTooNewError(CONNECTION_TOO_NEW_MESSAGE)
        tombstoned = existing.model_copy_update(
            to_update(existing.field_ref().state, CONNECTION_STATE_DELETED),
            to_update(existing.field_ref().encrypted_payload, None),
            to_update(existing.field_ref().is_dirty, True),
        )
        with self._lock:
            self._set_record_unlocked(owner_user_id, tombstoned)
        if self.cli is None or account_email is None:
            return
        try:
            self._push_document(owner_user_id, account_email, tombstoned)
        except (ImbueCloudCliError, WorkspaceSyncError) as e:
            logger.warning("Queued the tombstone for connection {} (push failed: {})", doc_id, e)

    # Push / pull
    def _push_document(
        self, user_id: str, account_email: str, record: ConnectionReplicaRecord
    ) -> ConnectionReplicaRecord:
        """CAS-push one document; on a revision conflict, rebase once onto the stored revision (local wins)."""
        if self.cli is None:
            raise WorkspaceSyncError("connection sync is not configured (no imbue_cloud CLI)")
        if is_connection_metadata_too_new(record):
            raise ConnectionTooNewError(CONNECTION_TOO_NEW_MESSAGE)
        # A tombstone sends an explicit null so the server clears the blob; an
        # active row always carries its blob (this device wrote it).
        wire = record.to_wire(CONNECTION_DOCUMENT_KIND, record.revision + 1, is_payload_included=True)
        try:
            stored = self.cli.sync_document_push(account_email, wire)
        except ImbueCloudSyncConflictCliError as conflict:
            if conflict.stored_record is None:
                if record.state == CONNECTION_STATE_DELETED:
                    # The server holds no row (e.g. retention purged another device's
                    # tombstone), so the delete is already complete there.
                    logger.debug("Connection {} is already gone on the server; dropping its tombstone", record.doc_id)
                    with self._lock:
                        self._drop_record_unlocked(user_id, record.doc_id)
                    return record.model_copy_update(to_update(record.field_ref().is_dirty, False))
                raise
            stored_record = connection_record_from_wire(conflict.stored_record)
            if is_connection_metadata_too_new(stored_record):
                raise ConnectionTooNewError(CONNECTION_TOO_NEW_MESSAGE) from conflict
            if stored_record.state == CONNECTION_STATE_DELETED:
                if record.state == CONNECTION_STATE_DELETED:
                    # Both sides removed it: the delete is complete, whichever device landed it.
                    with self._lock:
                        self._drop_record_unlocked(user_id, record.doc_id)
                    return stored_record
                raise ConnectionRemovedElsewhereError("This connection was removed on another device") from conflict
            server_revision = int(str(conflict.stored_record.get("revision", 0)))
            rebased = record.to_wire(CONNECTION_DOCUMENT_KIND, server_revision + 1, is_payload_included=True)
            stored = self.cli.sync_document_push(account_email, rebased)
        acked = record.model_copy_update(
            to_update(record.field_ref().revision, int(str(stored.get("revision", record.revision + 1)))),
            to_update(record.field_ref().is_dirty, False),
        )
        with self._lock:
            if acked.state == CONNECTION_STATE_DELETED:
                self._drop_record_unlocked(user_id, acked.doc_id)
            else:
                self._set_record_unlocked(user_id, acked)
        return acked

    def push_connection(self, user_id: str, account_email: str, doc_id: str) -> ConnectionReplicaRecord:
        """Push one connection now (the add route's synchronous push after the credentials validated).

        Raises ``WorkspaceSyncError`` when the push fails, leaving the row dirty
        (``ConnectionRemovedElsewhereError`` when another device removed it).
        """
        with self._lock:
            self._load_unlocked()
            record = self._records_by_user_id.get(user_id, {}).get(doc_id)
        if record is None:
            raise ConnectionNotFoundError(f"No connection {doc_id} for owner {user_id[:8]}")
        try:
            return self._push_document(user_id, account_email, record)
        except ImbueCloudCliError as e:
            raise WorkspaceSyncError(f"could not push the connection to the connector: {e}") from e

    def push_dirty(self, user_id: str, account_email: str) -> None:
        """Push the dirty rows a sync pass may flush: edits and tombstones of rows the server has acknowledged.

        A never-acknowledged ACTIVE row (revision 0) is an add whose credentials
        are still being validated; the add route pushes it itself with
        :meth:`push_connection`, so a pass that ran meanwhile must not sync it
        to other devices first.
        """
        for record in self.list_connections(user_id):
            if not record.is_dirty or (record.revision == 0 and record.state == CONNECTION_STATE_ACTIVE):
                continue
            if record.validation_rollback is not None:
                # An update whose credentials are still being validated; the update route pushes it itself.
                continue
            try:
                self._push_document(user_id, account_email, record)
            except ConnectionRemovedElsewhereError as e:
                # The server's tombstone wins over the local edit; without dropping the
                # row, the dirty copy would win every pull and fail every push.
                logger.warning("Dropping the local change to connection {}: {}", record.doc_id, e)
                with self._lock:
                    self._drop_record_unlocked(user_id, record.doc_id)
            except (ImbueCloudCliError, WorkspaceSyncError) as e:
                logger.warning("Deferred dirty connection push for {}: {}", record.doc_id, e)

    def pull(self, user_id: str, account_email: str) -> ConnectionPullOutcome:
        """Merge the server's connection documents into the replica (dirty rows win until pushed)."""
        if self.cli is None:
            return ConnectionPullOutcome(is_reached=False, is_changed=False)
        try:
            wire_documents = self.cli.sync_documents_pull(account_email, CONNECTION_DOCUMENT_KIND)
        except ImbueCloudCliError as e:
            logger.warning("Could not pull the connections of {}: {}", account_email, e)
            return ConnectionPullOutcome(is_reached=False, is_changed=False)
        with self._lock:
            self._load_unlocked()
            by_doc_id = self._records_by_user_id.setdefault(user_id, {})
            before = dict(by_doc_id)
            server_doc_ids: set[str] = set()
            for wire in wire_documents:
                server_record = connection_record_from_wire(wire)
                server_doc_ids.add(server_record.doc_id)
                local = by_doc_id.get(server_record.doc_id)
                if local is not None and local.validation_rollback is not None:
                    # Mid-update: the route settles the row (clears or restores) before any sync touches it.
                    continue
                if local is not None and local.is_dirty:
                    if not is_connection_metadata_too_new(server_record):
                        continue
                    # This app could never push over a row a newer app wrote, so the server copy wins.
                    logger.warning(
                        "Dropping the unpushed local change to connection {}: a newer app version rewrote it",
                        server_record.doc_id,
                    )
                if server_record.state == CONNECTION_STATE_DELETED:
                    by_doc_id.pop(server_record.doc_id, None)
                    continue
                by_doc_id[server_record.doc_id] = server_record
            for doc_id in list(by_doc_id):
                if doc_id not in server_doc_ids and not by_doc_id[doc_id].is_dirty:
                    del by_doc_id[doc_id]
            is_changed = by_doc_id != before
            if is_changed:
                self._save_unlocked(user_id)
        return ConnectionPullOutcome(is_reached=True, is_changed=is_changed)

    # Clear-password re-homing
    def rehome_account_connections_to_local(self, user_id: str, account_email: str) -> list[str]:
        """Move every decryptable connection of the account into the local profile, keeping its id.

        Run by the clear-master-password flow before the server-side scrub,
        which would otherwise strip the credentials from every device
        including this one. Returns the ids re-homed. The account documents
        are tombstoned (best-effort push) so other devices drop them.
        """
        rehomed: list[str] = []
        for record in self.list_active_connections(user_id):
            if is_connection_metadata_too_new(record) or record.validation_rollback is not None:
                continue
            raw = self._raw_credentials(user_id, record)
            if raw is None:
                continue
            # The typed parse gates on a supported, well-formed payload; the raw
            # dict is what moves, so keys a newer client added survive.
            credentials = _credentials_from_raw(raw, record.doc_id)
            if credentials is None:
                continue
            local_record = ConnectionReplicaRecord(
                doc_id=record.doc_id,
                metadata=dict(record.metadata),
                payload=raw,
                is_dirty=False,
            )
            with self._lock:
                self._set_record_unlocked(LOCAL_PROFILE_USER_ID, local_record)
            self.remove_connection(user_id, account_email, record.doc_id)
            rehomed.append(record.doc_id)
        return rehomed
