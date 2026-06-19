import base64
import os
import re
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import Final
from typing import Self
from uuid import NAMESPACE_URL
from uuid import uuid4
from uuid import uuid5

from azure.core.exceptions import HttpResponseError
from azure.core.polling import LROPoller
from azure.mgmt.authorization import AuthorizationManagementClient
from azure.mgmt.authorization.v2022_04_01.models import Permission
from azure.mgmt.authorization.v2022_04_01.models import RoleAssignmentCreateParameters
from azure.mgmt.authorization.v2022_04_01.models import RoleDefinition
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.compute import models as compute_models
from azure.mgmt.network import NetworkManagementClient
from azure.mgmt.network import models as network_models
from azure.mgmt.resource import ResourceManagementClient
from azure.mgmt.resource.resources.models import ResourceGroup
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import ProviderResourceInfo
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.polling import poll_until
from imbue.mngr_azure.config import AZURE_MANAGED_BY_TAG_KEY
from imbue.mngr_azure.config import AZURE_MANAGED_BY_TAG_VALUE
from imbue.mngr_azure.config import DEFAULT_IMAGE_OFFER
from imbue.mngr_azure.config import DEFAULT_IMAGE_PUBLISHER
from imbue.mngr_azure.config import DEFAULT_IMAGE_SKU
from imbue.mngr_azure.config import DEFAULT_IMAGE_VERSION
from imbue.mngr_azure.errors import InvalidAzureIdentifierError
from imbue.mngr_vps.errors import VpsApiError
from imbue.mngr_vps.errors import VpsProvisioningError
from imbue.mngr_vps.primitives import VpsInstanceId
from imbue.mngr_vps.primitives import VpsInstanceStatus
from imbue.mngr_vps.vps_client import VpsClientInterface

# Tag key/value that ``create_instance`` adds to every VM launched while
# ``PYTEST_CURRENT_TEST`` is set. The conftest session-end scanner uses this
# tag (not the VM name) to find leaked VMs, which means tests do not have to
# constrain host naming: any agent name works.
AZURE_PYTEST_LAUNCHED_TAG: Final[str] = "mngr-pytest-launched"

# Tag holding the human host name (``mngr-<host_name>``) so a deallocated VM still
# resolves by name in offline discovery (the read side strips the ``mngr-`` prefix).
HOST_NAME_TAG_KEY: Final[str] = "mngr-host-name"

# Least-privilege custom role that lets a VM's system-assigned managed identity
# deallocate ITSELF (idle self-stop) -- the only way to halt Azure compute billing
# from inside the guest, since an OS shutdown leaves the VM "Stopped (not
# deallocated)" still billing. ``mngr azure prepare`` creates the role
# definition; ``create``-time assigns it to each VM's identity scoped to that VM.
# The role-definition name must be a GUID, so derive a deterministic one from the
# role name (uuid5) to keep re-runs idempotent.
SELF_DEALLOCATE_ROLE_NAME: Final[str] = "mngr-self-deallocate"
SELF_DEALLOCATE_ROLE_ID: Final[str] = str(uuid5(NAMESPACE_URL, "mngr-self-deallocate-role"))

# Resource providers a brand-new subscription must have registered before any
# compute/network deploy. New pay-as-you-go subscriptions often start with these
# unregistered, producing opaque ``MissingSubscriptionRegistration`` errors;
# ``mngr azure prepare`` registers them up front.
_REQUIRED_RESOURCE_PROVIDERS: Final[tuple[str, ...]] = (
    "Microsoft.Compute",
    "Microsoft.Network",
    "Microsoft.Storage",
)

# Azure exposes the live run-state of a VM as an instance-view status whose code
# is ``PowerState/<state>``. Map those to the shared lifecycle enum.
_POWER_STATE_MAP: Final[dict[str, VpsInstanceStatus]] = {
    "PowerState/starting": VpsInstanceStatus.PENDING,
    "PowerState/running": VpsInstanceStatus.ACTIVE,
    "PowerState/stopping": VpsInstanceStatus.HALTED,
    "PowerState/stopped": VpsInstanceStatus.HALTED,
    "PowerState/deallocating": VpsInstanceStatus.HALTED,
    "PowerState/deallocated": VpsInstanceStatus.HALTED,
}

# Azure VM resource names are <= 64 chars; the Linux computer-name (hostname) is
# <= 64 but we cap at 63 to stay clear of RFC1035 host tooling edges. We append a
# 32-hex host-id suffix (+ separating dash) for uniqueness, leaving this many
# characters for the human-readable stem.
_MAX_VM_NAME_LENGTH: Final[int] = 64
_MAX_LINUX_HOSTNAME_LENGTH: Final[int] = 63
_INVALID_NAME_CHARS_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9-]")
_VM_NAME_STEM_LENGTH: Final[int] = _MAX_VM_NAME_LENGTH - 33
# The shape ``_make_vm_name`` produces: lowercase alphanumerics and dashes, not
# starting or ending with a dash, 1-64 chars. A subset of what Azure accepts for
# a Linux VM resource name, but the only shape the coercion ever emits. The same
# shape is a valid Linux hostname (within its tighter length cap), so
# ``LinuxHostname`` reuses it.
_AZURE_VM_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


class AzureVmName(NonEmptyStr):
    """An Azure VM resource name: non-empty, ``[a-z0-9-]``, no leading/trailing dash, at most 64 chars.

    The codebase models identifier strings as ``NonEmptyStr`` subtypes
    (``SnapshotId``, ``ProviderInstanceName``, ...); this is the Azure-VM-name
    analog (mirrors ``mngr_gcp``'s ``GceInstanceName``). ``_make_vm_name``
    produces it, and the constructor re-asserts the coercion output is valid --
    so a future regression in that coercion fails fast here rather than at the
    Azure API. Azure has no per-VM label-value restriction (tags accept nearly
    any string), so there is no ``GceLabelValue`` analog.
    """

    def __new__(cls, value: str) -> Self:
        candidate = value.strip()
        if not _AZURE_VM_NAME_RE.match(candidate) or len(candidate) > _MAX_VM_NAME_LENGTH:
            raise InvalidAzureIdentifierError(
                f"{candidate!r} is not a valid Azure VM name "
                f"([a-z0-9-], no leading/trailing dash, at most {_MAX_VM_NAME_LENGTH} chars)"
            )
        return super().__new__(cls, candidate)


class LinuxHostname(NonEmptyStr):
    """A Linux computer-name: non-empty, ``[a-z0-9-]``, no leading/trailing dash, at most 63 chars.

    Codifies the hostname charset and the <= 63 char cap for the value written to
    a VM's ``os_profile.computer_name``. ``_computer_name`` derives it from an
    ``AzureVmName`` (truncating to length), and the constructor re-asserts the
    result is a valid hostname -- so a regression in that derivation fails fast
    here rather than as an opaque Azure API error.
    """

    def __new__(cls, value: str) -> Self:
        candidate = value.strip()
        if not _AZURE_VM_NAME_RE.match(candidate) or len(candidate) > _MAX_LINUX_HOSTNAME_LENGTH:
            raise InvalidAzureIdentifierError(
                f"{candidate!r} is not a valid Linux hostname "
                f"([a-z0-9-], no leading/trailing dash, at most {_MAX_LINUX_HOSTNAME_LENGTH} chars)"
            )
        return super().__new__(cls, candidate)


