import base64
import json
from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import AbstractContextManager
from contextlib import contextmanager
from typing import Any
from typing import Final
from uuid import NAMESPACE_URL
from uuid import uuid5

from azure.core.exceptions import AzureError
from azure.core.exceptions import ResourceExistsError
from azure.core.exceptions import ResourceNotFoundError
from azure.mgmt.authorization import AuthorizationManagementClient
from azure.mgmt.authorization.v2022_04_01.models import RoleAssignmentCreateParameters
from azure.mgmt.storage import StorageManagementClient
from azure.mgmt.storage.models import Sku
from azure.mgmt.storage.models import StorageAccountCreateParameters
from azure.mgmt.storage.models import StorageAccountPropertiesCreateParameters
from azure.storage.blob import BlobServiceClient
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.volume import Volume
from imbue.mngr.utils.polling import poll_until
from imbue.mngr_vps import state_keys
from imbue.mngr_vps.state_bucket_base import BaseObjectStoreVolume
from imbue.mngr_vps.state_bucket_base import BaseStateBucket
from imbue.mngr_vps.state_bucket_base import ObjectStoreEntry

# The Azure analog of an S3 bucket is a Blob *container* inside a *storage
# account*. The container name is fixed (container names allow hyphens, 3-63
# lowercase); the storage-account name is derived per scope (3-24 chars,
# lowercase alphanumeric only) -- see ``AzureProviderConfig`` for derivation.
DEFAULT_STATE_CONTAINER_NAME: Final[str] = "mngr-state"

# Storage account SKU + kind for the state account: locally-redundant standard
# storage with a general-purpose-v2 account (the modern default), private.
_STORAGE_ACCOUNT_SKU: Final[str] = "Standard_LRS"
_STORAGE_ACCOUNT_KIND: Final[str] = "StorageV2"

# Built-in Azure role that grants data-plane read/write on Blob storage. Scoped
# (by ``ensure_operator_blob_access``) to JUST the state storage account, so the
# operator can read/write mngr's state blobs but cannot touch any other storage.
# The id is the well-known, stable role-definition GUID for ``Storage Blob Data
# Contributor`` (least privilege: data-plane only, no account management).
STORAGE_BLOB_DATA_CONTRIBUTOR_ROLE_ID: Final[str] = "ba92f5b4-2d11-453d-a403-e96b0029c9fe"

# Azure role assignments are eventually consistent: a freshly-created principal
# may not yet be resolvable as a role-assignment principal. Bound how long
# ``ensure_blob_data_contributor`` waits for the assignment to stick before
# proceeding (mirrors the AWS IAM consistency poll).
_AZURE_CONSISTENCY_TIMEOUT_SECONDS: Final[float] = 30.0
_AZURE_CONSISTENCY_POLL_SECONDS: Final[float] = 2.0


class BlobStateBucketError(MngrError):
    """An Azure Blob state-bucket operation failed."""


def _decode_jwt_claims(token: str) -> dict[str, Any]:
    """Decode (without verifying) the claims payload of a JWT access token.

    We trust our own management token, so no signature check is needed -- this
    just base64url-decodes the middle segment and parses the JSON claims.
    """
    parts = token.split(".")
    if len(parts) < 2:
        raise BlobStateBucketError("The Azure access token is not a JWT; cannot determine the operator principal.")
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload + padding))
    except (ValueError, json.JSONDecodeError) as e:
        raise BlobStateBucketError(f"Could not decode the Azure access-token claims: {e}") from e


def _principal_type_from_claims(claims: Mapping[str, Any]) -> str:
    """Classify the token's principal as ``"User"`` or ``"ServicePrincipal"`` from its claims.

    Prefers the ``idtyp`` claim (``"user"`` / ``"app"``) when present. ARM v1.0
    tokens often omit ``idtyp``, so fall back to the presence of an app-identity
    claim (``appid`` / ``azp``), which a service-principal token carries and a
    user token does not; default to ``"User"`` otherwise. The value is only a hint
    to the role-assignment API (it skips a directory lookup), and the operator is
    always an already-existing principal, so a wrong guess is low-risk.
    """
    idtyp = str(claims.get("idtyp", "")).lower()
    if idtyp == "user":
        return "User"
    if idtyp == "app":
        return "ServicePrincipal"
    if claims.get("appid") or claims.get("azp"):
        return "ServicePrincipal"
    return "User"


