"""Shared test helpers and constants for mngr_azure.

Lives outside ``conftest.py`` so other test modules (e.g. ``test_release_azure``)
can import these directly; importing from a ``conftest.py`` is a pytest
anti-pattern (those files are auto-discovered, not designed for direct import).
Mirrors ``libs/mngr_aws/imbue/mngr_aws/testing.py`` and ``mngr_gcp``'s.
"""

import base64
import json
import os
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from typing import Final

from azure.core.exceptions import AzureError
from azure.core.exceptions import HttpResponseError
from azure.core.exceptions import ResourceExistsError
from azure.core.exceptions import ResourceNotFoundError
from pydantic import Field

from imbue.mngr.primitives import HostId
from imbue.mngr_azure.client import AzureVpsClient
from imbue.mngr_azure.config import AzureProviderConfig
from imbue.mngr_azure.errors import AzureSubscriptionError
from imbue.mngr_azure.state_bucket import BlobStateBucket
from imbue.mngr_azure.state_bucket import BlobVolume

# Optional prefix release tests use for their agent names so leaked VMs (should
# the scanner ever fail) are still visually identifiable as mngr-created test
# VMs. No "azure" in the prefix: a leaked VM is already in an Azure subscription,
# so naming it "azure" would be redundant. Cleanup logic does NOT depend on this
# -- ``AzureVpsClient.create_instance`` tags pytest-launched VMs with
# ``mngr-pytest-launched=true`` and the conftest scanner filters on that tag.
AZURE_TEST_NAME_PREFIX: Final[str] = "mngr-test-"

# Region used by the Azure release tests and the session-end leak scan. Tests can
# override via ``MNGR_AZURE_REGION``; defaults to ``westus``. Read once at import
# time so conftest and test_release_azure observe the same value.
AZURE_DEFAULT_REGION: Final[str] = os.environ.get("MNGR_AZURE_REGION", "westus")

# VM size the lifecycle release tests provision. Defaults to a D-series size
# because B-series (the provider default, ``Standard_B2s``) is currently
# ``NotAvailableForSubscription`` in westus -- so without this override the
# create/exec/destroy and stop/start tests would fail on ``SkuNotAvailable``
# before exercising anything. Override via ``MNGR_AZURE_VM_SIZE``.
AZURE_TEST_VM_SIZE: Final[str] = os.environ.get("MNGR_AZURE_VM_SIZE", "Standard_D2s_v3")

# Resource group the release tests + scanner operate in. Read once at import time.
AZURE_DEFAULT_RESOURCE_GROUP: Final[str] = os.environ.get("MNGR_AZURE_RESOURCE_GROUP", "mngr")

# Release-test opt-in flag. Mirrors the gate that ``test_release_azure.py`` uses
# on ``pytestmark`` and that ``conftest.py`` uses to suppress the session-end
# orphan scan when no release tests were requested.
AZURE_RELEASE_TESTS_OPT_IN: Final[bool] = os.environ.get("MNGR_AZURE_RELEASE_TESTS") == "1"

# Single source of truth for the release-test VM lifetime. Used in two places
# that must stay aligned:
#   1. ``test_release_azure.py`` writes it into a tmp-path settings.toml
#      (``[providers.azure] auto_shutdown_seconds``) so cloud-init runs
#      ``shutdown -P +N`` on every test VM.
#   2. ``conftest.py`` derives the orphan-scan grace period from this value so
#      the session-end leak detector never race-kills an in-flight test on a
#      parallel worker.
AZURE_TEST_INSTANCE_AUTO_SHUTDOWN_SECONDS: Final[int] = 60 * 60