# How long to wait for a resource-provider registration to flip to "Registered".
_PROVIDER_REGISTRATION_TIMEOUT_SECONDS: Final[float] = 180.0
_PROVIDER_REGISTRATION_POLL_SECONDS: Final[float] = 3.0

# Minimum age before an unattached NIC / public IP is eligible for reclaim by GC.
# Every healthy create leaves its NIC briefly unattached (between creating it and
# the VM associating it), so the age gate is what keeps the sweep from racing an
# in-flight create -- whether that create is in this process, on this machine, or
# on another machine sharing the resource group. The window sits above Azure's
# 180s post-failure NIC reservation, so a younger orphan's delete would fail anyway.
_ORPHAN_RECLAIM_MIN_AGE_SECONDS: Final[float] = 240.0


def _make_vm_name(label: str, tags: Mapping[str, str]) -> AzureVmName:
    """Build a unique, Azure-valid VM resource name from the label and tags.

    Azure identifies a VM by name within its resource group (used for every
    get/delete/instance-view call), so the name must be valid and unique. The
    human-readable ``label`` stem is sanitized to lowercase alphanumerics +
    dashes, and a 32-hex host-id suffix (from the ``mngr-host-id`` tag) is
    appended for uniqueness; absent that tag (direct-client use), a fresh uuid
    is used instead.
    """
    stem = _INVALID_NAME_CHARS_RE.sub("-", label.lower()).strip("-")[:_VM_NAME_STEM_LENGTH].strip("-")
    if not stem:
        stem = "mngr"
    host_id = tags.get("mngr-host-id", "")
    suffix = host_id.lower().rsplit("-", 1)[-1] if host_id else uuid4().hex
    suffix = _INVALID_NAME_CHARS_RE.sub("", suffix)
    return AzureVmName(f"{stem}-{suffix}"[:_MAX_VM_NAME_LENGTH].rstrip("-"))


def _computer_name(vm_name: AzureVmName) -> LinuxHostname:
    """Derive a Linux computer-name (hostname) from the VM name.

    The input is an ``AzureVmName`` (charset ``[a-z0-9-]``, no edge dashes) -- a
    subset of valid hostnames -- so truncation to the 63-char cap (then stripping
    any trailing dash that truncation exposes) is the only step needed to keep the
    result a valid hostname.
    """
    return LinuxHostname(vm_name[:_MAX_LINUX_HOSTNAME_LENGTH].rstrip("-"))


class AzureNetworkPrepareResult(FrozenModel):
    """Outcome of ``AzureVpsClient.ensure_network`` / ``mngr azure prepare``."""

    resource_group: str = Field(description="Name of the mngr-owned resource group holding the prepared network")
    region: str = Field(description="Azure region the resource group / network was prepared in")
    was_created: bool = Field(
        description=(
            "True if the resource group did not exist before this call (a first-run create); False on an "
            "idempotent re-run where it already existed. Tracked at the resource-group (top-level "
            "container) granularity -- the NSG / vnet / subnet are create_or_update'd within it regardless."
        )
    )


