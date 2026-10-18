import re
from enum import auto
from typing import Final

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.ids import RandomId
from imbue.imbue_common.pure import pure

# The account-document kind a cloud connection is stored under.
CONNECTION_DOCUMENT_KIND: Final[str] = "connection"

# The pseudo-account owning unsynced connections: same document schema, stored
# plaintext under the minds data dir, never pushed.
LOCAL_PROFILE_USER_ID: Final[str] = "local"

# The newest connection metadata semantics this app understands. A document
# whose ``connection_format`` is above this is shown but never materialized or
# rewritten here.
SUPPORTED_CONNECTION_FORMAT: Final[int] = 1

# The newest credentials payload semantics this app understands; the same
# read-only rule, applied to the encrypted half.
SUPPORTED_CREDENTIALS_PAYLOAD_FORMAT: Final[int] = 1

# The longest Modal sandbox lifetime a connection block's default or a create's
# own override may name: Modal's hard cap is 86400 s and the mngr_modal provider
# adds its 90 s shutdown buffer on top of the timeout it is given.
MODAL_SANDBOX_TIMEOUT_SECONDS_MAX: Final[int] = 86310

IMBUE_CLOUD_PROVIDER_NAME_PREFIX: Final[str] = "imbue_cloud_"

# What a this-device-only Modal connection may name its Modal namespace with
# (the block's ``user_id``, hence the environment name ``<prefix><user id>``):
# 48 characters is the evals harness's own user-id limit, and under the staging
# prefix the environment name then stays inside Modal's 64-character limit.
MODAL_USER_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")


class ConnectionId(RandomId):
    """Identity of one cloud connection; minted once when it is added and never changed."""

    PREFIX = "conn"


class ConnectionBackend(UpperCaseStrEnum):
    """The mngr provider backends a connection can target."""

    AWS = auto()
    GCP = auto()
    AZURE = auto()
    MODAL = auto()


# The backends whose region is pinned when the connection is added: their
# discovery clients are region-bound (aws, gcp) or their scaffolding is
# region-locked (azure's resource group). Modal picks the region per create.
REGION_PINNED_BACKENDS: Final[frozenset[ConnectionBackend]] = frozenset(
    {ConnectionBackend.AWS, ConnectionBackend.GCP, ConnectionBackend.AZURE}
)


@pure
def is_region_pinned_for_backend(backend: ConnectionBackend) -> bool:
    """Whether a connection of this backend pins its region at add time (else the create form picks one)."""
    return backend in REGION_PINNED_BACKENDS


@pure
def mngr_backend_name(backend: ConnectionBackend) -> str:
    """The lowercase backend name mngr registers the provider under."""
    return backend.value.lower()


@pure
def connection_backend_from_name(backend_name: str) -> ConnectionBackend | None:
    """The inverse of :func:`mngr_backend_name`: the typed backend, or None for a name this app does not know."""
    try:
        return ConnectionBackend(backend_name.upper())
    except ValueError:
        return None


_CONNECTION_INSTANCE_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<backend>"
    + "|".join(mngr_backend_name(backend) for backend in ConnectionBackend)
    + r")_(?P<hex>[0-9a-f]{32})$"
)


@pure
def imbue_cloud_provider_name_for_user(user_id: str) -> str:
    """The account instance name: ``imbue_cloud_<full SuperTokens user id>``."""
    return f"{IMBUE_CLOUD_PROVIDER_NAME_PREFIX}{user_id}"


@pure
def provider_instance_name_for_synced_row(user_id: str, provider_kind: str) -> str:
    """The instance an account-synced row's host lives under on this device.

    Any ``imbue_cloud_`` row maps to the account instance named from the
    account's user id (never from the record's ``provider_kind``, which may
    carry the legacy email-slug spelling); a connection row's instance is its
    ``provider_kind``.
    """
    if provider_kind.startswith(IMBUE_CLOUD_PROVIDER_NAME_PREFIX):
        return imbue_cloud_provider_name_for_user(user_id)
    return provider_kind


@pure
def connection_id_hex(connection_id: ConnectionId) -> str:
    """The 32 hex characters of the connection id, the part that names its instance."""
    return connection_id.get_uuid().hex


@pure
def connection_instance_name(backend_name: str, connection_id: ConnectionId) -> str:
    """``<backend>_<connection id hex>`` for any backend name, including one this app does not know."""
    return f"{backend_name}_{connection_id_hex(connection_id)}"


@pure
def provider_instance_name_for_connection(backend: ConnectionBackend, connection_id: ConnectionId) -> str:
    """The connection instance name: ``<backend>_<connection id hex>``."""
    return connection_instance_name(mngr_backend_name(backend), connection_id)


@pure
def is_connection_instance_name(provider_name: str) -> bool:
    return _CONNECTION_INSTANCE_NAME_RE.match(provider_name) is not None


_ACCOUNT_INSTANCE_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"^"
    + re.escape(IMBUE_CLOUD_PROVIDER_NAME_PREFIX)
    + r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


@pure
def is_derived_provider_instance_name(provider_name: str) -> bool:
    """Whether the name is one the materializer derives from an account or a connection.

    An account instance (``imbue_cloud_<SuperTokens user id>``) or a connection
    instance (``<backend>_<32 hex>``); never a backend's default instance such as
    ``imbue_cloud`` or ``imbue_cloud_slice``, which mngr itself configures.
    """
    return _ACCOUNT_INSTANCE_NAME_RE.match(provider_name) is not None or is_connection_instance_name(provider_name)


@pure
def backend_for_connection_instance_name(provider_name: str) -> ConnectionBackend | None:
    """The backend a connection instance name encodes, or None for any other name."""
    match = _CONNECTION_INSTANCE_NAME_RE.match(provider_name)
    if match is None:
        return None
    return connection_backend_from_name(match.group("backend"))


@pure
def azure_resource_group_for_connection(connection_id: ConnectionId, region: str) -> str:
    """The resource group minted once at add time: ``mngr-<connection id hex>-<region>``."""
    return f"mngr-{connection_id_hex(connection_id)}-{region}"