def azure_credentials_available() -> bool:
    """Return True iff ``DefaultAzureCredential`` can mint an ARM token.

    Used to gate release tests and the session-end cleanup hook (no-op when
    credentials are absent). Mints an ARM-scoped token through the same
    ``AzureProviderConfig.get_credential`` the provider uses at construction time,
    so the gate and production code agree on what counts as "available". This only
    runs behind the ``MNGR_AZURE_RELEASE_TESTS`` opt-in, so the network call never
    fires in an ordinary unit-test run.
    """
    try:
        AzureProviderConfig().get_credential().get_token("https://management.azure.com/.default")
    except (AzureError, ValueError):
        return False
    return True


def get_default_subscription_id() -> str | None:
    """Return the resolved Azure subscription for release tests / the scanner, or None.

    Routes through the exact resolution the provider uses on the normal create
    path: the ``MNGR_AZURE_SUBSCRIPTION_ID`` env override is mapped onto the
    config's ``subscription_id`` (so the configured value wins), and resolution
    otherwise falls back to ``AZURE_SUBSCRIPTION_ID`` and then the Azure CLI's
    active subscription -- via ``AzureProviderConfig.get_subscription_id``, the
    same code production runs. The production path raises ``AzureSubscriptionError``
    when nothing resolves; here we translate that to ``None`` so the release tests
    and the session-end scanner can skip cleanly instead of erroring.
    """
    config = AzureProviderConfig(subscription_id=os.environ.get("MNGR_AZURE_SUBSCRIPTION_ID", ""))
    try:
        return config.get_subscription_id()
    except AzureSubscriptionError:
        return None


def make_azure_http_error(status_code: int, message: str) -> HttpResponseError:
    """Build an ``HttpResponseError`` with a set ``status_code`` for fakes to raise.

    Constructing with ``response=None`` skips the SDK's response-body parsing;
    the status code (which ``AzureVpsClient._translate_azure_errors`` reads) is
    then assigned directly. ``ResourceNotFoundError`` / ``ClientAuthenticationError``
    both subclass ``HttpResponseError``, so a plain instance with the right
    status code exercises the same translation path.
    """
    error = HttpResponseError(message=message)
    error.status_code = status_code
    return error


class FakePoller:
    """Stand-in for an azure LROPoller: ``result()``/``wait()`` return a preset value or raise.

    ``wait()`` re-raises the preset error (like the real poller surfacing the
    operation's failure) but, like the real one, does NOT raise merely because a
    timeout elapsed. Set ``completes=False`` to model an operation still in flight
    after ``wait(timeout)`` so ``done()`` reports ``False`` (the timeout path).
    """

    def __init__(self, result_value: Any = None, error: Exception | None = None, completes: bool = True) -> None:
        self._result_value = result_value
        self._error = error
        self._completes = completes

    def wait(self, timeout: float | None = None) -> None:
        del timeout
        if self._error is not None:
            raise self._error

    def done(self) -> bool:
        return self._completes

    def result(self, timeout: float | None = None) -> Any:
        del timeout
        if self._error is not None:
            raise self._error
        return self._result_value


