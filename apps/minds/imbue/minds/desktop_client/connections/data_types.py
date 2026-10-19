from collections.abc import Mapping
from enum import auto
from typing import Any

from pydantic import ConfigDict
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.connections.primitives import ConnectionBackend
from imbue.minds.desktop_client.connections.primitives import connection_backend_from_name

CONNECTION_STATE_ACTIVE = "active"
CONNECTION_STATE_DELETED = "deleted"


class ConnectionCredentialSource(UpperCaseStrEnum):
    """Where a connection's credentials come from.

    PASTED connections carry their keys in the encrypted payload (plaintext for
    the local profile). LOCAL_PROFILE connections carry no secret at all: the
    block names a login installed on the machine that materializes it (an AWS
    named profile, an Azure CLI login, a Modal profile), so the payload is empty
    and the profile's name travels in the plaintext placement.
    """

    PASTED = auto()
    LOCAL_PROFILE = auto()


@pure
def credential_source_name(source: ConnectionCredentialSource) -> str:
    """The lowercase wire spelling of a credential source (``pasted`` / ``local_profile``)."""
    return source.value.lower()


@pure
def credential_source_from_name(name: str) -> ConnectionCredentialSource | None:
    """The inverse of :func:`credential_source_name`: the typed source, or None for one this app does not know."""
    try:
        return ConnectionCredentialSource(name.upper())
    except ValueError:
        return None


DEFAULT_CREDENTIAL_SOURCE_NAME = credential_source_name(ConnectionCredentialSource.PASTED)


class AwsPlacement(FrozenModel):
    """Placement defaults for an AWS connection."""

    model_config = ConfigDict(extra="ignore")

    default_region: str = Field(description="EC2 region new machines land in")
    default_instance_type: str = Field(description="EC2 instance type new machines use")
    aws_profile: str | None = Field(
        default=None,
        description="The ~/.aws/config profile a local-profile connection authenticates with; None for pasted keys",
    )


class GcpPlacement(FrozenModel):
    """Placement defaults for a GCP connection (GCE is zonal)."""

    model_config = ConfigDict(extra="ignore")

    default_zone: str = Field(description="GCE zone new machines land in")
    default_machine_type: str = Field(description="GCE machine type new machines use")


class AzurePlacement(FrozenModel):
    """Placement defaults for an Azure connection; the resource group is minted once at add time."""

    model_config = ConfigDict(extra="ignore")

    default_region: str = Field(description="Azure region new machines land in")
    default_vm_size: str = Field(description="Azure VM size new machines use")
    resource_group: str = Field(description="The mngr-owned resource group holding every machine of this connection")
    tenant_id: str | None = Field(
        default=None,
        description="The Entra tenant a local-profile connection's az login is used for; None for a pasted principal",
    )
    subscription_id: str | None = Field(
        default=None,
        description="The subscription a local-profile connection's az login is used for; None for a pasted principal",
    )


class ModalPlacement(FrozenModel):
    """Modal has no placement knobs beyond an optional namespace name."""

    model_config = ConfigDict(extra="ignore")

    user_id: str | None = Field(
        default=None,
        description=(
            "The Modal block's user_id (so the environment name) when the connection named one at registration; "
            "None derives it from the connection id. This-device-only connections only."
        ),
    )
    modal_profile: str | None = Field(
        default=None,
        description="The ~/.modal.toml profile a local-profile connection authenticates with; None for a pasted token",
    )


class ConnectionMetadata(FrozenModel):
    """The plaintext half of a connection document (server-visible, never secret)."""

    # Unknown keys are tolerated so a newer client can add metadata fields
    # without this version rejecting the whole document.
    model_config = ConfigDict(extra="ignore")

    connection_format: int = Field(
        default=1,
        description="Semantic format of the metadata; a client below it treats the connection as read-only",
    )
    backend: str = Field(description="The mngr backend name (aws, gcp, azure, modal); unknown values are read-only")
    alias: str = Field(default="", description="Display name chosen by the user; not unique, not an identity")
    identifier: str = Field(default="", description="Masked, display-safe credential hint; never a secret")
    placement: dict[str, Any] = Field(default_factory=dict, description="Backend-specific placement defaults")
    created_on_device_label: str = Field(default="", description="Hostname of the device that added it")
    credential_source: str = Field(
        default=DEFAULT_CREDENTIAL_SOURCE_NAME,
        description=(
            "Where the credentials come from: 'pasted' (keys in the payload) or 'local_profile' (no payload; the "
            "placement names a login installed on each device). An unknown value is read-only, never dropped."
        ),
    )


@pure
def parse_connection_backend(metadata: ConnectionMetadata) -> ConnectionBackend | None:
    """The typed backend of a connection, or None when this app does not know the backend."""
    return connection_backend_from_name(metadata.backend)


@pure
def parse_credential_source(metadata: ConnectionMetadata) -> ConnectionCredentialSource | None:
    """The typed credential source of a connection, or None when a newer app wrote one this app does not know."""
    return credential_source_from_name(metadata.credential_source)


@pure
def credential_source_of_raw_metadata(raw_metadata: Mapping[str, Any]) -> ConnectionCredentialSource | None:
    """The credential source a raw metadata object names (``pasted`` when it names none), or None for an unknown one."""
    raw = raw_metadata.get("credential_source", DEFAULT_CREDENTIAL_SOURCE_NAME)
    return credential_source_from_name(str(raw))


