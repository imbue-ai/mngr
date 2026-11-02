"""Generic local replica + sync engine for one kind of account document.

The abstract base every account-document client builds on: one replica file
per account under a kind-specific directory, dirty rows that win until pushed,
CAS pushes with one rebase on conflict (local wins), tombstones that delete
the local row once it is clean, and a too-new gate that keeps this app from
rewriting a document a newer app wrote. ``ConnectionStore`` layers the
connection-specific machinery (payload encryption, validation rollback,
materialization) on top; ``AccountSettingsStore`` is a second, thinner client.

Subclasses configure the kind, the replica location, and the record type, and
may tighten the policy hooks (what counts as too new, which dirty rows a sync
pass must leave alone, what happens to a clean row the server no longer
holds). Thread-safe via one internal lock; CLI calls happen outside the lock
so a slow connector round-trip never blocks readers.
"""

import json
import threading
from abc import ABC
from abc import abstractmethod
from pathlib import Path
from typing import Generic

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.account_documents.data_types import DOCUMENT_STATE_DELETED
from imbue.minds.desktop_client.account_documents.data_types import DocumentPullOutcome
from imbue.minds.desktop_client.account_documents.data_types import RecordT
from imbue.minds.desktop_client.account_documents.data_types import document_record_from_wire
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudSyncConflictCliError
from imbue.minds.errors import AccountDocumentRemovedElsewhereError
from imbue.minds.errors import AccountDocumentTooNewError
from imbue.minds.errors import WorkspaceSyncError
from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr_imbue_cloud.wire_types import SUPPORTED_DOCUMENT_FORMAT