class FakeVirtualMachinesOperations:
    """Fake ComputeManagementClient.virtual_machines: records calls, returns canned data."""

    def __init__(self) -> None:
        self.created: list[tuple[str, Any]] = []
        self.deleted: list[str] = []
        self.deallocated: list[str] = []
        self.started: list[str] = []
        self.create_error: Exception | None = None
        self.delete_error: Exception | None = None
        self.deallocate_error: Exception | None = None
        self.start_error: Exception | None = None
        # When False, the corresponding long-running-operation poller reports ``done()`` as False after
        # ``wait(timeout)`` -- models an operation still in flight at the deadline.
        self.deallocate_completes: bool = True
        self.start_completes: bool = True
        self.instance_view_result: Any = None
        self.instance_view_error: Exception | None = None
        self.get_result: Any = None
        self.get_error: Exception | None = None
        self.list_result: list[Any] = []
        self.list_error: Exception | None = None
        # Records ``(vm_name, parameters)`` for each begin_update call (tag upserts).
        self.updated: list[tuple[str, Any]] = []
        self.update_error: Exception | None = None
        # Records the ``expand`` value the production code passes to ``list`` so a
        # test can assert it is NOT ``instanceView`` (Azure 400s that on a
        # resource-group list; power state is fetched per-VM via instance_view).
        self.last_list_expand: str | None = None

    def begin_create_or_update(self, resource_group: str, vm_name: str, parameters: Any) -> FakePoller:
        self.created.append((vm_name, parameters))
        return FakePoller(error=self.create_error)

    def begin_delete(self, resource_group: str, vm_name: str) -> FakePoller:
        if self.delete_error is not None:
            return FakePoller(error=self.delete_error)
        self.deleted.append(vm_name)
        return FakePoller()

    def begin_deallocate(self, resource_group: str, vm_name: str) -> FakePoller:
        if self.deallocate_error is not None:
            return FakePoller(error=self.deallocate_error)
        self.deallocated.append(vm_name)
        return FakePoller(completes=self.deallocate_completes)

    def begin_start(self, resource_group: str, vm_name: str) -> FakePoller:
        if self.start_error is not None:
            return FakePoller(error=self.start_error)
        self.started.append(vm_name)
        return FakePoller(completes=self.start_completes)

    def begin_update(self, resource_group: str, vm_name: str, parameters: Any) -> FakePoller:
        if self.update_error is not None:
            return FakePoller(error=self.update_error)
        self.updated.append((vm_name, parameters))
        return FakePoller()

    def instance_view(self, resource_group: str, vm_name: str) -> Any:
        if self.instance_view_error is not None:
            raise self.instance_view_error
        return self.instance_view_result

    def get(self, resource_group: str, vm_name: str) -> Any:
        if self.get_error is not None:
            raise self.get_error
        return self.get_result

    def list(self, resource_group: str, expand: str | None = None) -> list[Any]:
        self.last_list_expand = expand
        if self.list_error is not None:
            raise self.list_error
        return self.list_result


class FakeComputeClient:
    """Fake ComputeManagementClient bundling the virtual_machines operations."""

    def __init__(self) -> None:
        self.virtual_machines = FakeVirtualMachinesOperations()


class FakePublicIPAddressesOperations:
    """Fake NetworkManagementClient.public_ip_addresses."""

    def __init__(self) -> None:
        self.created: list[tuple[str, Any]] = []
        self.deleted: list[str] = []
        self.get_result: Any = None
        self.get_error: Exception | None = None
        self.list_result: list[Any] = []

    def begin_create_or_update(self, resource_group: str, name: str, parameters: Any) -> FakePoller:
        self.created.append((name, parameters))
        return FakePoller(result_value=SimpleNamespace(id=f"/pip/{name}", name=name, ip_address="203.0.113.7"))

    def begin_delete(self, resource_group: str, name: str) -> FakePoller:
        self.deleted.append(name)
        return FakePoller()

    def get(self, resource_group: str, name: str) -> Any:
        if self.get_error is not None:
            raise self.get_error
        return self.get_result

    def list(self, resource_group: str) -> list[Any]:
        return self.list_result


class FakeNetworkInterfacesOperations:
    """Fake NetworkManagementClient.network_interfaces."""

    def __init__(self) -> None:
        self.created: list[tuple[str, Any]] = []
        self.deleted: list[str] = []
        self.list_result: list[Any] = []

    def begin_create_or_update(self, resource_group: str, name: str, parameters: Any) -> FakePoller:
        self.created.append((name, parameters))
        return FakePoller(result_value=SimpleNamespace(id=f"/nic/{name}", name=name))

    def begin_delete(self, resource_group: str, name: str) -> FakePoller:
        self.deleted.append(name)
        return FakePoller()

    def list(self, resource_group: str) -> list[Any]:
        return self.list_result


