"""Shared test helpers and constants for mngr_azure.

Lives outside ``conftest.py`` so other test modules (e.g. ``test_release_azure``)
can import these directly; importing from a ``conftest.py`` is a pytest
anti-pattern (those files are auto-discovered, not designed for direct import).
Mirrors ``libs/mngr_aws/imbue/mngr_aws/testing.py`` and ``mngr_gcp``'s.
"""

import os
from types import SimpleNamespace
from typing import Any
from typing import Final

from azure.core.exceptions import AzureError
from azure.core.exceptions import HttpResponseError
from pydantic import Field

from imbue.mngr_azure.client import AzureVpsClient
from imbue.mngr_azure.config import AzureProviderConfig
from imbue.mngr_azure.errors import AzureSubscriptionError

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

    Used to gate release tests and the session-end cleanup hook (which, when
    ``MNGR_AZURE_RELEASE_TESTS`` is set, fails the session if credentials are
    absent rather than skipping). Mints an ARM-scoped token through the same
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
    """Stand-in for an azure LROPoller: ``result()`` returns a preset value or raises."""

    def __init__(self, result_value: Any = None, error: Exception | None = None) -> None:
        self._result_value = result_value
        self._error = error

    def result(self) -> Any:
        if self._error is not None:
            raise self._error
        return self._result_value


class FakeVirtualMachinesOperations:
    """Fake ComputeManagementClient.virtual_machines: records calls, returns canned data."""

    def __init__(self) -> None:
        self.created: list[tuple[str, Any]] = []
        self.deleted: list[str] = []
        self.create_error: Exception | None = None
        self.delete_error: Exception | None = None
        self.instance_view_result: Any = None
        self.instance_view_error: Exception | None = None
        self.get_result: Any = None
        self.get_error: Exception | None = None
        self.list_result: list[Any] = []
        self.list_error: Exception | None = None

    def begin_create_or_update(self, resource_group: str, vm_name: str, parameters: Any) -> FakePoller:
        self.created.append((vm_name, parameters))
        return FakePoller(error=self.create_error)

    def begin_delete(self, resource_group: str, vm_name: str) -> FakePoller:
        if self.delete_error is not None:
            return FakePoller(error=self.delete_error)
        self.deleted.append(vm_name)
        return FakePoller()

    def instance_view(self, resource_group: str, vm_name: str) -> Any:
        if self.instance_view_error is not None:
            raise self.instance_view_error
        return self.instance_view_result

    def get(self, resource_group: str, vm_name: str) -> Any:
        if self.get_error is not None:
            raise self.get_error
        return self.get_result

    def list(self, resource_group: str) -> list[Any]:
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


class FakeResourceClient:
    """Fake ResourceManagementClient bundling resource_groups + providers."""

    def __init__(self) -> None:
        self.resource_groups = FakeResourceGroupsOperations()
        self.providers = FakeProvidersOperations()


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

    def _compute(self) -> Any:
        return self.stubbed_compute_client

    def _network(self) -> Any:
        return self.stubbed_network_client

    def _resource(self) -> Any:
        return self.stubbed_resource_client