class AzureVpsClient(VpsClientInterface):
    """Azure VM client implementing the VPS provider interface via the azure-mgmt SDK.

    Bound at construction to a single ``subscription_id`` + ``region`` +
    ``resource_group`` (analogous to ``AwsVpsClient`` being bound to a region).
    To target a different region, instantiate a separate client.

    The one-off infrastructure (resource group, vnet, subnet, NSG) is created by
    ``ensure_network`` (the privileged ``mngr azure prepare`` path); the hot
    ``create_instance`` path only looks that infrastructure up
    (``resolve_subnet_id``), so it needs VM/NIC/IP-create permissions but none of
    the network-management permissions ``ensure_network`` uses.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Azure VM provisioning (begin_create_or_update blocks until the VM is fully
    # running) routinely takes 60-120s, slower than EC2; raise the warning
    # threshold so normal boots don't log a "slow" warning.
    slow_provisioning_warning_threshold_seconds: float = Field(default=180.0)

    # Typed ``Any`` because azure.core.credentials.TokenCredential is a Protocol,
    # which pydantic's arbitrary_types_allowed validation cannot accept.
    credential: Any = Field(frozen=True, description="DefaultAzureCredential (or compatible) for the mgmt clients")
    subscription_id: str = Field(frozen=True, description="Azure subscription this client targets")
    region: str = Field(frozen=True, description="Azure region / location this client targets")
    resource_group: str = Field(default="mngr", description="Resource group holding all mngr infrastructure and VMs")
    vnet_name: str = Field(default="mngr-vnet", description="Virtual network name")
    subnet_name: str = Field(default="mngr-subnet", description="Subnet name")
    nsg_name: str = Field(default="mngr-nsg", description="Network security group name")
    vnet_address_prefix: str = Field(default="10.0.0.0/16", description="vnet CIDR address space")
    subnet_address_prefix: str = Field(default="10.0.0.0/24", description="subnet CIDR address range")
    vm_size: str = Field(default="Standard_B2s", description="Default VM size for instances created via this client")
    image_publisher: str = Field(default=DEFAULT_IMAGE_PUBLISHER, description="Marketplace image publisher")
    image_offer: str = Field(default=DEFAULT_IMAGE_OFFER, description="Marketplace image offer")
    image_sku: str = Field(default=DEFAULT_IMAGE_SKU, description="Marketplace image SKU")
    image_version: str = Field(default=DEFAULT_IMAGE_VERSION, description="Marketplace image version")
    admin_username: str = Field(default="azureuser", description="Admin user the SSH public key is attached to")
    os_disk_size_gb: int = Field(default=30, description="OS managed-disk size in GB")
    os_disk_type: str = Field(default="StandardSSD_LRS", description="OS managed-disk storage account type")
    allowed_ssh_cidrs: tuple[str, ...] = Field(
        default=("0.0.0.0/0",),
        description=(
            "CIDR blocks allowed inbound on tcp/22 and tcp/container_ssh_port of the NSG created by "
            "ensure_network. Default ('0.0.0.0/0',) allows any IP; set to e.g. ('203.0.113.4/32',) to "
            "restrict to your own IP, or () for no SSH allow rule (the NSG default-deny then leaves "
            "instances unreachable from outside the vnet). A warning is logged when the effective range "
            "is 0.0.0.0/0 or empty."
        ),
    )
    associate_public_ip: bool = Field(default=True, description="Assign a public IPv4 to launched VMs")
    container_ssh_port: int = Field(
        default=2222, description="Port the container's sshd is exposed on (added to the NSG)"
    )

    # There is no per-key Azure resource (unlike an EC2 KeyPair); the public key
    # lives only in per-VM os_profile config. This in-memory map bridges the base
    # flow's upload_ssh_key -> create_instance(ssh_key_ids=[...]) handoff within a
    # single process. A later fresh-process delete is a tolerant no-op.
    _ssh_public_keys_by_id: dict[str, str] = PrivateAttr(default_factory=dict)
    _cached_compute_client: Any = PrivateAttr(default=None)
    _cached_network_client: Any = PrivateAttr(default=None)
    _cached_resource_client: Any = PrivateAttr(default=None)
    _cached_authorization_client: Any = PrivateAttr(default=None)

    # =========================================================================
    # Management clients
    #
    # Built lazily and cached on first use. Defined as methods (not inlined) so
    # the test-only subclass in testing.py can override them to inject fakes --
    # the same seam as the aws/gcp provider clients.
    # =========================================================================

    def _compute(self) -> Any:
        if self._cached_compute_client is None:
            self._cached_compute_client = ComputeManagementClient(self.credential, self.subscription_id)
        return self._cached_compute_client

    def _network(self) -> Any:
        if self._cached_network_client is None:
            self._cached_network_client = NetworkManagementClient(self.credential, self.subscription_id)
        return self._cached_network_client

    def _resource(self) -> Any:
        if self._cached_resource_client is None:
            self._cached_resource_client = ResourceManagementClient(self.credential, self.subscription_id)
        return self._cached_resource_client

    def _authorization(self) -> Any:
        if self._cached_authorization_client is None:
            self._cached_authorization_client = AuthorizationManagementClient(self.credential, self.subscription_id)
        return self._cached_authorization_client

    @contextmanager
    def _translate_azure_errors(self) -> Iterator[None]:
        """Translate ``azure.core.exceptions.HttpResponseError`` into ``VpsApiError``.

        ``ResourceNotFoundError`` and ``ClientAuthenticationError`` both subclass
        ``HttpResponseError`` and carry a ``status_code``, so this single handler
        covers not-found (404) and auth (401) failures too.
        """
        try:
            yield
        except HttpResponseError as e:
            status_code = e.status_code if isinstance(e.status_code, int) else 0
            raise VpsApiError(status_code, e.message or str(e)) from e

    def _base_tags(self) -> dict[str, str]:
        return {AZURE_MANAGED_BY_TAG_KEY: AZURE_MANAGED_BY_TAG_VALUE}

    # =========================================================================
    # Network management (idempotent; the privileged `mngr azure prepare` path)
    # =========================================================================

    def _register_resource_providers(self) -> None:
        """Register the Compute/Network/Storage resource providers and wait until ready.

        New subscriptions often start with these unregistered. Registration is
        asynchronous, so each is kicked off and then polled until its
        ``registration_state`` is ``Registered`` (already-registered providers
        return immediately). Bounded by ``_PROVIDER_REGISTRATION_TIMEOUT_SECONDS``.
        """
        for namespace in _REQUIRED_RESOURCE_PROVIDERS:
            with self._translate_azure_errors():
                provider = self._resource().providers.get(namespace)
            if provider.registration_state == "Registered":
                continue
            logger.info("Registering Azure resource provider {} ...", namespace)
            with self._translate_azure_errors():
                self._resource().providers.register(namespace)
            self._wait_for_provider_registered(namespace)

    def _is_provider_registered(self, namespace: str) -> bool:
        with self._translate_azure_errors():
            provider = self._resource().providers.get(namespace)
        return bool(provider.registration_state == "Registered")

    def _wait_for_provider_registered(self, namespace: str) -> None:
        if not poll_until(
            lambda: self._is_provider_registered(namespace),
            timeout=_PROVIDER_REGISTRATION_TIMEOUT_SECONDS,
            poll_interval=_PROVIDER_REGISTRATION_POLL_SECONDS,
        ):
            raise MngrError(
                f"Azure resource provider {namespace!r} did not reach 'Registered' within "
                f"{_PROVIDER_REGISTRATION_TIMEOUT_SECONDS:.0f}s. Register it manually with "
                f"`az provider register --namespace {namespace}` and retry `mngr azure prepare`."
            )

    def ensure_network(self) -> AzureNetworkPrepareResult:
        """Create the mngr resource group + vnet/subnet/NSG if absent. Returns a prepare result.

        Idempotent (``create_or_update`` throughout). Registers the required
        resource providers first, then creates the resource group (tagged
        ``managed-by=mngr`` so ``cleanup`` can prove ownership), the NSG (opening
        tcp/22 + tcp/``container_ssh_port`` to ``allowed_ssh_cidrs``), and the
        vnet whose subnet references that NSG.

        Fails open (mirrors ``AwsVpsClient.ensure_security_group`` /
        ``GcpVpsClient.ensure_firewall``): the default ``allowed_ssh_cidrs`` of
        ('0.0.0.0/0',) creates a world-open allow rule and is logged as a
        warning. An empty ``allowed_ssh_cidrs`` creates the NSG with no SSH allow
        rule at all -- the NSG's implicit default-deny inbound then leaves
        instances unreachable from outside the vnet, the analog of AWS's
        zero-ingress security group; this is also warned. This is the privileged
        write path used by ``mngr azure prepare``; the hot path in
        ``create_instance`` uses ``resolve_subnet_id`` instead (lookup-only).
        """
        self._warn_about_cidrs_if_needed()
        self._register_resource_providers()
        with self._translate_azure_errors():
            # Check existence before the (idempotent) create so the CLI can
            # report a first-run create vs an idempotent re-run. One cheap GET on
            # the one-time prepare path.
            already_existed = self._resource().resource_groups.check_existence(self.resource_group)
            self._resource().resource_groups.create_or_update(
                self.resource_group,
                ResourceGroup(location=self.region, tags=self._base_tags()),
            )
        nsg_id = self._ensure_nsg()
        self._ensure_vnet_and_subnet(nsg_id)
        logger.info(
            "Ensured Azure resource group {} (vnet {}, subnet {}, nsg {}) in region {}",
            self.resource_group,
            self.vnet_name,
            self.subnet_name,
            self.nsg_name,
            self.region,
        )
        return AzureNetworkPrepareResult(
            resource_group=self.resource_group, region=self.region, was_created=not already_existed
        )

    def _warn_about_cidrs_if_needed(self) -> None:
        """Emit a one-line warning when the effective CIDR set is empty or 0.0.0.0/0.

        The two cases need different wording: empty means "no usable ingress"
        (the NSG is created with no SSH allow rule, so its default-deny leaves
        the instance unreachable from outside the vnet), whereas 0.0.0.0/0 means
        "open to the internet" (default but worth flagging). Anything else is
        silent. Mirrors ``AwsVpsClient._warn_about_cidrs_if_needed`` /
        ``GcpVpsClient._warn_about_cidrs_if_needed``.
        """
        if not self.allowed_ssh_cidrs:
            logger.warning(
                "Azure allowed_ssh_cidrs is empty; NSG {!r} will be created with no SSH allow rule and "
                "instances will be unreachable from outside the vnet (default-deny) unless another rule "
                "grants ingress. Set allowed_ssh_cidrs on the provider config (e.g. ('203.0.113.4/32',)) to fix.",
                self.nsg_name,
            )
            return
        if "0.0.0.0/0" in self.allowed_ssh_cidrs:
            logger.warning(
                "Azure allowed_ssh_cidrs includes 0.0.0.0/0; NSG {!r} will permit SSH from the public internet.",
                self.nsg_name,
            )

    def _ensure_nsg(self) -> str:
        """Create / update the NSG opening SSH ingress. Returns its resource id.

        With an empty ``allowed_ssh_cidrs`` no allow rule is added: the NSG is
        created with only Azure's implicit rules (default-deny inbound), so the
        instance is unreachable from outside the vnet -- the analog of AWS's
        zero-ingress security group. (An Azure ``SecurityRule`` with an empty
        ``source_address_prefixes`` is rejected by the API, so "no ingress" must
        be expressed as the absence of the rule, not an empty-source rule.)
        """
        security_rules: list[Any] = []
        priority = 1000
        for port in ("22", str(self.container_ssh_port)) if self.allowed_ssh_cidrs else ():
            security_rules.append(
                network_models.SecurityRule(
                    name=f"mngr-allow-tcp-{port}",
                    protocol="Tcp",
                    source_port_range="*",
                    destination_port_range=port,
                    source_address_prefixes=list(self.allowed_ssh_cidrs),
                    destination_address_prefix="*",
                    access="Allow",
                    priority=priority,
                    direction="Inbound",
                )
            )
            priority += 10
        nsg = network_models.NetworkSecurityGroup(
            location=self.region, security_rules=security_rules, tags=self._base_tags()
        )
        with self._translate_azure_errors():
            poller = self._network().network_security_groups.begin_create_or_update(
                self.resource_group, self.nsg_name, nsg
            )
            created_nsg = poller.result()
        return created_nsg.id

    def _ensure_vnet_and_subnet(self, nsg_id: str) -> None:
        """Create / update the vnet with a single subnet that references the NSG."""
        vnet = network_models.VirtualNetwork(
            location=self.region,
            address_space=network_models.AddressSpace(address_prefixes=[self.vnet_address_prefix]),
            subnets=[
                network_models.Subnet(
                    name=self.subnet_name,
                    address_prefix=self.subnet_address_prefix,
                    network_security_group=network_models.NetworkSecurityGroup(id=nsg_id),
                )
            ],
            tags=self._base_tags(),
        )
        with self._translate_azure_errors():
            self._network().virtual_networks.begin_create_or_update(self.resource_group, self.vnet_name, vnet).result()

    def resolve_subnet_id(self) -> str:
        """Look up the prepared subnet id without creating anything. Returns the id.

        Mirrors ``ensure_network`` but with no write API calls -- the hot
        ``create_instance`` path needs only VM/NIC/IP-create permissions. When
        the subnet is missing, raises a ``MngrError`` pointing at
        ``mngr azure prepare`` so a user with a restricted role gets a clear next
        step rather than an opaque permission/not-found error.
        """
        try:
            with self._translate_azure_errors():
                subnet = self._network().subnets.get(self.resource_group, self.vnet_name, self.subnet_name)
        except VpsApiError as e:
            if e.status_code == 404:
                raise MngrError(
                    f"Azure subnet {self.subnet_name!r} (vnet {self.vnet_name!r}, resource group "
                    f"{self.resource_group!r}) does not exist in region {self.region!r}. "
                    "Run `mngr azure prepare` once to create it, then retry the create."
                ) from e
            raise
        return subnet.id

    # =========================================================================
    # Instance Operations
    # =========================================================================

    def create_instance(
        self,
        label: str,
        region: str,
        plan: str,
        user_data: str,
        ssh_key_ids: Sequence[str],
        tags: Mapping[str, str],
        spot: bool = False,
    ) -> VpsInstanceId:
        """Provision an Azure VM (public IP + NIC + VM) in the client's region.

        ``plan`` is the Azure VM size (e.g. ``Standard_B2s``). The public IP and
        NIC are created with ``delete_option=Delete`` and the OS disk with
        ``delete_option=Delete``, so deleting the VM later cascades all four
        resources -- ``destroy_instance`` only deletes the VM.

        When ``spot`` is True (from the presence-only ``--azure-spot`` build
        arg), the VM is launched with ``priority=Spot``, ``eviction_policy=Delete``
        and ``max_price=-1`` (pay up to on-demand; evicted only on capacity, and
        *deleted* not stopped on eviction -- matching AWS spot's terminate-on-
        reclaim semantics). ``spot`` is Azure-specific: it widens this method's
        signature beyond the shared ``VpsClientInterface.create_instance``
        contract, so providers reach it via ``self.azure_client.create_instance``.

        The VM is given a system-assigned managed identity (used by the in-VM idle
        watcher to deallocate itself). Offline ``host_dir`` needs no VM identity --
        it is captured operator-side at ``mngr stop``.
        """
        if region != self.region:
            raise VpsApiError(
                400,
                f"Cross-region create not supported: client bound to {self.region!r}, "
                f"got region={region!r}. Instantiate a region-specific client.",
            )

        subnet_id = self.resolve_subnet_id()
        vm_name = _make_vm_name(label, tags)
        public_key = self._resolve_ssh_public_key(ssh_key_ids)

        vm_tags: dict[str, str] = {**self._base_tags(), **dict(tags)}
        vm_tags["mngr-created-at"] = datetime.now(timezone.utc).isoformat()
        # Host name as a tag so a deallocated VM still resolves by name in offline
        # discovery (``label`` is ``mngr-<host_name>``; the read side strips it).
        vm_tags[HOST_NAME_TAG_KEY] = label
        # Mark VMs launched during pytest so the conftest session-end orphan
        # scanner can identify and force-delete any leaks without having to
        # constrain the agent / host name shape.
        if "PYTEST_CURRENT_TEST" in os.environ:
            vm_tags[AZURE_PYTEST_LAUNCHED_TAG] = "true"

        nic_id = self._create_public_ip_and_nic(vm_name, subnet_id, vm_tags)
        vm_model = self._build_vm_model(vm_name, plan, user_data, public_key, nic_id, vm_tags, spot)

        # The public IP + NIC are created before the VM. If VM creation fails
        # (e.g. SkuNotAvailable / quota), this method raises before returning an
        # instance id, so the create_host failure-cleanup -- which deletes the VM
        # by id -- can never reach the orphaned NIC + public IP. Delete them here
        # so a failed create leaks nothing. On success the VM owns them and
        # destroy_instance's delete cascades them via delete_option=Delete.
        vm_created = False
        try:
            with self._translate_azure_errors():
                self._compute().virtual_machines.begin_create_or_update(
                    self.resource_group, vm_name, vm_model
                ).result()
            vm_created = True
        finally:
            if not vm_created:
                self._delete_nic_and_public_ip(vm_name)
        logger.info(
            "Created Azure VM {} (label: {}, region: {}, size: {}, image: {}:{}:{})",
            vm_name,
            label,
            region,
            plan,
            self.image_publisher,
            self.image_offer,
            self.image_sku,
        )
        return VpsInstanceId(vm_name)

    def _resolve_ssh_public_key(self, ssh_key_ids: Sequence[str]) -> str:
        """Return the single public key to inject, from the in-memory upload map.

        Azure keeps SSH keys only in per-VM os_profile config (no provider key
        resource), so the key must have been stashed in this process by a prior
        ``upload_ssh_key`` call. A missing id means the caller broke that
        in-process handoff; surface a typed error rather than a bare KeyError.
        """
        if not ssh_key_ids:
            raise VpsApiError(400, "create_instance requires at least one ssh_key_id for the admin user")
        key_id = ssh_key_ids[0]
        public_key = self._ssh_public_keys_by_id.get(key_id)
        if public_key is None:
            raise VpsApiError(
                400,
                f"No in-memory SSH public key for id {key_id!r}; upload_ssh_key must be called "
                "in the same process before create_instance (Azure keeps keys only in per-VM "
                "config, not as a provider resource).",
            )
        return public_key

    def _create_public_ip_and_nic(self, vm_name: str, subnet_id: str, vm_tags: Mapping[str, str]) -> str:
        """Create the per-VM public IP and NIC. Returns the NIC resource id.

        The public IP is referenced from the NIC ip-config with
        ``delete_option=Delete`` so deleting the NIC reaps the IP; the NIC itself
        is reaped when the VM is deleted (the VM's network profile sets the NIC's
        ``delete_option=Delete``).
        """
        ip_config = network_models.NetworkInterfaceIPConfiguration(
            name="ipconfig1", subnet=network_models.Subnet(id=subnet_id)
        )
        if self.associate_public_ip:
            public_ip = network_models.PublicIPAddress(
                location=self.region,
                sku=network_models.PublicIPAddressSku(name="Standard"),
                public_ip_allocation_method="Static",
                public_ip_address_version="IPV4",
                tags=dict(vm_tags),
            )
            with self._translate_azure_errors():
                created_ip = (
                    self._network()
                    .public_ip_addresses.begin_create_or_update(
                        self.resource_group, self._public_ip_name(vm_name), public_ip
                    )
                    .result()
                )
            ip_config.public_ip_address = network_models.PublicIPAddress(id=created_ip.id, delete_option="Delete")
        nic = network_models.NetworkInterface(location=self.region, ip_configurations=[ip_config], tags=dict(vm_tags))
        with self._translate_azure_errors():
            created_nic = (
                self._network()
                .network_interfaces.begin_create_or_update(self.resource_group, self._nic_name(vm_name), nic)
                .result()
            )
        return created_nic.id

    def _build_vm_model(
        self,
        vm_name: AzureVmName,
        plan: str,
        user_data: str,
        public_key: str,
        nic_id: str,
        vm_tags: Mapping[str, str],
        spot: bool,
    ) -> Any:
        # azure-mgmt-compute 38.x "flattens" the per-VM sub-profiles into
        # ``properties`` at runtime, but its typed __init__ overload only exposes
        # ``properties=...`` (not the flattened names), so the nested
        # ``VirtualMachineProperties`` is constructed explicitly to stay type-clean.
        properties = compute_models.VirtualMachineProperties(
            hardware_profile=compute_models.HardwareProfile(vm_size=plan),
            storage_profile=compute_models.StorageProfile(
                image_reference=compute_models.ImageReference(
                    publisher=self.image_publisher,
                    offer=self.image_offer,
                    sku=self.image_sku,
                    version=self.image_version,
                ),
                os_disk=compute_models.OSDisk(
                    create_option="FromImage",
                    delete_option="Delete",
                    disk_size_gb=self.os_disk_size_gb,
                    managed_disk=compute_models.ManagedDiskParameters(storage_account_type=self.os_disk_type),
                ),
            ),
            os_profile=compute_models.OSProfile(
                computer_name=_computer_name(vm_name),
                admin_username=self.admin_username,
                linux_configuration=compute_models.LinuxConfiguration(
                    disable_password_authentication=True,
                    ssh=compute_models.SshConfiguration(
                        public_keys=[
                            compute_models.SshPublicKey(
                                path=f"/home/{self.admin_username}/.ssh/authorized_keys", key_data=public_key
                            )
                        ]
                    ),
                ),
                # Azure requires custom_data base64-encoded; the cloud-init Azure
                # datasource decodes it and runs it as user-data on first boot.
                custom_data=base64.b64encode(user_data.encode("utf-8")).decode("ascii"),
            ),
            network_profile=compute_models.NetworkProfile(
                network_interfaces=[
                    compute_models.NetworkInterfaceReference(
                        id=nic_id,
                        properties=compute_models.NetworkInterfaceReferenceProperties(
                            primary=True, delete_option="Delete"
                        ),
                    )
                ]
            ),
        )
        if spot:
            properties.priority = "Spot"
            properties.eviction_policy = "Delete"
            properties.billing_profile = compute_models.BillingProfile(max_price=-1.0)
        # System-assigned managed identity: the in-VM idle watcher uses its IMDS
        # token to deallocate this VM itself (see AzureProvider's idle watcher). A
        # per-VM role assignment (assign_self_deallocate_role) grants it the
        # least-privilege deallocate action scoped to just this VM. Offline
        # host_dir needs no VM identity -- it is captured operator-side at stop.
        return compute_models.VirtualMachine(
            location=self.region,
            tags=dict(vm_tags),
            properties=properties,
            identity=compute_models.VirtualMachineIdentity(type="SystemAssigned"),
        )

    def _public_ip_name(self, vm_name: str) -> str:
        return f"{vm_name}-ip"

    def _nic_name(self, vm_name: str) -> str:
        return f"{vm_name}-nic"

    def _delete_nic_and_public_ip(self, vm_name: str) -> None:
        """Best-effort delete the per-VM NIC then its public IP (NIC must go first).

        Used to reclaim the NIC + public IP that ``create_instance`` provisions
        before the VM when VM creation fails. The NIC references the public IP, so
        it must be deleted first; each delete is best-effort (a 404 / already-gone
        is fine) so cleanup never masks the original create failure.

        When VM creation failed for *capacity* reasons (``SkuNotAvailable``),
        Azure reserves the NIC for the would-be VM for 180s, so the delete here
        raises ``NicReservedForAnotherVm``. That is expected, not an error:
        ``reclaim_orphaned_network_resources`` reaps it at GC time once the
        reservation expires, so it is logged at info, not warning.
        """
        try:
            with self._translate_azure_errors():
                self._network().network_interfaces.begin_delete(self.resource_group, self._nic_name(vm_name)).result()
        except VpsApiError as e:
            self._log_orphan_cleanup_failure("NIC", vm_name, e)
        try:
            with self._translate_azure_errors():
                self._network().public_ip_addresses.begin_delete(
                    self.resource_group, self._public_ip_name(vm_name)
                ).result()
        except VpsApiError as e:
            self._log_orphan_cleanup_failure("public IP", vm_name, e)

    def _log_orphan_cleanup_failure(self, kind: str, vm_name: str, error: VpsApiError) -> None:
        if "NicReservedForAnotherVm" in str(error) or "PublicIPAddressCannotBeDeleted" in str(error):
            logger.info(
                "Orphaned {} for {} is still reserved by the failed VM; it will be reclaimed by the next gc.",
                kind,
                vm_name,
            )
        else:
            logger.warning("Failed to delete orphaned {} for {}: {}", kind, vm_name, error)

    def _is_reclaimable_orphan(self, resource: Any, cutoff: datetime) -> bool:
        """True if ``resource`` is an mngr-tagged NIC/IP created before ``cutoff``.

        The age gate ensures the sweep never deletes a NIC/IP belonging to a
        concurrent create that is still mid-flight (its NIC is briefly unattached
        before the VM associates it).
        """
        tags = dict(resource.tags or {})
        if tags.get("mngr-provider") is None:
            return False
        created_raw = tags.get("mngr-created-at")
        if created_raw is None:
            return False
        try:
            created_at = datetime.fromisoformat(created_raw)
        except ValueError:
            return False
        return created_at < cutoff

    def reclaim_orphaned_network_resources(
        self, provider_name: ProviderInstanceName, dry_run: bool = False
    ) -> list[ProviderResourceInfo]:
        """Best-effort delete unattached, aged mngr NICs / public IPs from failed creates.

        ``create_instance`` provisions a public IP + NIC before the VM; a VM
        create that fails (capacity / quota) leaves them orphaned, and Azure
        reserves the NIC for the would-be VM for 180s so immediate cleanup is
        blocked. Rather than block a failed create for ~3 minutes, these are
        reclaimed here at GC time (``mngr gc``, which also runs after every
        ``mngr destroy``). Only resources older than the reservation window
        (``_ORPHAN_RECLAIM_MIN_AGE_SECONDS``) are touched, so an in-flight
        concurrent create is never disturbed. The whole sweep is best-effort: a
        list / delete failure is logged and never aborts the surrounding GC.

        Returns the resources reclaimed (or, when ``dry_run``, that would be).
        """
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=_ORPHAN_RECLAIM_MIN_AGE_SECONDS)
        reclaimed: list[ProviderResourceInfo] = []
        # NICs first -- a public IP attached to a NIC cannot be deleted.
        try:
            with self._translate_azure_errors():
                nics = list(self._network().network_interfaces.list(self.resource_group))
        except VpsApiError as e:
            logger.debug("Skipping orphan NIC reclaim (list failed): {}", e)
            nics = []
        for nic in nics:
            if nic.virtual_machine is not None or not self._is_reclaimable_orphan(nic, cutoff):
                continue
            info = ProviderResourceInfo(provider_name=provider_name, kind="network_interface", name=nic.name)
            if dry_run:
                reclaimed.append(info)
                continue
            try:
                with self._translate_azure_errors():
                    self._network().network_interfaces.begin_delete(self.resource_group, nic.name).result()
                logger.info("Reclaimed orphaned NIC {}", nic.name)
                reclaimed.append(info)
            except VpsApiError as e:
                logger.warning("Failed to reclaim orphaned NIC {}: {}", nic.name, e)
        # Then unattached public IPs (their NIC is gone, or never had one).
        try:
            with self._translate_azure_errors():
                public_ips = list(self._network().public_ip_addresses.list(self.resource_group))
        except VpsApiError as e:
            logger.debug("Skipping orphan public-IP reclaim (list failed): {}", e)
            public_ips = []
        for public_ip in public_ips:
            if public_ip.ip_configuration is not None or not self._is_reclaimable_orphan(public_ip, cutoff):
                continue
            info = ProviderResourceInfo(provider_name=provider_name, kind="public_ip", name=public_ip.name)
            if dry_run:
                reclaimed.append(info)
                continue
            try:
                with self._translate_azure_errors():
                    self._network().public_ip_addresses.begin_delete(self.resource_group, public_ip.name).result()
                logger.info("Reclaimed orphaned public IP {}", public_ip.name)
                reclaimed.append(info)
            except VpsApiError as e:
                logger.warning("Failed to reclaim orphaned public IP {}: {}", public_ip.name, e)
        return reclaimed

    def destroy_instance(self, instance_id: VpsInstanceId) -> None:
        try:
            with self._translate_azure_errors():
                self._compute().virtual_machines.begin_delete(self.resource_group, str(instance_id)).result()
        except VpsApiError as e:
            # Already gone (deleted by a prior call) -- destroy is idempotent.
            if e.status_code != 404:
                raise
            logger.info("Azure VM {} already gone; treating destroy as success", instance_id)
            return
        logger.info("Deleted Azure VM {} (NIC, public IP and OS disk cascade via delete_option)", instance_id)

    def set_instance_tags(self, instance_id: VpsInstanceId, tags: Mapping[str, str]) -> None:
        """Upsert ``tags`` on an existing VM, preserving its other tags.

        Azure's VM update replaces the whole tags dict, so read the current tags,
        merge in the new ones, then write back. Widens ``AzureVpsClient`` beyond the
        shared ``VpsClientInterface``; ``AzureProvider`` reaches it via ``self.azure_client``.
        """
        with self._translate_azure_errors():
            vm = self._compute().virtual_machines.get(self.resource_group, str(instance_id))
            merged = dict(vm.tags or {})
            merged.update(tags)
            self._compute().virtual_machines.begin_update(
                self.resource_group, str(instance_id), compute_models.VirtualMachineUpdate(tags=merged)
            ).result()

    def _await_long_running_operation(self, poller: LROPoller[None], timeout_seconds: float, description: str) -> None:
        """Block on an Azure long-running operation up to ``timeout_seconds``.

        ``LROPoller.wait(timeout)`` re-raises the operation's own error (translated
        to ``VpsApiError`` by the surrounding ``_translate_azure_errors``) but does
        NOT raise when the timeout merely elapses -- it returns with the poll still
        in flight. So we check ``done()`` afterward and surface a clear
        ``VpsProvisioningError``, matching the AWS/GCP clients' wait contract (the
        operation itself keeps running server-side).
        """
        poller.wait(timeout_seconds)
        if not poller.done():
            raise VpsProvisioningError(f"Azure operation did not finish within {timeout_seconds}s: {description}")

    def deallocate_instance(self, instance_id: VpsInstanceId, timeout_seconds: float = 300.0) -> None:
        """Deallocate (not delete) an Azure VM, halting compute billing; the OS disk persists.

        Critically distinct from an OS-level shutdown, which only powers the VM
        off ("Stopped (not deallocated)") and STILL bills compute -- only a
        ``deallocate`` halts compute billing. The OS disk (and all on-disk state)
        survives, so ``start_instance`` resumes it. The ``begin_deallocate``
        long-running operation is bounded by ``timeout_seconds`` (re-raised as ``VpsProvisioningError`` on
        exceedance, matching the AWS/GCP clients' wait contract). Idempotent.

        Widens ``AzureVpsClient`` beyond the shared ``VpsClientInterface`` (which
        has no stop/start); ``AzureProvider`` reaches it via ``self.azure_client``.
        """
        with self._translate_azure_errors():
            poller = self._compute().virtual_machines.begin_deallocate(self.resource_group, str(instance_id))
            self._await_long_running_operation(poller, timeout_seconds, f"deallocate VM {instance_id}")
        logger.info("Deallocated Azure VM {} (compute billing halted; OS disk preserved)", instance_id)

    def start_instance(self, instance_id: VpsInstanceId, timeout_seconds: float = 300.0) -> str:
        """Start a deallocated Azure VM and return its public IP.

        The public IP is allocated ``Static`` (see ``_create_public_ip_and_nic``),
        so it is PRESERVED across deallocate/start -- the returned IP equals the
        pre-stop address. (This is why ``AzureProvider.start_host`` needs no
        known_hosts rebind, unlike AWS/GCP whose ephemeral IPs change.) The
        ``begin_start`` long-running operation is bounded by ``timeout_seconds`` (re-raised as
        ``VpsProvisioningError`` on exceedance). Idempotent.

        Azure-only, like ``deallocate_instance`` -- reached via ``self.azure_client``.
        """
        with self._translate_azure_errors():
            poller = self._compute().virtual_machines.begin_start(self.resource_group, str(instance_id))
            self._await_long_running_operation(poller, timeout_seconds, f"start VM {instance_id}")
        logger.info("Started Azure VM {}", instance_id)
        return self.get_instance_ip(instance_id)

    def _vm_resource_id(self, vm_name: str) -> str:
        """Full ARM resource id of a VM in this client's subscription + resource group."""
        return (
            f"/subscriptions/{self.subscription_id}/resourceGroups/{self.resource_group}"
            f"/providers/Microsoft.Compute/virtualMachines/{vm_name}"
        )

    def ensure_self_deallocate_role(self) -> str | None:
        """Create the least-privilege custom role that lets a VM deallocate itself. Best-effort.

        Idempotent (deterministic role-definition GUID). Returns the role
        definition's resource id, or ``None`` when the operator lacks
        ``Microsoft.Authorization/roleDefinitions/write`` (Owner / User Access
        Administrator): in that case idle self-deallocate is disabled and only
        ``mngr stop``/``start`` will halt billing (an in-VM OS ``shutdown`` does not,
        on Azure). The missing privilege is logged at WARNING and swallowed rather
        than failing ``mngr azure prepare``. See specs/gcp-azure-stop-start-lifecycle.
        """
        subscription_scope = f"/subscriptions/{self.subscription_id}"
        role_definition = RoleDefinition(
            role_name=SELF_DEALLOCATE_ROLE_NAME,
            description="Lets a mngr-managed VM's identity deallocate itself (idle self-stop).",
            role_type="CustomRole",
            permissions=[
                Permission(
                    actions=[
                        "Microsoft.Compute/virtualMachines/deallocate/action",
                        "Microsoft.Compute/virtualMachines/read",
                    ]
                )
            ],
            assignable_scopes=[subscription_scope],
        )
        try:
            with self._translate_azure_errors():
                created = self._authorization().role_definitions.create_or_update(
                    scope=subscription_scope,
                    role_definition_id=SELF_DEALLOCATE_ROLE_ID,
                    role_definition=role_definition,
                )
        except VpsApiError as e:
            if self._is_authorization_error(e):
                logger.warning(
                    "Could not create the {!r} custom role (needs Microsoft.Authorization/roleDefinitions/write -- "
                    "Owner or User Access Administrator). Idle self-deallocate is disabled; only `mngr stop`/`start` "
                    "will halt billing (an in-VM OS shutdown does not, on Azure). ({})",
                    SELF_DEALLOCATE_ROLE_NAME,
                    e,
                )
                return None
            raise
        logger.info("Ensured custom role {!r} in subscription {}", SELF_DEALLOCATE_ROLE_NAME, self.subscription_id)
        return created.id

    def assign_self_deallocate_role(self, vm_name: str) -> bool:
        """Assign the self-deallocate role to a VM's identity, scoped to that VM. Best-effort.

        Returns True on success (or when the assignment already exists). Returns
        False -- after a single clear WARNING -- when the VM has no system-assigned
        identity principal yet, or when the operator lacks
        ``Microsoft.Authorization/roleAssignments/write``: idle self-deallocate is
        then disabled for this host but manual ``mngr stop``/``start`` still works.
        """
        with self._translate_azure_errors():
            vm = self._compute().virtual_machines.get(self.resource_group, vm_name)
        identity = vm.identity
        principal_id = identity.principal_id if identity is not None else None
        if not principal_id:
            logger.warning(
                "Azure VM {} has no system-assigned identity principal; skipping self-deallocate role assignment "
                "(idle self-deallocate disabled for this host)",
                vm_name,
            )
            return False
        role_definition_id = (
            f"/subscriptions/{self.subscription_id}/providers/Microsoft.Authorization/"
            f"roleDefinitions/{SELF_DEALLOCATE_ROLE_ID}"
        )
        parameters = RoleAssignmentCreateParameters(
            role_definition_id=role_definition_id,
            principal_id=principal_id,
            principal_type="ServicePrincipal",
        )
        try:
            with self._translate_azure_errors():
                self._authorization().role_assignments.create(
                    scope=self._vm_resource_id(vm_name),
                    role_assignment_name=str(uuid4()),
                    parameters=parameters,
                )
        except VpsApiError as e:
            if self._is_role_assignment_exists(e):
                return True
            if self._is_authorization_error(e):
                logger.warning(
                    "Could not assign the self-deallocate role to VM {} (needs "
                    "Microsoft.Authorization/roleAssignments/write). Idle self-deallocate disabled for this host; "
                    "manual `mngr stop`/`start` still works. ({})",
                    vm_name,
                    e,
                )
                return False
            raise
        logger.info("Assigned self-deallocate role to VM {} managed identity", vm_name)
        return True

    def _is_authorization_error(self, error: VpsApiError) -> bool:
        """True when an Azure error is a permission denial (so callers can degrade gracefully)."""
        return error.status_code == 403 or "authorization" in str(error).lower()

    def _is_role_assignment_exists(self, error: VpsApiError) -> bool:
        """True when a role-assignment create failed only because the assignment already exists."""
        return error.status_code == 409 or "roleassignmentexists" in str(error).lower().replace(" ", "")

    def get_instance_status(self, instance_id: VpsInstanceId) -> VpsInstanceStatus:
        try:
            with self._translate_azure_errors():
                instance_view = self._compute().virtual_machines.instance_view(self.resource_group, str(instance_id))
        except VpsApiError as e:
            if e.status_code == 404:
                return VpsInstanceStatus.UNKNOWN
            raise
        for status in instance_view.statuses or ():
            code = status.code or ""
            if code in _POWER_STATE_MAP:
                return _POWER_STATE_MAP[code]
        return VpsInstanceStatus.UNKNOWN

    def get_instance_ip(self, instance_id: VpsInstanceId) -> str:
        with self._translate_azure_errors():
            public_ip = self._network().public_ip_addresses.get(
                self.resource_group, self._public_ip_name(str(instance_id))
            )
        if not public_ip.ip_address:
            raise VpsProvisioningError(f"Instance {instance_id} does not have a public IP yet")
        return public_ip.ip_address

    def _normalize_vm(self, vm: Any, ip_by_name: Mapping[str, str]) -> dict[str, Any]:
        # ``state`` is left empty: Azure's resource-group VM list cannot return
        # power state (``expand=instanceView`` is rejected unless a VM Scale Set
        # filter is applied), so callers that need the live power state of a
        # specific VM fetch it on demand via ``get_instance_status`` (a per-VM
        # ``instance_view`` call). See ``AzureProvider.discover_hosts_and_agents``.
        tags = dict(vm.tags or {})
        return {
            "id": vm.name,
            "main_ip": ip_by_name.get(self._public_ip_name(vm.name), ""),
            "state": "",
            "tags": [f"{key}={value}" for key, value in tags.items()],
        }

    def _list_vms_with_ips(self) -> list[dict[str, Any]]:
        """List all VMs in the resource group, normalized with their public IPs.

        Resolves each VM's public IP from a single ``public_ip_addresses.list``
        call (Azure's VM list does not include IPs). Power state is NOT requested
        here: Azure rejects ``expand=instanceView`` on a resource-group VM list
        (it is only valid with a VM Scale Set filter), so the normalized ``state``
        is empty and live power state is fetched per-VM on demand via
        ``get_instance_status``. Returns ``[]`` when the resource group does not
        exist yet (pre-prepare), so discovery does not error on an unconfigured
        subscription.
        """
        try:
            with self._translate_azure_errors():
                vms = list(self._compute().virtual_machines.list(self.resource_group))
                public_ips = list(self._network().public_ip_addresses.list(self.resource_group))
        except VpsApiError as e:
            if e.status_code == 404:
                return []
            raise
        ip_by_name = {pip.name: (pip.ip_address or "") for pip in public_ips}
        return [self._normalize_vm(vm, ip_by_name) for vm in vms]

    def list_instances(self, provider_tag: str | None = None) -> list[dict[str, Any]]:
        """List VMs in the resource group. Optionally filtered by ``mngr-provider`` tag.

        Returns a normalized list of dicts with keys ``id`` (the VM name),
        ``main_ip``, ``state``, and ``tags`` (a list of ``"key=value"`` strings,
        mirroring Vultr's tag shape). Azure has no server-side tag filter on the
        VM list within a resource group, so the filter is applied client-side.
        """
        instances = self._list_vms_with_ips()
        if provider_tag is None:
            return instances
        wanted = f"mngr-provider={provider_tag}"
        return [instance for instance in instances if wanted in instance["tags"]]

    def list_mngr_managed_vms(self) -> list[dict[str, Any]]:
        """List VMs in the resource group tagged ``managed-by=mngr``.

        Filters on the single decisive ownership tag every mngr-created VM carries
        (the same ``managed-by=mngr`` tag ``delete_managed_resource_group`` uses to
        prove it owns the group), so it spans every mngr provider config bound to
        this resource group regardless of provider name. Used by ``mngr azure
        cleanup`` to refuse deleting the shared resource group while any
        mngr-managed agent still exists.
        """
        managed_by_tag = f"{AZURE_MANAGED_BY_TAG_KEY}={AZURE_MANAGED_BY_TAG_VALUE}"
        return [instance for instance in self._list_vms_with_ips() if managed_by_tag in instance["tags"]]

    def delete_managed_resource_group(self) -> str | None:
        """Delete the mngr-owned resource group (and everything in it). Returns its name.

        The inverse of ``ensure_network``. Returns ``None`` when the group does
        not exist (idempotent). Only deletes a group tagged ``managed-by=mngr``
        (set by ``ensure_network``) -- a group lacking that tag is not mngr's to
        delete, so this raises rather than touching a user-owned resource. The
        ``mngr azure cleanup`` command checks for live mngr-managed VMs before
        calling this, so it never strands a running agent.
        """
        try:
            with self._translate_azure_errors():
                group = self._resource().resource_groups.get(self.resource_group)
        except VpsApiError as e:
            if e.status_code == 404:
                return None
            raise
        tags = dict(group.tags or {})
        if tags.get(AZURE_MANAGED_BY_TAG_KEY) != AZURE_MANAGED_BY_TAG_VALUE:
            raise MngrError(
                f"Refusing to delete resource group {self.resource_group!r}: it is not tagged "
                f"{AZURE_MANAGED_BY_TAG_KEY}={AZURE_MANAGED_BY_TAG_VALUE} and so was not created by "
                "`mngr azure prepare`. If you really want it gone, delete it yourself."
            )
        with self._translate_azure_errors():
            self._resource().resource_groups.begin_delete(self.resource_group).result()
        logger.info("Deleted Azure resource group {} in region {}", self.resource_group, self.region)
        return self.resource_group

    # =========================================================================
    # SSH Key Operations (no native Azure per-key resource; in-memory map)
    # =========================================================================

    def upload_ssh_key(self, name: str, public_key: str) -> str:
        """Stash the public key in memory under ``name``; return ``name`` as the key ID.

        Azure has no per-key resource, so nothing is uploaded to the provider
        here -- ``create_instance`` writes the key into the VM's os_profile. The
        base flow uses one client instance for both calls, so the in-memory map
        bridges them.
        """
        self._ssh_public_keys_by_id[name] = public_key
        logger.debug("Stored SSH public key {} for per-VM injection", name)
        return name

    def delete_ssh_key(self, key_id: str) -> None:
        """Drop the in-memory key entry. Tolerant of an absent key (fresh-process delete)."""
        self._ssh_public_keys_by_id.pop(key_id, None)
        logger.debug("Dropped in-memory SSH public key {}", key_id)