class FakeVirtualNetworksOperations:
    """Fake NetworkManagementClient.virtual_networks."""

    def __init__(self) -> None:
        self.created: list[tuple[str, Any]] = []

    def begin_create_or_update(self, resource_group: str, name: str, parameters: Any) -> FakePoller:
        self.created.append((name, parameters))
        return FakePoller(result_value=SimpleNamespace(id=f"/vnet/{name}", name=name))


class FakeSubnetsOperations:
    """Fake NetworkManagementClient.subnets: ``get`` raises 404 unless a subnet is preset."""

    def __init__(self) -> None:
        self.get_result: Any = None
        self.get_error: Exception | None = None

    def get(self, resource_group: str, vnet_name: str, subnet_name: str) -> Any:
        if self.get_error is not None:
            raise self.get_error
        if self.get_result is None:
            raise make_azure_http_error(404, "subnet not found")
        return self.get_result


class FakeNetworkSecurityGroupsOperations:
    """Fake NetworkManagementClient.network_security_groups."""

    def __init__(self) -> None:
        self.created: list[tuple[str, Any]] = []

    def begin_create_or_update(self, resource_group: str, name: str, parameters: Any) -> FakePoller:
        self.created.append((name, parameters))
        return FakePoller(result_value=SimpleNamespace(id=f"/nsg/{name}", name=name))


class FakeNetworkClient:
    """Fake NetworkManagementClient bundling the operation groups the client uses."""

    def __init__(self) -> None:
        self.public_ip_addresses = FakePublicIPAddressesOperations()
        self.network_interfaces = FakeNetworkInterfacesOperations()
        self.virtual_networks = FakeVirtualNetworksOperations()
        self.subnets = FakeSubnetsOperations()
        self.network_security_groups = FakeNetworkSecurityGroupsOperations()


class FakeResourceGroupsOperations:
    """Fake ResourceManagementClient.resource_groups."""

    def __init__(self) -> None:
        self.created: list[tuple[str, Any]] = []
        self.deleted: list[str] = []
        self.get_result: Any = None
        self.get_error: Exception | None = None
        # Whether ``check_existence`` reports the RG as already present (drives the
        # ``was_created`` signal ensure_network returns). Default False: a fresh RG.
        self.exists: bool = False

    def check_existence(self, resource_group: str) -> bool:
        return self.exists

    def create_or_update(self, resource_group: str, parameters: Any) -> Any:
        self.created.append((resource_group, parameters))
        return SimpleNamespace(name=resource_group)

    def get(self, resource_group: str) -> Any:
        if self.get_error is not None:
            raise self.get_error
        if self.get_result is None:
            raise make_azure_http_error(404, "resource group not found")
        return self.get_result

    def begin_delete(self, resource_group: str) -> FakePoller:
        self.deleted.append(resource_group)
        return FakePoller()


class FakeProvidersOperations:
    """Fake ResourceManagementClient.providers.

    ``registration_state`` is the *initial* state reported by ``get`` for a
    namespace that has not been registered yet. Once ``register`` is called for a
    namespace, subsequent ``get`` calls report it ``Registered`` -- modeling the
    real (eventually-consistent) registration completing, so the production
    poll loop in ``_wait_for_provider_registered`` terminates without sleeping.
    """

    def __init__(self) -> None:
        self.registered: list[str] = []
        self.registration_state: str = "Registered"

    def get(self, namespace: str) -> Any:
        state = "Registered" if namespace in self.registered else self.registration_state
        return SimpleNamespace(namespace=namespace, registration_state=state)

    def register(self, namespace: str) -> Any:
        self.registered.append(namespace)
        return SimpleNamespace(namespace=namespace)


class FakeTagsOperations:
    """Fake ResourceManagementClient.tags: records server-side tag Merge/Delete patches."""

    def __init__(self) -> None:
        # (scope, parameters) for each begin_update_at_scope call.
        self.updates: list[tuple[str, Any]] = []
        self.update_error: Exception | None = None

    def begin_update_at_scope(self, scope: str, parameters: Any) -> FakePoller:
        if self.update_error is not None:
            return FakePoller(error=self.update_error)
        self.updates.append((scope, parameters))
        return FakePoller()


