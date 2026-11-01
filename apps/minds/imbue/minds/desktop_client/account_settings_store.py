"""Per-account replicas of the synced ``settings`` account document (spec section 7.2).

One document per account: kind ``settings``, document id ``default``, whose
plaintext metadata holds the account-keyed settings. Replicas live at
``<data_dir>/account_documents/settings/<user_id>.json``, with ``local.json``
for the device's pseudo-account, which is never pushed (the sync scheduler
only passes over signed-in accounts). The metadata holds only the keys that
have explicitly been chosen -- the carrier's defaults answer the rest on read
-- so "has the user ever chosen this" stays answerable, exactly as it was
when absence from ``config.toml`` carried that meaning.

A record whose ``settings_format`` postdates this app stays readable (the
carrier tolerates unknown keys) but refuses writes, per spec section 4.3.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.model_update import to_update
from imbue.minds.desktop_client.account_documents.data_types import AccountDocumentRecord
from imbue.minds.desktop_client.account_documents.store import AccountDocumentStore
from imbue.minds.desktop_client.settings.schema import AccountSettings
from imbue.minds.desktop_client.settings.schema import SUPPORTED_SETTINGS_FORMAT
from imbue.minds.desktop_client.settings.schema import is_settings_format_supported
from imbue.minds.errors import AccountDocumentRemovedElsewhereError
from imbue.minds.errors import SettingValueError
from imbue.minds.errors import SettingsRecordTooNewError

SETTINGS_DOCUMENT_KIND: Final[str] = "settings"
# One settings document per account, so its id within the kind is fixed.
SETTINGS_DOCUMENT_ID: Final[str] = "default"

_ACCOUNT_DOCUMENTS_DIRNAME: Final[str] = "account_documents"

SETTINGS_RECORD_TOO_NEW_MESSAGE: Final[str] = (
    "These settings were saved by a newer version of Imbue Studio and can only be changed there."
)


def _stored_settings_format(record: AccountDocumentRecord) -> int:
    raw_format = record.metadata.get("settings_format", 1)
    return raw_format if isinstance(raw_format, int) and not isinstance(raw_format, bool) else 1


class AccountSettingsStore(AccountDocumentStore[AccountDocumentRecord]):
    """Owns the per-account settings replicas and every push/pull of the ``settings`` document."""

    data_dir: Path = Field(frozen=True, description="Minds data dir (the replicas live under it)")

    # The generic store's kind configuration
    def _document_kind(self) -> str:
        return SETTINGS_DOCUMENT_KIND

    def _replica_dir(self) -> Path:
        return self.data_dir / _ACCOUNT_DOCUMENTS_DIRNAME / SETTINGS_DOCUMENT_KIND

    def _file_entries_key(self) -> str:
        return "documents"

    def _record_type(self) -> type[AccountDocumentRecord]:
        return AccountDocumentRecord

    def _too_new_error(self) -> SettingsRecordTooNewError:
        return SettingsRecordTooNewError(SETTINGS_RECORD_TOO_NEW_MESSAGE)

    def _removed_elsewhere_error(self) -> AccountDocumentRemovedElsewhereError:
        return AccountDocumentRemovedElsewhereError("The settings document was removed on another device")

    def _is_record_too_new(self, record: AccountDocumentRecord) -> bool:
        if super()._is_record_too_new(record):
            return True
        return not is_settings_format_supported(_stored_settings_format(record))

    def _is_payload_included_on_push(self) -> bool:
        # The settings document is plaintext metadata only (spec S7).
        return False

    def _on_pull_left_unmatched(self, record: AccountDocumentRecord) -> AccountDocumentRecord | None:
        # A clean, never-pushed record is the migration's seed. The reached
        # server holding no settings document means this account has never
        # pushed one anywhere, so the seed becomes the initial document and
        # the next push creates it (spec section 7.5); a record the server
        # once acknowledged and no longer holds was removed there, and the
        # defaults return.
        if record.revision == 0:
            return record.model_copy_update(to_update(record.field_ref().is_dirty, True))
        return None

    # Reads
    def read_settings(self, user_id: str) -> AccountSettings:
        """The account's settings record; the declared defaults answer every key never chosen (or a malformed record)."""
        record = self.get_record(user_id, SETTINGS_DOCUMENT_ID)
        if record is None:
            return AccountSettings()
        try:
            return AccountSettings.model_validate(record.metadata)
        except ValidationError as e:
            logger.warning("The settings record of {} is malformed; resolving from defaults: {}", user_id[:8], e)
            return AccountSettings()

    def get_stored_settings_format(self, user_id: str) -> int:
        """The stored record's shape version (the supported one when no record exists yet)."""
        record = self.get_record(user_id, SETTINGS_DOCUMENT_ID)
        if record is None:
            return SUPPORTED_SETTINGS_FORMAT
        return max(_stored_settings_format(record), record.document_format)

    def is_notifications_enabled_chosen_anywhere(self) -> bool:
        """Whether any account's record (the device's included) carries an explicit master-toggle choice.

        Install-wide on purpose: "has the user chosen notification preferences"
        gates the one-time first-choice card, and before the settings moved to
        per-account records one install-wide copy answered it for every window.
        """
        with self._lock:
            self._load_unlocked()
            return any(
                "notifications_enabled" in record.metadata
                for by_doc_id in self._records_by_user_id.values()
                for record in by_doc_id.values()
            )

    # Writes
    def apply_settings_values(self, user_id: str, value_by_key: Mapping[str, object]) -> None:
        """Merge explicitly-chosen setting values into the account's record and mark it for the next push.

        Only the given keys change: keys never chosen stay absent (the
        carrier's defaults answer them), and keys a newer client wrote are
        carried through untouched. Raises ``SettingsRecordTooNewError`` for a
        record this app must not rewrite, and ``SettingValueError`` when the
        merged record does not validate against the carrier (a caller bug:
        routes validate their input shape before calling).
        """
        with self._lock:
            self._load_unlocked()
            existing = self._records_by_user_id.get(user_id, {}).get(SETTINGS_DOCUMENT_ID)
            if existing is not None and self._is_record_too_new(existing):
                raise self._too_new_error()
            metadata = dict(existing.metadata) if existing is not None else {}
            metadata.update(dict(value_by_key))
            metadata["settings_format"] = SUPPORTED_SETTINGS_FORMAT
            try:
                AccountSettings.model_validate(metadata)
            except ValidationError as e:
                raise SettingValueError(f"The settings write for {user_id[:8]} does not validate: {e}") from e
            if existing is not None:
                updated = existing.model_copy_update(
                    to_update(existing.field_ref().metadata, metadata),
                    to_update(existing.field_ref().is_dirty, True),
                )
            else:
                updated = AccountDocumentRecord(doc_id=SETTINGS_DOCUMENT_ID, metadata=metadata, is_dirty=True)
            self._set_record_unlocked(user_id, updated)

    def seed_if_absent(self, user_id: str, value_by_key: Mapping[str, object]) -> bool:
        """Create the account's record from migrated values when none exists yet; returns whether it did.

        The seed is stored clean at revision 0: the first pull replaces it
        when the server already holds a settings document (the existing one
        wins) and otherwise promotes it to the initial document (spec 7.5).
        """
        with self._lock:
            self._load_unlocked()
            if SETTINGS_DOCUMENT_ID in self._records_by_user_id.get(user_id, {}):
                return False
            metadata: dict[str, object] = {"settings_format": SUPPORTED_SETTINGS_FORMAT, **dict(value_by_key)}
            self._set_record_unlocked(
                user_id, AccountDocumentRecord(doc_id=SETTINGS_DOCUMENT_ID, metadata=metadata, is_dirty=False)
            )
            return True