class AccountDocumentStore(MutableModel, ABC, Generic[RecordT]):
    """Owns one document kind's per-account replica files and every push/pull of its documents."""

    cli: ImbueCloudCli | None = Field(default=None, frozen=True, description="Transport; None disables pushes/pulls")
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _records_by_user_id: dict[str, dict[str, RecordT]] = PrivateAttr(default_factory=dict)
    _is_loaded: bool = PrivateAttr(default=False)

    # Kind configuration
    @abstractmethod
    def _document_kind(self) -> str:
        """The account-document kind this store's documents are stored under."""

    @abstractmethod
    def _replica_dir(self) -> Path:
        """The directory holding one ``<user_id>.json`` replica file per account."""

    @abstractmethod
    def _file_entries_key(self) -> str:
        """The key the replica file's record list is stored under."""

    @abstractmethod
    def _record_type(self) -> type[RecordT]:
        """The replica row type, parsed from file entries and wire documents."""

    @abstractmethod
    def _too_new_error(self) -> AccountDocumentTooNewError:
        """The kind's error for a write refused because a newer app wrote the document."""

    @abstractmethod
    def _removed_elsewhere_error(self) -> AccountDocumentRemovedElsewhereError:
        """The kind's error for a push that would resurrect a tombstone another device wrote."""

    # Policy hooks
    def _is_record_too_new(self, record: RecordT) -> bool:
        """Whether this app must treat the document as read-only (written by a newer app version)."""
        return record.document_format > SUPPORTED_DOCUMENT_FORMAT

    def _is_dirty_push_deferred(self, record: RecordT) -> bool:
        """Whether a sync pass must leave this dirty row for its own route to push."""
        return False

    def _is_pull_update_blocked(self, local: RecordT) -> bool:
        """Whether a pull must leave this row exactly as it is (a route is mid-change on it)."""
        return False

    def _on_pull_left_unmatched(self, record: RecordT) -> RecordT | None:
        """What becomes of a clean local row the reached server holds no document for; None deletes it."""
        return None

    def _is_payload_included_on_push(self) -> bool:
        """Whether pushes carry the payload key (an explicit null clears the server's blob)."""
        return True

    # Replica persistence
    def _replica_path(self, user_id: str) -> Path:
        return self._replica_dir() / f"{user_id}.json"

    def _load_unlocked(self) -> None:
        if self._is_loaded:
            return
        self._records_by_user_id = {}
        replica_dir = self._replica_dir()
        entries_key = self._file_entries_key()
        if replica_dir.is_dir():
            for path in sorted(replica_dir.glob("*.json")):
                user_id = path.stem
                try:
                    raw = json.loads(path.read_text())
                except (OSError, json.JSONDecodeError) as e:
                    logger.warning("Ignoring unreadable {} replica {}: {}", self._document_kind(), path, e)
                    continue
                entries = raw.get(entries_key, []) if isinstance(raw, dict) else None
                if not isinstance(entries, list):
                    logger.warning(
                        "Ignoring {} replica {} without a {} list", self._document_kind(), path, entries_key
                    )
                    continue
                by_doc_id: dict[str, RecordT] = {}
                for entry in entries:
                    if not isinstance(entry, dict):
                        logger.warning(
                            "Skipping a {} replica entry in {} that is not an object: {!r}",
                            self._document_kind(),
                            path,
                            entry,
                        )
                        continue
                    try:
                        record = self._record_type().model_validate(entry)
                    except ValueError as e:
                        logger.warning("Skipping malformed {} record in {}: {}", self._document_kind(), path, e)
                        continue
                    by_doc_id[record.doc_id] = record
                self._records_by_user_id[user_id] = by_doc_id
        self._is_loaded = True

    def _save_unlocked(self, user_id: str) -> None:
        path = self._replica_path(user_id)
        by_doc_id = self._records_by_user_id.get(user_id, {})
        payload = {self._file_entries_key(): [record.model_dump(mode="json") for record in by_doc_id.values()]}
        atomic_write(path, json.dumps(payload, indent=2))

    def _set_record_unlocked(self, user_id: str, record: RecordT) -> None:
        self._load_unlocked()
        self._records_by_user_id.setdefault(user_id, {})[record.doc_id] = record
        self._save_unlocked(user_id)

    def _drop_record_unlocked(self, user_id: str, doc_id: str) -> None:
        self._load_unlocked()
        by_doc_id = self._records_by_user_id.get(user_id, {})
        if doc_id in by_doc_id:
            del by_doc_id[doc_id]
            self._save_unlocked(user_id)

    # Read API
    def list_records(self, user_id: str) -> list[RecordT]:
        with self._lock:
            self._load_unlocked()
            return list(self._records_by_user_id.get(user_id, {}).values())

    def get_record(self, user_id: str, doc_id: str) -> RecordT | None:
        with self._lock:
            self._load_unlocked()
            return self._records_by_user_id.get(user_id, {}).get(doc_id)

    # Push / pull
    def _push_document(self, user_id: str, account_email: str, record: RecordT) -> RecordT:
        """CAS-push one document; on a revision conflict, rebase once onto the stored revision (local wins)."""
        if self.cli is None:
            raise WorkspaceSyncError(f"{self._document_kind()} sync is not configured (no imbue_cloud CLI)")
        if self._is_record_too_new(record):
            raise self._too_new_error()
        kind = self._document_kind()
        is_payload_included = self._is_payload_included_on_push()
        wire = record.to_wire(kind, record.revision + 1, is_payload_included=is_payload_included)
        try:
            stored = self.cli.sync_document_push(account_email, wire)
        except ImbueCloudSyncConflictCliError as conflict:
            if conflict.stored_record is None:
                if record.state == DOCUMENT_STATE_DELETED:
                    # The server holds no row (e.g. retention purged another device's
                    # tombstone), so the delete is already complete there.
                    logger.debug(
                        "{} document {} is already gone on the server; dropping its tombstone", kind, record.doc_id
                    )
                    with self._lock:
                        self._drop_record_unlocked(user_id, record.doc_id)
                    return record.model_copy_update(to_update(record.field_ref().is_dirty, False))
                raise
            stored_record = document_record_from_wire(self._record_type(), conflict.stored_record)
            if self._is_record_too_new(stored_record):
                raise self._too_new_error() from conflict
            if stored_record.state == DOCUMENT_STATE_DELETED:
                if record.state == DOCUMENT_STATE_DELETED:
                    # Both sides removed it: the delete is complete, whichever device landed it.
                    with self._lock:
                        self._drop_record_unlocked(user_id, record.doc_id)
                    return stored_record
                raise self._removed_elsewhere_error() from conflict
            server_revision = int(str(conflict.stored_record.get("revision", 0)))
            rebased = record.to_wire(kind, server_revision + 1, is_payload_included=is_payload_included)
            stored = self.cli.sync_document_push(account_email, rebased)
        acked = record.model_copy_update(
            to_update(record.field_ref().revision, int(str(stored.get("revision", record.revision + 1)))),
            to_update(record.field_ref().is_dirty, False),
        )
        with self._lock:
            self._load_unlocked()
            current = self._records_by_user_id.get(user_id, {}).get(record.doc_id)
            if current != record:
                # The row changed (or was dropped) while the push was on the
                # wire: the newer row stands, still dirty, and its next push
                # CAS-conflicts on the acked revision and rebases onto it.
                return acked
            if acked.state == DOCUMENT_STATE_DELETED:
                self._drop_record_unlocked(user_id, acked.doc_id)
            else:
                self._set_record_unlocked(user_id, acked)
        return acked

    def push_dirty(self, user_id: str, account_email: str) -> None:
        """Push the dirty rows a sync pass may flush: edits and tombstones no route is mid-change on."""
        for record in self.list_records(user_id):
            if not record.is_dirty or self._is_dirty_push_deferred(record):
                continue
            try:
                self._push_document(user_id, account_email, record)
            except AccountDocumentRemovedElsewhereError as e:
                # The server's tombstone wins over the local edit; without dropping the
                # row, the dirty copy would win every pull and fail every push.
                logger.warning(
                    "Dropping the local change to {} document {}: {}", self._document_kind(), record.doc_id, e
                )
                with self._lock:
                    self._drop_record_unlocked(user_id, record.doc_id)
            except (ImbueCloudCliError, WorkspaceSyncError) as e:
                logger.warning("Deferred dirty {} push for {}: {}", self._document_kind(), record.doc_id, e)

    def pull(self, user_id: str, account_email: str) -> DocumentPullOutcome:
        """Merge the server's documents of this kind into the replica (dirty rows win until pushed)."""
        if self.cli is None:
            return DocumentPullOutcome(is_reached=False, is_changed=False)
        try:
            wire_documents = self.cli.sync_documents_pull(account_email, self._document_kind())
        except ImbueCloudCliError as e:
            logger.warning("Could not pull the {} documents of {}: {}", self._document_kind(), account_email, e)
            return DocumentPullOutcome(is_reached=False, is_changed=False)
        with self._lock:
            self._load_unlocked()
            by_doc_id = self._records_by_user_id.setdefault(user_id, {})
            before = dict(by_doc_id)
            server_doc_ids: set[str] = set()
            for wire in wire_documents:
                server_record = document_record_from_wire(self._record_type(), wire)
                server_doc_ids.add(server_record.doc_id)
                local = by_doc_id.get(server_record.doc_id)
                if local is not None and self._is_pull_update_blocked(local):
                    # Mid-change: the route settles the row before any sync touches it.
                    continue
                if local is not None and local.is_dirty:
                    if not self._is_record_too_new(server_record):
                        continue
                    # This app could never push over a row a newer app wrote, so the server copy wins.
                    logger.warning(
                        "Dropping the unpushed local change to {} document {}: a newer app version rewrote it",
                        self._document_kind(),
                        server_record.doc_id,
                    )
                if server_record.state == DOCUMENT_STATE_DELETED:
                    by_doc_id.pop(server_record.doc_id, None)
                    continue
                by_doc_id[server_record.doc_id] = server_record
            for doc_id in list(by_doc_id):
                if doc_id not in server_doc_ids and not by_doc_id[doc_id].is_dirty:
                    replacement = self._on_pull_left_unmatched(by_doc_id[doc_id])
                    if replacement is None:
                        del by_doc_id[doc_id]
                    else:
                        by_doc_id[doc_id] = replacement
            is_changed = by_doc_id != before
            if is_changed:
                self._save_unlocked(user_id)
        return DocumentPullOutcome(is_reached=True, is_changed=is_changed)