class FakeResourceClient:
    """Fake ResourceManagementClient bundling resource_groups + providers + tags."""

    def __init__(self) -> None:
        self.resource_groups = FakeResourceGroupsOperations()
        self.providers = FakeProvidersOperations()
        self.tags = FakeTagsOperations()


class FakeRoleDefinitionsOperations:
    """Fake AuthorizationManagementClient.role_definitions."""

    def __init__(self) -> None:
        self.created: list[tuple[str, str, Any]] = []
        self.create_error: Exception | None = None

    def create_or_update(self, scope: str, role_definition_id: str, role_definition: Any) -> Any:
        if self.create_error is not None:
            raise self.create_error
        self.created.append((scope, role_definition_id, role_definition))
        return SimpleNamespace(
            id=f"/subscriptions/sub/providers/Microsoft.Authorization/roleDefinitions/{role_definition_id}"
        )


class FakeRoleAssignmentsOperations:
    """Fake AuthorizationManagementClient.role_assignments."""

    def __init__(self) -> None:
        self.created: list[tuple[str, str, Any]] = []
        self.deleted: list[tuple[str, str]] = []
        self.create_error: Exception | None = None

    def create(self, scope: str, role_assignment_name: str, parameters: Any) -> Any:
        if self.create_error is not None:
            raise self.create_error
        self.created.append((scope, role_assignment_name, parameters))
        return SimpleNamespace(id=f"{scope}/providers/Microsoft.Authorization/roleAssignments/{role_assignment_name}")

    def delete(self, scope: str, role_assignment_name: str) -> Any:
        # A missing assignment 404s in the real SDK; model that so the production
        # idempotent-delete path is exercised when nothing was ever created.
        if (scope, role_assignment_name) not in {(s, n) for s, n, _p in self.created}:
            raise ResourceNotFoundError(message=f"role assignment {role_assignment_name} not found")
        self.deleted.append((scope, role_assignment_name))
        return None


class FakeAuthorizationClient:
    """Fake AuthorizationManagementClient bundling role_definitions + role_assignments."""

    def __init__(self) -> None:
        self.role_definitions = FakeRoleDefinitionsOperations()
        self.role_assignments = FakeRoleAssignmentsOperations()


def _encode_fake_jwt(claims: dict[str, Any]) -> str:
    """Build an (unsigned) JWT string whose payload carries ``claims``.

    Used so ``resolve_operator_principal``'s real base64url-decode path is
    exercised in tests against a chosen ``oid`` / ``idtyp``.
    """

    def _segment(obj: dict[str, Any]) -> str:
        raw = json.dumps(obj).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return f"{_segment({'alg': 'none', 'typ': 'JWT'})}.{_segment(claims)}.sig"


class FakeTokenCredential:
    """Minimal ``TokenCredential`` whose access token carries chosen ``oid`` / ``idtyp`` claims.

    Lets tests drive ``resolve_operator_principal`` (and the operator blob-role
    grant) without a real Azure login. Pass ``object_id=""`` to model a token with
    no ``oid`` claim.
    """

    def __init__(self, object_id: str = "operator-oid-1", idtyp: str | None = "user") -> None:
        self._object_id = object_id
        self._idtyp = idtyp

    def get_token(self, *scopes: str, **kwargs: Any) -> Any:
        del scopes, kwargs
        claims: dict[str, Any] = {"oid": self._object_id}
        if self._idtyp is not None:
            claims["idtyp"] = self._idtyp
        return SimpleNamespace(token=_encode_fake_jwt(claims), expires_on=0)