def resolve_operator_principal(credential: Any) -> tuple[str, str]:
    """Resolve the ``(object_id, principal_type)`` of the principal behind ``credential``.

    Reads the ``oid`` claim from a management-scope access token, so it works for
    both a signed-in user and a service principal without needing Microsoft Graph
    permissions. ``principal_type`` is classified from the token claims (see
    :func:`_principal_type_from_claims`).
    """
    token = credential.get_token("https://management.azure.com/.default").token
    claims = _decode_jwt_claims(token)
    object_id = claims.get("oid")
    if not object_id:
        raise BlobStateBucketError(
            "Could not determine the operator principal (the Azure token has no 'oid' claim); cannot grant it "
            "Storage Blob Data Contributor on the state account."
        )
    return object_id, _principal_type_from_claims(claims)


def _blob_data_role_assignment_name(principal_id: str, scope: str) -> str:
    """Deterministic role-assignment GUID for a principal on a scope (so ensure/delete match across runs)."""
    return str(uuid5(NAMESPACE_URL, f"{principal_id}/{scope}/blob-data-contributor"))


def _blob_data_contributor_role_definition_id(subscription_id: str) -> str:
    """The Storage Blob Data Contributor role-definition ARM id in the given subscription."""
    return (
        f"/subscriptions/{subscription_id}/providers/Microsoft.Authorization/"
        f"roleDefinitions/{STORAGE_BLOB_DATA_CONTRIBUTOR_ROLE_ID}"
    )


def _try_create_blob_role_assignment(
    authorization: Any,
    scope: str,
    assignment_name: str,
    parameters: Any,
    *,
    subject_label: str,
    account_name: str,
    error_cls: type[MngrError],
) -> bool:
    """Create the role assignment once. True on success/already-exists; False on a transient PrincipalNotFound.

    A 409 (assignment already exists) is success. A ``PrincipalNotFound`` is the
    eventual-consistency case (a just-created principal) -- return False so the
    caller retries. Any other Azure error is a real failure and is raised as
    ``error_cls``.
    """
    try:
        authorization.role_assignments.create(scope=scope, role_assignment_name=assignment_name, parameters=parameters)
    except ResourceExistsError:
        return True
    except AzureError as e:
        message = str(e).lower().replace(" ", "")
        if "roleassignmentexists" in message:
            return True
        if "principalnotfound" in message:
            return False
        raise error_cls(
            f"Failed to assign Storage Blob Data Contributor to {subject_label} on account {account_name!r}: {e}"
        ) from e
    return True


def ensure_blob_data_contributor(
    authorization: Any,
    subscription_id: str,
    scope: str,
    principal_id: str,
    principal_type: str,
    *,
    subject_label: str,
    account_name: str,
    error_cls: type[MngrError],
) -> None:
    """Assign Storage Blob Data Contributor to ``principal_id``, scoped to ``scope`` (idempotent).

    Shared by the VM managed-identity grant and the operator grant. The assignment
    name is a deterministic GUID (principal + scope), so a re-run targets the same
    assignment and an already-existing one is success. A freshly-created principal
    is eventually consistent, so a transient ``PrincipalNotFound`` is retried within
    the consistency window.
    """
    parameters = RoleAssignmentCreateParameters(
        role_definition_id=_blob_data_contributor_role_definition_id(subscription_id),
        principal_id=principal_id,
        principal_type=principal_type,
    )
    assignment_name = _blob_data_role_assignment_name(principal_id, scope)
    if not poll_until(
        lambda: _try_create_blob_role_assignment(
            authorization,
            scope,
            assignment_name,
            parameters,
            subject_label=subject_label,
            account_name=account_name,
            error_cls=error_cls,
        ),
        timeout=_AZURE_CONSISTENCY_TIMEOUT_SECONDS,
        poll_interval=_AZURE_CONSISTENCY_POLL_SECONDS,
    ):
        raise error_cls(
            f"Role assignment for {subject_label} on account {account_name!r} did not succeed within "
            f"{_AZURE_CONSISTENCY_TIMEOUT_SECONDS:.0f}s (the principal may not have propagated yet)."
        )


