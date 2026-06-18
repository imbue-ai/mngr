import base64
import json
from collections.abc import Iterator
from collections.abc import Mapping
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
from azure.mgmt.msi import ManagedServiceIdentityClient
from azure.mgmt.msi.models import Identity
from azure.mgmt.storage import StorageManagementClient
from azure.mgmt.storage.models import Sku
from azure.mgmt.storage.models import StorageAccountCreateParameters
from azure.mgmt.storage.models import StorageAccountPropertiesCreateParameters
from azure.storage.blob import BlobServiceClient
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr.interfaces.data_types import VolumeFile
from imbue.mngr.interfaces.volume import BaseVolume
from imbue.mngr.interfaces.volume import Volume
from imbue.mngr.primitives import HostId
from imbue.mngr.utils.polling import poll_until
from imbue.mngr_vps_docker import state_keys
from imbue.mngr_vps_docker.state_bucket_base import BaseStateBucket

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
# (in ``BlobStateHostIdentity``) to JUST the state storage account, so the VM's
# managed identity can push host_dir but cannot touch any other storage. The id
# is the well-known, stable role-definition GUID for ``Storage Blob Data
# Contributor`` (least privilege: data-plane only, no account management).
STORAGE_BLOB_DATA_CONTRIBUTOR_ROLE_ID: Final[str] = "ba92f5b4-2d11-453d-a403-e96b0029c9fe"

# Azure role assignments are eventually consistent: a freshly-created
# user-assigned identity may not yet be resolvable as a role-assignment
# principal. Bound how long ``ensure_host_identity`` waits for the assignment to
# stick before proceeding (mirrors the AWS IAM consistency poll).
_AZURE_CONSISTENCY_TIMEOUT_SECONDS: Final[float] = 30.0
_AZURE_CONSISTENCY_POLL_SECONDS: Final[float] = 2.0


def host_identity_name_for_account(account_name: str) -> str:
    """Return the deterministic user-assigned managed-identity name for a state account.

    The storage-account name already encodes the subscription + resource-group
    scope (or an operator override), so deriving the identity name from it gives
    one stable identity per ``prepare`` scope. Managed-identity resource names
    allow ``[\\w-]`` (3-128 chars); ``mngrid-<account>`` stays well within that.
    """
    return f"mngrid-{account_name}"


def host_dir_sync_target_for(account_name: str, container_name: str, host_id: HostId) -> str:
    """Return the ``https://<account>.blob.core.windows.net/<container>/hosts/<id>/host_dir`` sync target.

    The full blob URL the on-box ``azcopy sync`` pushes to. Mirrors the AWS
    ``host_dir_sync_target_for`` (which returns the ``s3://`` URI).
    """
    prefix = state_keys.host_dir_prefix(host_id).rstrip("/")
    return f"https://{account_name}.blob.core.windows.net/{container_name}/{prefix}"


class BlobStateBucketError(MngrError):
    """An Azure Blob state-bucket operation failed."""


class BlobStateHostIdentityError(MngrError):
    """A managed-identity / role-assignment host-identity operation failed."""


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

    def _make_host_dir_volume(self) -> Volume:
        return BlobVolume(
            credential=self.credential,
            account_name=self.account_name,
            container_name=self.container_name,
        )

    def _prefix_has_objects(self, prefix: str) -> bool:
        with _translate_blob_errors(self.account_name):
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

    def _get_object(self, key: str) -> str | None:
        try:
            with _translate_blob_errors(self.account_name):
                downloader = self._container().download_blob(key)
                return downloader.readall().decode("utf-8")
        except BlobStateBucketError as e:
            if isinstance(e.__cause__, ResourceNotFoundError):
                return None
            raise

    def _delete_object(self, key: str) -> None:
        try:
            with _translate_blob_errors(self.account_name):
                self._container().delete_blob(key)
        except BlobStateBucketError as e:
            # A missing blob is a no-op (idempotent delete); anything else propagates.
            if isinstance(e.__cause__, ResourceNotFoundError):
                return
            raise

    def _delete_keys(self, keys: list[str]) -> None:
        # Blob storage has no batch delete, so remove one at a time (each idempotent).
        for key in keys:
            self._delete_object(key)

    def _list_keys(self, prefix: str) -> list[str]:
        with _translate_blob_errors(self.account_name):
            return [blob.name for blob in self._container().list_blobs(name_starts_with=prefix)]