class _StubbedAzureVpsClient(AzureVpsClient):
    """Test-only AzureVpsClient that injects fake management clients.

    Production ``AzureVpsClient`` builds the azure-mgmt clients lazily from its
    credential; this subclass exposes constructor fields that callers can
    populate with hand-written fakes so unit tests exercise the request-building
    and response-handling logic without real API calls. Keeping the test-only
    injection out of the production model means production code never carries a
    field whose sole purpose is test orchestration.
    """

    stubbed_compute_client: Any = Field(default=None, description="Fake ComputeManagementClient")
    stubbed_network_client: Any = Field(default=None, description="Fake NetworkManagementClient")
    stubbed_resource_client: Any = Field(default=None, description="Fake ResourceManagementClient")
    stubbed_authorization_client: Any = Field(default=None, description="Fake AuthorizationManagementClient")

    def _compute(self) -> Any:
        return self.stubbed_compute_client

    def _network(self) -> Any:
        return self.stubbed_network_client

    def _resource(self) -> Any:
        return self.stubbed_resource_client

    def _authorization(self) -> Any:
        return self.stubbed_authorization_client


class FakeBlobStorageBackend:
    """In-memory backing store for the Azure Blob + storage-management fakes.

    There is no moto-equivalent for Azure Blob, so this models the slice of
    behavior ``BlobStateBucket`` depends on: a single storage account that may or
    may not exist, and one container holding ``{blob_name: bytes}``. Shared by the
    data-plane and management-plane fakes so they observe a consistent state.
    """

    def __init__(self, account_exists: bool = False) -> None:
        self.account_exists: bool = account_exists
        self.container_exists: bool = False
        self.blobs_by_name: dict[str, bytes] = {}
        self.deleted_account: bool = False