class BlobStateBucket(BaseStateBucket):
    """Reads/writes mngr control-plane state in an Azure Blob container, readable while offline.

    The Azure analog of ``S3StateBucket``: a Blob container inside a storage
    account holds the full host record and per-agent records keyed by host id,
    written by the mngr host machine with the operator's credentials, so a
    deallocated VM's state is readable without SSH and without the 256-char Azure
    tag limit. Data-plane access uses AAD (the same ``DefaultAzureCredential`` the
    provider uses) against ``https://<account>.blob.core.windows.net``; the
    storage account itself is created/deleted via the storage management plane.

    The cloud-agnostic record marshalling + key layout live on ``BaseStateBucket``;
    this class supplies the Blob/storage clients, the raw object primitives, error
    translation, and the storage-account + container lifecycle.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Typed ``Any`` because azure.core.credentials.TokenCredential is a Protocol,
    # which pydantic's arbitrary_types_allowed validation cannot accept as a field.
    credential: Any = Field(frozen=True, description="DefaultAzureCredential (or compatible) for blob + mgmt access")
    subscription_id: str = Field(frozen=True, description="Azure subscription the storage account lives in")
    resource_group: str = Field(frozen=True, description="Resource group holding the storage account")
    region: str = Field(frozen=True, description="Azure region the storage account lives in")
    account_name: str = Field(frozen=True, description="Globally-unique storage-account name (3-24 lowercase alnum)")
    container_name: str = Field(
        default=DEFAULT_STATE_CONTAINER_NAME, frozen=True, description="Blob container name holding mngr state"
    )
    _cached_blob_service_client: Any = PrivateAttr(default=None)
    _cached_storage_mgmt_client: Any = PrivateAttr(default=None)
    _cached_authorization_client: Any = PrivateAttr(default=None)

    def _account_url(self) -> str:
        return f"https://{self.account_name}.blob.core.windows.net"

    def _blob_service(self) -> Any:
        """Return the data-plane ``BlobServiceClient``, building and caching it on first use."""
        if self._cached_blob_service_client is None:
            self._cached_blob_service_client = BlobServiceClient(self._account_url(), credential=self.credential)
        return self._cached_blob_service_client

    def _storage_mgmt(self) -> Any:
        """Return the management-plane ``StorageManagementClient`` (account create/delete), cached."""
        if self._cached_storage_mgmt_client is None:
            self._cached_storage_mgmt_client = StorageManagementClient(self.credential, self.subscription_id)
        return self._cached_storage_mgmt_client

    def _authorization(self) -> Any:
        """Return the ``AuthorizationManagementClient`` (role assignments), cached.

        Used to grant the operator's own principal data-plane blob access on this
        account (see ``ensure_operator_blob_access``).
        """
        if self._cached_authorization_client is None:
            self._cached_authorization_client = AuthorizationManagementClient(self.credential, self.subscription_id)
        return self._cached_authorization_client

    def _account_scope(self) -> str:
        """ARM resource id of the storage account -- the role-assignment scope (least privilege)."""
        return (
            f"/subscriptions/{self.subscription_id}/resourceGroups/{self.resource_group}"
            f"/providers/Microsoft.Storage/storageAccounts/{self.account_name}"
        )

    def _container(self) -> Any:
        return self._blob_service().get_container_client(self.container_name)

    @property
    def _store_label(self) -> str:
        return f"Azure container {self.container_name}"

    def _translate_errors(self) -> AbstractContextManager[None]:
        return _translate_blob_errors(self.account_name)

    def _is_not_found(self, error: MngrError) -> bool:
        return isinstance(error.__cause__, ResourceNotFoundError)

    @property
    def _bucket_error_type(self) -> type[MngrError]:
        return BlobStateBucketError

    def _make_host_dir_volume(self) -> Volume:
        return BlobVolume(
            credential=self.credential,
            account_name=self.account_name,
            container_name=self.container_name,
        )

    def _prefix_has_any_object(self, prefix: str) -> bool:
        for _ in self._container().list_blobs(name_starts_with=prefix):
            return True
        return False

    def account_exists(self) -> bool:
        """Return whether the storage account already exists (read-only management GET)."""
        try:
            self._storage_mgmt().storage_accounts.get_properties(self.resource_group, self.account_name)
        except ResourceNotFoundError:
            return False
        except AzureError as e:
            raise BlobStateBucketError(
                f"Failed to check existence of storage account {self.account_name!r}: {e}"
            ) from e
        return True

    def container_exists(self) -> bool:
        """Return whether the state container exists (read-only data-plane check)."""
        with _translate_blob_errors(self.account_name):
            return bool(self._container().exists())

    def ensure_bucket(self) -> bool:
        """Idempotently create the storage account + state container; return True iff the account was created.

        Read-only-first: an account-existence GET precedes any create, so a re-run
        on an already-prepared account issues no account write. The created account
        is private (no public blob access), encrypted at rest by default (Azure
        Storage service-side encryption is always on), and tagged ``managed-by=mngr``.
        The container is then created if absent (private access). Returns whether
        the *account* (the "bucket") was created; the container create is folded in
        (a re-run that adds only the container still returns False).
        """
        was_account_created = self._ensure_account()
        self._ensure_container()
        return was_account_created

    def ensure_operator_blob_access(self) -> None:
        """Grant the operator's own principal Storage Blob Data Contributor on this account (idempotent).

        Azure splits control-plane and data-plane: creating the storage account
        (or holding Owner/Contributor on it) does NOT grant data-plane blob
        read/write. mngr reads/writes the state blobs from the operator's machine
        via ``DefaultAzureCredential`` during offline discovery (``mngr list`` /
        ``start`` on a deallocated host), so the account creator must be granted the
        data role explicitly -- scoped to JUST this account -- mirroring the VM
        managed-identity grant. Without this, every operator offline read/write
        fails with ``AuthorizationPermissionMismatch``.

        Grants the principal behind this bucket's credential (the ``mngr azure
        prepare`` runner). In a multi-operator setup, each operator that runs
        against the bucket needs this grant; re-running ``prepare`` as that operator
        (or granting their principal out of band) is the way to add them.
        """
        principal_id, principal_type = resolve_operator_principal(self.credential)
        ensure_blob_data_contributor(
            self._authorization(),
            self.subscription_id,
            self._account_scope(),
            principal_id,
            principal_type,
            subject_label="the operator principal",
            account_name=self.account_name,
            error_cls=BlobStateBucketError,
        )
        logger.info("Granted the operator principal Storage Blob Data Contributor on account {}", self.account_name)

    def _ensure_account(self) -> bool:
        """Create the storage account if absent. Returns True iff it was created."""
        if self.account_exists():
            logger.debug("Azure storage account {} already exists; skipping create", self.account_name)
            return False
        parameters = StorageAccountCreateParameters(
            sku=Sku(name=_STORAGE_ACCOUNT_SKU),
            kind=_STORAGE_ACCOUNT_KIND,
            location=self.region,
            tags={state_keys.MANAGED_BY_TAG_KEY: state_keys.MANAGED_BY_TAG_VALUE},
            properties=StorageAccountPropertiesCreateParameters(
                allow_blob_public_access=False,
                minimum_tls_version="TLS1_2",
            ),
        )
        with _translate_blob_errors(self.account_name):
            self._storage_mgmt().storage_accounts.begin_create(
                self.resource_group, self.account_name, parameters
            ).result()
        logger.info(
            "Created Azure storage account {} in resource group {} (region {})",
            self.account_name,
            self.resource_group,
            self.region,
        )
        return True

    def _ensure_container(self) -> None:
        """Create the private state container if absent. Idempotent."""
        try:
            with _translate_blob_errors(self.account_name):
                self._blob_service().create_container(self.container_name)
        except BlobStateBucketError as e:
            # ``ResourceExistsError`` (the container already exists) is the expected
            # idempotent re-run; only that is swallowed. Anything else propagates.
            if isinstance(e.__cause__, ResourceExistsError):
                logger.debug("Azure state container {} already exists; skipping create", self.container_name)
                return
            raise
        logger.info("Created Azure state container {} in account {}", self.container_name, self.account_name)

    def delete_bucket(self) -> None:
        """Delete the storage account (cascading the container + blobs). Idempotent."""
        if not self.account_exists():
            return
        # Storage-account delete is synchronous in the SDK (no LRO poller), so the
        # call returns once the account is gone; a failure surfaces here directly.
        with _translate_blob_errors(self.account_name):
            self._storage_mgmt().storage_accounts.delete(self.resource_group, self.account_name)
        logger.info("Deleted Azure storage account {} in resource group {}", self.account_name, self.resource_group)

    def _put_object(self, key: str, body: str) -> None:
        with _translate_blob_errors(self.account_name):
            self._container().upload_blob(name=key, data=body.encode("utf-8"), overwrite=True)

    def _read_object_bytes(self, key: str) -> bytes:
        return self._container().download_blob(key).readall()

    def _delete_single_object(self, key: str) -> None:
        self._container().delete_blob(key)

    def _delete_keys(self, keys: list[str]) -> None:
        # Blob storage has no batch delete, so remove one at a time (each idempotent).
        for key in keys:
            self._delete_object(key)

    def _list_keys(self, prefix: str) -> list[str]:
        with _translate_blob_errors(self.account_name):
            return [blob.name for blob in self._container().list_blobs(name_starts_with=prefix)]


class BlobVolume(BaseObjectStoreVolume):
    """A ``Volume`` backed by an Azure Blob container, for reading a host's offline host_dir.

    Maps volume-relative paths to blob names under whatever prefix it is
    ``scoped()`` to. Reads use the operator's credentials (the same data-plane
    auth as ``BlobStateBucket``). The shared object-store logic (listing /
    existence / read / write / delete) lives on ``BaseObjectStoreVolume``; this
    class supplies only the Blob client + SDK primitives + error seam. Mirrors
    ``S3Volume``.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    credential: Any = Field(frozen=True, description="DefaultAzureCredential (or compatible) for blob access")
    account_name: str = Field(frozen=True, description="Storage account holding the container")
    container_name: str = Field(frozen=True, description="Blob container name")
    _cached_blob_service_client: Any = PrivateAttr(default=None)

    def _account_url(self) -> str:
        return f"https://{self.account_name}.blob.core.windows.net"

    def _blob_service(self) -> Any:
        if self._cached_blob_service_client is None:
            self._cached_blob_service_client = BlobServiceClient(self._account_url(), credential=self.credential)
        return self._cached_blob_service_client

    def _container(self) -> Any:
        return self._blob_service().get_container_client(self.container_name)

    def _translate_errors(self) -> AbstractContextManager[None]:
        return _translate_blob_errors(self.account_name)

    def _is_not_found(self, error: MngrError) -> bool:
        return isinstance(error.__cause__, ResourceNotFoundError)

    @property
    def _bucket_error_type(self) -> type[MngrError]:
        return BlobStateBucketError

    def _make_missing_file_error(self, path: str) -> MngrError:
        return BlobStateBucketError(f"File {path!r} does not exist in container {self.container_name!r}")

    def _iter_delimited_entries(self, prefix: str) -> Iterator[ObjectStoreEntry]:
        for item in self._container().walk_blobs(name_starts_with=prefix, delimiter="/"):
            name = item.name
            # A delimited walk yields BlobPrefix entries (name ends with "/") for
            # immediate sub-"directories" and BlobProperties for files directly
            # under the prefix.
            if name.endswith("/"):
                yield ObjectStoreEntry(name=name.rstrip("/"), is_directory=True, mtime=0, size=0)
                continue
            last_modified = item.last_modified
            yield ObjectStoreEntry(
                name=name,
                is_directory=False,
                mtime=int(last_modified.timestamp()) if last_modified is not None else 0,
                size=item.size or 0,
            )

    def _prefix_has_any_object(self, prefix: str) -> bool:
        for _ in self._container().list_blobs(name_starts_with=prefix):
            return True
        return False

    def _has_object_at_key(self, key: str) -> bool:
        return any(blob.name == key for blob in self._container().list_blobs(name_starts_with=key))

    def _read_object_bytes(self, key: str) -> bytes:
        return self._container().download_blob(key).readall()

    def _delete_single_object(self, key: str) -> None:
        self._container().delete_blob(key)

    def _delete_prefix(self, prefix: str) -> None:
        container = self._container()
        for blob in list(container.list_blobs(name_starts_with=prefix)):
            container.delete_blob(blob.name)

    def _write_object(self, key: str, content: bytes) -> None:
        self._container().upload_blob(name=key, data=content, overwrite=True)


@contextmanager
def _translate_blob_errors(account_name: str) -> Iterator[None]:
    """Translate ``azure.core.exceptions.AzureError`` into ``BlobStateBucketError`` within the block.

    ``ResourceNotFoundError`` / ``ResourceExistsError`` subclass ``AzureError``,
    so callers that need to special-case them inspect ``__cause__`` on the raised
    ``BlobStateBucketError`` (mirrors how the S3 variant inspects the error code).
    """
    try:
        yield
    except AzureError as e:
        raise BlobStateBucketError(f"Azure Blob operation on storage account {account_name!r} failed: {e}") from e