class BlobStateHostIdentity(MutableModel):
    """Manages the user-assigned managed identity + role assignment that lets a VM push host_dir.

    The Azure analog of ``S3StateHostIdentity``: a user-assigned managed identity
    (in the mngr resource group) plus a ``Storage Blob Data Contributor`` role
    assignment SCOPED TO JUST the state storage account -- least privilege, so the
    VM's identity can write Blob data on this one account and nothing else.
    Provisioned by ``mngr azure prepare`` and attached at VM create so the on-box
    sync daemon can write via IMDS/MSI. Reads never need this identity -- they use
    the operator's credentials.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Typed ``Any`` because azure.core.credentials.TokenCredential is a Protocol,
    # which pydantic's arbitrary_types_allowed validation cannot accept as a field.
    credential: Any = Field(frozen=True, description="DefaultAzureCredential (or compatible) for the mgmt clients")
    subscription_id: str = Field(frozen=True, description="Azure subscription the identity + account live in")
    resource_group: str = Field(frozen=True, description="Resource group holding the identity + storage account")
    region: str = Field(frozen=True, description="Azure region the identity is created in")
    account_name: str = Field(frozen=True, description="State storage account the role assignment is scoped to")
    _cached_msi_client: Any = PrivateAttr(default=None)
    _cached_authorization_client: Any = PrivateAttr(default=None)

    def _msi(self) -> Any:
        """Return the ``ManagedServiceIdentityClient`` (identity create/get/delete), cached."""
        if self._cached_msi_client is None:
            self._cached_msi_client = ManagedServiceIdentityClient(self.credential, self.subscription_id)
        return self._cached_msi_client

    def _authorization(self) -> Any:
        """Return the ``AuthorizationManagementClient`` (role assignments), cached."""
        if self._cached_authorization_client is None:
            self._cached_authorization_client = AuthorizationManagementClient(self.credential, self.subscription_id)
        return self._cached_authorization_client

    @property
    def identity_name(self) -> str:
        """The deterministic user-assigned managed-identity name for this state account."""
        return host_identity_name_for_account(self.account_name)

    def resource_id(self) -> str:
        """ARM resource id of the user-assigned managed identity (for VM-create attachment)."""
        return (
            f"/subscriptions/{self.subscription_id}/resourceGroups/{self.resource_group}"
            f"/providers/Microsoft.ManagedIdentity/userAssignedIdentities/{self.identity_name}"
        )

    def _account_scope(self) -> str:
        """ARM resource id of the state storage account -- the role-assignment scope (least privilege)."""
        return (
            f"/subscriptions/{self.subscription_id}/resourceGroups/{self.resource_group}"
            f"/providers/Microsoft.Storage/storageAccounts/{self.account_name}"
        )

    def _get_identity(self) -> Any | None:
        """Return the user-assigned identity resource (read-only GET), or None if absent."""
        try:
            return self._msi().user_assigned_identities.get(self.resource_group, self.identity_name)
        except ResourceNotFoundError:
            return None
        except AzureError as e:
            raise BlobStateHostIdentityError(
                f"Failed to check user-assigned managed identity {self.identity_name!r}: {e}"
            ) from e

    def host_identity_exists(self) -> bool:
        """Return whether the user-assigned managed identity already exists (read-only GET)."""
        return self._get_identity() is not None

    def get_host_identity_client_id(self) -> str | None:
        """Return the managed identity's client id (for the on-box azcopy MSI login), or None if absent.

        The on-box sync daemon authenticates azcopy as a *user-assigned* identity,
        which requires that identity's client id (a VM may carry several). Read-only.
        """
        identity = self._get_identity()
        if identity is None:
            return None
        return identity.client_id

    def ensure_host_identity(self) -> str:
        """Idempotently create the managed identity + scoped role assignment; return the identity resource id.

        Read-only-first: a GET precedes the (idempotent) identity create. Then the
        ``Storage Blob Data Contributor`` role is assigned to the identity's
        principal, scoped to JUST the state storage account. Both the identity
        create_or_update and the role-assignment create are idempotent. Azure is
        eventually consistent about a fresh identity's principal, so the role
        assignment is retried briefly. Mirrors ``S3StateHostIdentity.ensure_host_identity``.
        """
        with _translate_identity_errors(self.identity_name):
            identity = self._msi().user_assigned_identities.create_or_update(
                self.resource_group,
                self.identity_name,
                Identity(location=self.region, tags={state_keys.MANAGED_BY_TAG_KEY: state_keys.MANAGED_BY_TAG_VALUE}),
            )
            principal_id = identity.principal_id
            if not principal_id:
                raise BlobStateHostIdentityError(
                    f"User-assigned managed identity {self.identity_name!r} has no principal id; cannot "
                    "assign the Storage Blob Data Contributor role."
                )
            self._ensure_blob_role_assignment(principal_id)
        logger.info(
            "Provisioned managed identity {} (Storage Blob Data Contributor on account {})",
            self.identity_name,
            self.account_name,
        )
        return self.resource_id()

    def _blob_role_assignment_name(self, principal_id: str) -> str:
        """The deterministic role-assignment GUID for this principal on the account scope.

        Derived from the principal + scope so ``ensure`` and ``delete`` target the
        same assignment across runs.
        """
        return _blob_data_role_assignment_name(principal_id, self._account_scope())

    def _ensure_blob_role_assignment(self, principal_id: str) -> None:
        """Assign Storage Blob Data Contributor to the identity's principal, scoped to the state account.

        Delegates to the shared :func:`ensure_blob_data_contributor` helper (the
        same logic the operator grant uses), passing ``ServicePrincipal`` since a
        managed identity is a service principal.
        """
        ensure_blob_data_contributor(
            self._authorization(),
            self.subscription_id,
            self._account_scope(),
            principal_id,
            "ServicePrincipal",
            subject_label=f"managed identity {self.identity_name!r}",
            account_name=self.account_name,
            error_cls=BlobStateHostIdentityError,
        )

    def delete_host_identity(self) -> None:
        """Delete the scoped role assignment, then the user-assigned managed identity. Idempotent.

        The role assignment is deleted explicitly (rather than relying on Azure's
        stale-principal reaping) so cleanup leaves nothing orphaned, mirroring how
        ``S3StateHostIdentity`` explicitly detaches+deletes its role/profile/policy.
        Both deletes tolerate a missing target. Skips the assignment delete when the
        identity is already gone (no principal to derive the assignment name from).
        """
        with _translate_identity_errors(self.identity_name):
            identity = self._get_identity()
            if identity is not None and identity.principal_id:
                self._delete_blob_role_assignment_if_present(identity.principal_id)
            try:
                self._msi().user_assigned_identities.delete(self.resource_group, self.identity_name)
            except ResourceNotFoundError:
                return
        logger.info("Deleted managed identity {} for state account {}", self.identity_name, self.account_name)

    def _delete_blob_role_assignment_if_present(self, principal_id: str) -> None:
        """Delete the deterministic blob-data-contributor assignment for this principal. Idempotent."""
        try:
            self._authorization().role_assignments.delete(
                scope=self._account_scope(),
                role_assignment_name=self._blob_role_assignment_name(principal_id),
            )
        except ResourceNotFoundError:
            return


class BlobVolume(BaseVolume):
    """A ``Volume`` backed by an Azure Blob container, for reading a host's offline host_dir.

    Maps volume-relative paths to blob names under whatever prefix it is
    ``scoped()`` to. Reads use the operator's credentials (the same data-plane
    auth as ``BlobStateBucket``). Blob storage has no real directories, so a
    "directory" is the set of blobs sharing a prefix; ``listdir`` synthesizes
    directory entries from the common prefixes a delimited walk returns. Mirrors
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

    def listdir(self, path: str) -> list[VolumeFile]:
        prefix = _as_dir_prefix(path)
        entries: list[VolumeFile] = []
        with _translate_blob_errors(self.account_name):
            for item in self._container().walk_blobs(name_starts_with=prefix, delimiter="/"):
                name = item.name
                # A delimited walk yields BlobPrefix entries (name ends with "/")
                # for immediate sub-"directories" and BlobProperties for files
                # directly under the prefix.
                if name.endswith("/"):
                    child = name[len(prefix) :].rstrip("/")
                    if child:
                        entries.append(VolumeFile(path=child, file_type=FileType.DIRECTORY, mtime=0, size=0))
                    continue
                child = name[len(prefix) :]
                if not child or "/" in child:
                    continue
                # A file entry is a BlobProperties with typed last_modified / size.
                last_modified = item.last_modified
                entries.append(
                    VolumeFile(
                        path=child,
                        file_type=FileType.FILE,
                        mtime=int(last_modified.timestamp()) if last_modified is not None else 0,
                        size=item.size or 0,
                    )
                )
        return entries

    def path_exists(self, path: str) -> bool:
        key = path.lstrip("/")
        # A file exists if the exact blob exists; a directory exists if any blob
        # shares its prefix. One prefix list covers both.
        dir_prefix = _as_dir_prefix(path)
        with _translate_blob_errors(self.account_name):
            for blob in self._container().list_blobs(name_starts_with=key):
                if blob.name == key or blob.name.startswith(dir_prefix):
                    return True
        return False

    def read_file(self, path: str) -> bytes:
        key = path.lstrip("/")
        try:
            with _translate_blob_errors(self.account_name):
                return self._container().download_blob(key).readall()
        except BlobStateBucketError as e:
            if isinstance(e.__cause__, ResourceNotFoundError):
                raise BlobStateBucketError(f"File {path!r} does not exist in container {self.container_name!r}") from e
            raise

    def remove_file(self, path: str, *, recursive: bool = False) -> None:
        if recursive:
            self.remove_directory(path)
            return
        try:
            with _translate_blob_errors(self.account_name):
                self._container().delete_blob(path.lstrip("/"))
        except BlobStateBucketError as e:
            if isinstance(e.__cause__, ResourceNotFoundError):
                return
            raise

    def remove_directory(self, path: str) -> None:
        prefix = _as_dir_prefix(path)
        with _translate_blob_errors(self.account_name):
            container = self._container()
            for blob in list(container.list_blobs(name_starts_with=prefix)):
                container.delete_blob(blob.name)

    def write_files(self, file_contents_by_path: Mapping[str, bytes]) -> None:
        with _translate_blob_errors(self.account_name):
            container = self._container()
            for path, content in file_contents_by_path.items():
                container.upload_blob(name=path.lstrip("/"), data=content, overwrite=True)


def _as_dir_prefix(path: str) -> str:
    """Normalize a volume path to a blob directory prefix (no leading slash, trailing slash)."""
    cleaned = path.strip("/")
    return f"{cleaned}/" if cleaned else ""


@contextmanager
def _translate_identity_errors(identity_name: str) -> Iterator[None]:
    """Translate ``azure.core.exceptions.AzureError`` into ``BlobStateHostIdentityError`` within the block."""
    try:
        yield
    except AzureError as e:
        raise BlobStateHostIdentityError(f"Managed-identity operation on {identity_name!r} failed: {e}") from e


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