class _FakeBlobDownloader:
    """Stand-in for the StorageStreamDownloader: ``readall`` returns the blob bytes."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def readall(self) -> bytes:
        return self._data


class FakeContainerClient:
    """Fake ``ContainerClient`` over a ``FakeBlobStorageBackend``.

    ``list_blobs`` items carry ``size`` / ``last_modified`` so ``BlobVolume.listdir``
    can read them. ``walk_blobs`` models the delimited walk the real SDK does:
    blobs directly under the prefix are returned as themselves, and deeper blobs
    collapse to a single ``BlobPrefix``-shaped entry whose name ends with ``/``.
    """

    def __init__(self, backend: FakeBlobStorageBackend) -> None:
        self._backend = backend

    def exists(self) -> bool:
        return self._backend.container_exists

    def list_blobs(self, name_starts_with: str = "") -> Iterator[Any]:
        for name in sorted(self._backend.blobs_by_name):
            if name.startswith(name_starts_with):
                data = self._backend.blobs_by_name[name]
                yield SimpleNamespace(name=name, size=len(data), last_modified=None)

    def walk_blobs(self, name_starts_with: str = "", delimiter: str = "/") -> Iterator[Any]:
        seen_prefixes: set[str] = set()
        for name in sorted(self._backend.blobs_by_name):
            if not name.startswith(name_starts_with):
                continue
            remainder = name[len(name_starts_with) :]
            head, sep, _tail = remainder.partition(delimiter)
            if sep:
                # A nested blob -> collapse to its immediate sub-"directory" prefix.
                prefix = f"{name_starts_with}{head}{delimiter}"
                if prefix not in seen_prefixes:
                    seen_prefixes.add(prefix)
                    yield SimpleNamespace(name=prefix)
            else:
                data = self._backend.blobs_by_name[name]
                yield SimpleNamespace(name=name, size=len(data), last_modified=None)

    def upload_blob(self, name: str, data: bytes, overwrite: bool = False) -> None:
        if name in self._backend.blobs_by_name and not overwrite:
            raise ResourceExistsError(message=f"blob {name} already exists")
        self._backend.blobs_by_name[name] = data

    def download_blob(self, name: str) -> _FakeBlobDownloader:
        if name not in self._backend.blobs_by_name:
            raise ResourceNotFoundError(message=f"blob {name} not found")
        return _FakeBlobDownloader(self._backend.blobs_by_name[name])

    def delete_blob(self, name: str) -> None:
        if name not in self._backend.blobs_by_name:
            raise ResourceNotFoundError(message=f"blob {name} not found")
        del self._backend.blobs_by_name[name]


class FakeBlobServiceClient:
    """Fake ``BlobServiceClient`` returning a single shared ``FakeContainerClient``."""

    def __init__(self, backend: FakeBlobStorageBackend) -> None:
        self._backend = backend

    def get_container_client(self, container_name: str) -> FakeContainerClient:
        del container_name
        return FakeContainerClient(self._backend)

    def create_container(self, name: str) -> None:
        del name
        if self._backend.container_exists:
            raise ResourceExistsError(message="container already exists")
        self._backend.container_exists = True


class FakeStorageAccountsOperations:
    """Fake ``StorageManagementClient.storage_accounts``."""

    def __init__(self, backend: FakeBlobStorageBackend) -> None:
        self._backend = backend

    def get_properties(self, resource_group_name: str, account_name: str) -> Any:
        del resource_group_name
        if not self._backend.account_exists:
            raise ResourceNotFoundError(message="storage account not found")
        return SimpleNamespace(name=account_name)

    def begin_create(self, resource_group_name: str, account_name: str, parameters: Any) -> FakePoller:
        del resource_group_name, parameters
        self._backend.account_exists = True
        return FakePoller(result_value=SimpleNamespace(name=account_name))

    def delete(self, resource_group_name: str, account_name: str) -> None:
        del resource_group_name, account_name
        self._backend.account_exists = False
        self._backend.container_exists = False
        self._backend.blobs_by_name.clear()
        self._backend.deleted_account = True


class FakeStorageManagementClient:
    """Fake ``StorageManagementClient`` bundling the ``storage_accounts`` operations."""

    def __init__(self, backend: FakeBlobStorageBackend) -> None:
        self.storage_accounts = FakeStorageAccountsOperations(backend)


class _StubbedBlobVolume(BlobVolume):
    """Test-only ``BlobVolume`` whose data-plane client is a fake over a shared backend."""

    fake_backend: Any = Field(default=None, description="Shared FakeBlobStorageBackend for the injected fake")

    def _blob_service(self) -> Any:
        return FakeBlobServiceClient(self.fake_backend)


class _StubbedBlobStateBucket(BlobStateBucket):
    """Test-only ``BlobStateBucket`` that injects in-memory blob + storage clients.

    Production ``BlobStateBucket`` builds the azure SDK clients lazily from its
    credential; this subclass routes the data-plane and management-plane client
    accessors to hand-written fakes backed by a single ``FakeBlobStorageBackend``,
    so unit tests exercise the request-building and response-handling logic without
    real Azure calls. Mirrors ``_StubbedAzureVpsClient``.
    """

    fake_backend: Any = Field(default=None, description="Shared FakeBlobStorageBackend for the injected fakes")
    fake_authorization: Any = Field(
        default=None, description="Fake AuthorizationManagementClient for the operator blob-role grant"
    )

    def _blob_service(self) -> Any:
        return FakeBlobServiceClient(self.fake_backend)

    def _storage_mgmt(self) -> Any:
        return FakeStorageManagementClient(self.fake_backend)

    def _authorization(self) -> Any:
        return self.fake_authorization

    def volume_for_host(self, host_id: HostId) -> Any:
        """Return a fake-backed ``BlobVolume`` scoped to the host's host_dir prefix.

        Overrides the production builder so offline-read tests against a stubbed
        bucket exercise ``BlobVolume`` over the same in-memory backend (the
        production method would build a real credential-backed ``BlobVolume``).
        """
        host_dir_prefix = f"hosts/{host_id.get_uuid().hex}/host_dir"
        return _StubbedBlobVolume(
            credential=None,
            account_name=self.account_name,
            container_name=self.container_name,
            fake_backend=self.fake_backend,
        ).scoped(host_dir_prefix)
