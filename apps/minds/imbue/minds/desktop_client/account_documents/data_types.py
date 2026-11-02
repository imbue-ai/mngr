from collections.abc import Mapping
from typing import Any
from typing import TypeVar

from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

DOCUMENT_STATE_ACTIVE = "active"
DOCUMENT_STATE_DELETED = "deleted"


class AccountDocumentRecord(FrozenModel):
    """One account document as held in a local replica (wire fields plus local-only sync state).

    The base row every document kind shares; a kind with extra local-only state
    (the connections' plaintext local-profile payload, their mid-update
    rollback) subclasses it with defaulted fields.
    """

    # A row a newer app wrote with an added local-only field must still load:
    # a skipped row is dropped on the next save, and a kind's local-only rows
    # may never be pushed, so nothing would bring it back.
    model_config = ConfigDict(extra="ignore")

    doc_id: str = Field(description="The document id within the account and kind")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Raw metadata object, unknown keys intact")
    encrypted_payload: str | None = Field(default=None, description="Base64 AEAD blob under the account DEK")
    state: str = Field(default=DOCUMENT_STATE_ACTIVE, description="'active' or 'deleted' (tombstone)")
    revision: int = Field(default=0, description="Last server-acknowledged revision (0 = never pushed)")
    document_format: int = Field(default=1, description="Semantic format of the document")
    deleted_at: str | None = Field(default=None, description="Server tombstone stamp")
    is_dirty: bool = Field(default=False, description="Local changes not yet pushed")

    def to_wire(self, kind: str, push_revision: int, is_payload_included: bool) -> dict[str, object]:
        """Render the document for a CLI push; the payload key is omitted (server keeps its blob) unless included."""
        wire: dict[str, object] = {
            "kind": kind,
            "doc_id": self.doc_id,
            "metadata": dict(self.metadata),
            "state": self.state,
            "revision": push_revision,
            "document_format": self.document_format,
        }
        if is_payload_included:
            wire["encrypted_payload"] = self.encrypted_payload
        return wire


RecordT = TypeVar("RecordT", bound=AccountDocumentRecord)


@pure
def document_record_from_wire(record_type: type[RecordT], wire: Mapping[str, object]) -> RecordT:
    """Build a clean (non-dirty) replica row from a server wire document; kind-specific fields keep their defaults."""
    raw_metadata = wire.get("metadata")
    raw_payload = wire.get("encrypted_payload")
    raw_deleted_at = wire.get("deleted_at")
    return record_type(
        doc_id=str(wire.get("doc_id", "")),
        metadata={str(key): value for key, value in raw_metadata.items()} if isinstance(raw_metadata, dict) else {},
        encrypted_payload=str(raw_payload) if raw_payload is not None else None,
        state=str(wire.get("state", DOCUMENT_STATE_ACTIVE)),
        revision=int(str(wire.get("revision", 0))),
        document_format=int(str(wire.get("document_format", 1))),
        deleted_at=str(raw_deleted_at) if raw_deleted_at is not None else None,
        is_dirty=False,
    )


class DocumentPullOutcome(FrozenModel):
    """Result of pulling one account's documents of one kind."""

    is_reached: bool = Field(description="Whether the connector answered")
    is_changed: bool = Field(description="Whether any replica row changed")
