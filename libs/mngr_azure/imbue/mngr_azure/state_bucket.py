import json
from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import contextmanager
from typing import Any
from typing import Final

from azure.core.exceptions import AzureError
from azure.core.exceptions import ResourceExistsError
from azure.core.exceptions import ResourceNotFoundError
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
from imbue.mngr.primitives import HostId

# Object-key layout in the state bucket, per host. The full host record lives at
# ``hosts/<host_id_hex>/host_state.json`` and each agent's record under
# ``hosts/<host_id_hex>/agents/<agent_id>.json``. ``<host_id_hex>`` matches the
# per-host btrfs subvolume naming (``host_id.get_uuid().hex``) so the same id keys
# both the on-instance volume and the bucket. Identical to the AWS S3 layout.
_HOSTS_PREFIX: Final[str] = "hosts"
_HOST_STATE_FILENAME: Final[str] = "host_state.json"
_AGENTS_SUBPREFIX: Final[str] = "agents"

# The Azure analog of an S3 bucket is a Blob *container* inside a *storage
# account*. The container name is fixed (container names allow hyphens, 3-63
# lowercase); the storage-account name is derived per scope (3-24 chars,
# lowercase alphanumeric only) -- see ``AzureProviderConfig`` for derivation.
DEFAULT_STATE_CONTAINER_NAME: Final[str] = "mngr-state"

# Tag/metadata marking the storage account as mngr-managed, mirroring the AWS
# bucket's ``managed-by=mngr`` tag (so a future cleanup can prove ownership).
_MANAGED_BY_TAG_KEY: Final[str] = "managed-by"
_MANAGED_BY_TAG_VALUE: Final[str] = "mngr"

# Storage account SKU + kind for the state account: locally-redundant standard
# storage with a general-purpose-v2 account (the modern default), private.
_STORAGE_ACCOUNT_SKU: Final[str] = "Standard_LRS"
_STORAGE_ACCOUNT_KIND: Final[str] = "StorageV2"


class BlobStateBucketError(MngrError):
    """An Azure Blob state-bucket operation failed."""


class BlobStateBucket(MutableModel):
    """Reads/writes mngr control-plane state in an Azure Blob container, readable while offline.

    The Azure analog of ``S3StateBucket``: a Blob container inside a storage
    account holds the full host record and per-agent records keyed by host id,
    written by the mngr host machine with the operator's credentials, so a
    deallocated VM's state is readable without SSH and without the 256-char Azure
    tag limit. Data-plane access uses AAD (the same ``DefaultAzureCredential`` the
    provider uses) against ``https://<account>.blob.core.windows.net``; the
    storage account itself is created/deleted via the storage management plane.
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

    def _container(self) -> Any:
        return self._blob_service().get_container_client(self.container_name)

    def _host_prefix(self, host_id: HostId) -> str:
        return f"{_HOSTS_PREFIX}/{host_id.get_uuid().hex}"

    def _host_state_key(self, host_id: HostId) -> str:
        return f"{self._host_prefix(host_id)}/{_HOST_STATE_FILENAME}"

    def _agent_key(self, host_id: HostId, agent_id: str) -> str:
        return f"{self._host_prefix(host_id)}/{_AGENTS_SUBPREFIX}/{agent_id}.json"

    def write_host_record(self, host_id: HostId, record_json: str) -> None:
        """Write the host record JSON for a host, overwriting any existing blob."""
        self._put_blob(self._host_state_key(host_id), record_json)

    def read_host_record(self, host_id: HostId) -> str | None:
        """Return the host record JSON for a host, or None if no blob exists."""
        return self._get_blob(self._host_state_key(host_id))

    def write_agent_record(self, host_id: HostId, agent_id: str, data: Mapping[str, object]) -> None:
        """Write a single agent's record (serialized as JSON) under the host's prefix."""
        self._put_blob(self._agent_key(host_id, agent_id), json.dumps(dict(data)))

    def list_agent_records(self, host_id: HostId) -> list[dict]:
        """Return every agent record stored under the host's ``agents/`` prefix.

        A stored blob that is not valid JSON (externally edited / corrupted) is
        skipped with a warning rather than crashing the listing.
        """
        agents_prefix = f"{self._host_prefix(host_id)}/{_AGENTS_SUBPREFIX}/"
        records: list[dict] = []
        for key in self._list_keys(agents_prefix):
            body = self._get_blob(key)
            if body is None:
                continue
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError as e:
                logger.warning("Skipping unparseable agent record {} in container {}: {}", key, self.container_name, e)
                continue
            if isinstance(parsed, dict):
                records.append(parsed)
            else:
                logger.warning("Skipping agent record {} in container {}: not a JSON object", key, self.container_name)
        return records

    def remove_agent_record(self, host_id: HostId, agent_id: str) -> None:
        """Delete a single agent's record. Idempotent (no error if absent)."""
        self._delete_blob(self._agent_key(host_id, agent_id))

    def delete_host_state(self, host_id: HostId) -> None:
        """Delete every blob under the host's prefix. Idempotent."""
        for key in self._list_keys(f"{self._host_prefix(host_id)}/"):
            self._delete_blob(key)

    def has_any_host_state(self) -> bool:
        """Return whether any blob exists under the ``hosts/`` prefix."""
        with _translate_blob_errors(self.account_name):
            for _ in self._container().list_blobs(name_starts_with=f"{_HOSTS_PREFIX}/"):
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

    def _ensure_account(self) -> bool:
        """Create the storage account if absent. Returns True iff it was created."""
        if self.account_exists():
            logger.debug("Azure storage account {} already exists; skipping create", self.account_name)
            return False
        parameters = StorageAccountCreateParameters(
            sku=Sku(name=_STORAGE_ACCOUNT_SKU),
            kind=_STORAGE_ACCOUNT_KIND,
            location=self.region,
            tags={_MANAGED_BY_TAG_KEY: _MANAGED_BY_TAG_VALUE},
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

    def _put_blob(self, key: str, body: str) -> None:
        with _translate_blob_errors(self.account_name):
            self._container().upload_blob(name=key, data=body.encode("utf-8"), overwrite=True)

    def _get_blob(self, key: str) -> str | None:
        try:
            with _translate_blob_errors(self.account_name):
                downloader = self._container().download_blob(key)
                return downloader.readall().decode("utf-8")
        except BlobStateBucketError as e:
            if isinstance(e.__cause__, ResourceNotFoundError):
                return None
            raise

    def _delete_blob(self, key: str) -> None:
        try:
            with _translate_blob_errors(self.account_name):
                self._container().delete_blob(key)
        except BlobStateBucketError as e:
            # A missing blob is a no-op (idempotent delete); anything else propagates.
            if isinstance(e.__cause__, ResourceNotFoundError):
                return
            raise

    def _list_keys(self, prefix: str) -> list[str]:
        with _translate_blob_errors(self.account_name):
            return [blob.name for blob in self._container().list_blobs(name_starts_with=prefix)]


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