class ConnectionCredentials(FrozenModel):
    """The decrypted credentials payload of a connection; only the owning backend's fields are set."""

    model_config = ConfigDict(extra="ignore")

    payload_format: int = Field(
        default=1,
        description="Semantic format of the payload; a client below it never rewrites or materializes it",
    )
    aws_access_key_id: str | None = Field(default=None, description="AWS access key id")
    aws_secret_access_key: SecretStr | None = Field(default=None, description="AWS secret access key")
    service_account_key_json: SecretStr | None = Field(default=None, description="GCP service-account key JSON")
    subscription_id: str | None = Field(default=None, description="Azure subscription id")
    tenant_id: str | None = Field(default=None, description="Azure tenant id")
    client_id: str | None = Field(default=None, description="Azure service-principal client id")
    client_secret: SecretStr | None = Field(default=None, description="Azure service-principal client secret")
    token_id: str | None = Field(default=None, description="Modal token id")
    token_secret: SecretStr | None = Field(default=None, description="Modal token secret")

    def to_plaintext_dict(self) -> dict[str, object]:
        """The payload as it is encrypted (or stored plaintext for the local profile); unset fields are omitted."""
        plaintext: dict[str, object] = {"payload_format": self.payload_format}
        for name, value in self.model_dump(exclude={"payload_format"}).items():
            if value is None:
                continue
            plaintext[name] = value.get_secret_value() if isinstance(value, SecretStr) else value
        return plaintext


class ConnectionReplicaRecord(FrozenModel):
    """One connection document as held in the local replica (wire fields plus local-only state)."""

    # A row a newer app wrote with an added local-only field must still load:
    # a skipped row is dropped on the next save, and the local profile is never
    # pushed, so nothing would bring it back.
    model_config = ConfigDict(extra="ignore")

    doc_id: str = Field(description="The connection id (the document id within the account)")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Raw metadata object, unknown keys intact")
    encrypted_payload: str | None = Field(default=None, description="Base64 AEAD blob under the account DEK")
    payload: dict[str, Any] | None = Field(
        default=None, description="Plaintext credentials; local profile only, never pushed"
    )
    state: str = Field(default=CONNECTION_STATE_ACTIVE, description="'active' or 'deleted' (tombstone)")
    revision: int = Field(default=0, description="Last server-acknowledged revision (0 = never pushed)")
    document_format: int = Field(default=1, description="Semantic format of the document")
    deleted_at: str | None = Field(default=None, description="Server tombstone stamp")
    is_dirty: bool = Field(default=False, description="Local changes not yet pushed")
    validation_rollback: "ConnectionReplicaRecord | None" = Field(
        default=None,
        description=(
            "Local only, never sent: the row as it was before an in-place credential update whose new "
            "credentials are still being validated; restored if the validation fails or the app dies mid-update"
        ),
    )

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


@pure
def connection_record_from_wire(wire: Mapping[str, object]) -> ConnectionReplicaRecord:
    """Build a clean (non-dirty) replica row from a server wire document."""
    raw_metadata = wire.get("metadata")
    raw_payload = wire.get("encrypted_payload")
    raw_deleted_at = wire.get("deleted_at")
    return ConnectionReplicaRecord(
        doc_id=str(wire.get("doc_id", "")),
        metadata={str(key): value for key, value in raw_metadata.items()} if isinstance(raw_metadata, dict) else {},
        encrypted_payload=str(raw_payload) if raw_payload is not None else None,
        state=str(wire.get("state", CONNECTION_STATE_ACTIVE)),
        revision=int(str(wire.get("revision", 0))),
        document_format=int(str(wire.get("document_format", 1))),
        deleted_at=str(raw_deleted_at) if raw_deleted_at is not None else None,
        is_dirty=False,
    )


class ConnectionAvailability(UpperCaseStrEnum):
    """Whether this device can use a connection's credentials right now."""

    # The credentials are decryptable here (or plaintext in the local profile).
    AVAILABLE = auto()
    # The owning account has no DEK on this device.
    LOCKED = auto()
    # The payload was scrubbed server-side (master password cleared), never synced, or is
    # unreadable here (undecryptable or malformed).
    NO_CREDENTIALS = auto()
    # Written by a newer app version (metadata or payload format above what this app supports).
    READ_ONLY = auto()


class ConnectionView(FrozenModel):
    """What the UI and the create form consume for one connection."""

    connection_id: str = Field(description="The connection id")
    instance_name: str = Field(description="The mngr provider instance name, <backend>_<hex>")
    backend: str = Field(description="The backend name from the metadata (may be unknown to this app)")
    alias: str = Field(description="Display name")
    identifier: str = Field(description="Masked credential hint")
    placement: dict[str, Any] = Field(default_factory=dict, description="Backend-specific placement defaults")
    owner_user_id: str = Field(description="The owning account's user id, or 'local' for the local profile")
    availability: ConnectionAvailability = Field(description="Whether the credentials are usable here")
    credential_source: str = Field(
        default=DEFAULT_CREDENTIAL_SOURCE_NAME,
        description="'pasted' or 'local_profile' (or a name this app does not know)",
    )

    @property
    def region(self) -> str:
        """The connection's pinned placement (region, or GCE zone), empty when the backend has none."""
        return str(self.placement.get("default_region") or self.placement.get("default_zone") or "")


class SignedInAccount(FrozenModel):
    """One signed-in imbue cloud account, as the materializer needs it."""

    user_id: str = Field(description="The full SuperTokens user id")
    email: str = Field(description="The account email (the plugin's session index key)")


class ProviderMaterializationOutcome(UpperCaseStrEnum):
    """What one materializer run over the plugin's session list did."""

    # The provider blocks were re-derived and the settings file changed.
    CHANGED = auto()
    # The provider blocks were re-derived and already matched the file.
    UNCHANGED = auto()
    # The account listing failed, so the settings were left as they were.
    SKIPPED = auto()


class ConnectionPullOutcome(FrozenModel):
    """Result of pulling one account's connection documents."""

    is_reached: bool = Field(description="Whether the connector answered")
    is_changed: bool = Field(description="Whether any replica row changed")
