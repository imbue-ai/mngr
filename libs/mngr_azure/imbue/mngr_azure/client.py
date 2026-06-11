import base64
import os
import re
import time
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import Final
from uuid import uuid4

from azure.core.exceptions import HttpResponseError
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

from imbue.mngr.errors import MngrError
from imbue.mngr_azure.config import AZURE_MANAGED_BY_TAG_KEY
from imbue.mngr_azure.config import AZURE_MANAGED_BY_TAG_VALUE
from imbue.mngr_vps_docker.errors import VpsApiError
from imbue.mngr_vps_docker.errors import VpsProvisioningError
from imbue.mngr_vps_docker.primitives import VpsInstanceId
from imbue.mngr_vps_docker.primitives import VpsInstanceStatus
from imbue.mngr_vps_docker.vps_client import VpsClientInterface

# Tag key/value that ``create_instance`` adds to every VM launched while
# ``PYTEST_CURRENT_TEST`` is set. The conftest session-end scanner uses this
# tag (not the VM name) to find leaked VMs, which means tests do not have to
# constrain host naming: any agent name works.
AZURE_PYTEST_LAUNCHED_TAG: Final[str] = "mngr-pytest-launched"

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
_INVALID_NAME_CHARS_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9-]")
_VM_NAME_STEM_LENGTH: Final[int] = _MAX_VM_NAME_LENGTH - 33

# How long to wait for a resource-provider registration to flip to "Registered".
_PROVIDER_REGISTRATION_TIMEOUT_SECONDS: Final[float] = 180.0
_PROVIDER_REGISTRATION_POLL_SECONDS: Final[float] = 3.0

# Minimum age before an unattached NIC / public IP is eligible for reclaim by the
# next create. Azure reserves a NIC for its would-be VM for 180s after a failed
# create, so a younger unattached NIC is either still reserved (delete would
# fail) or belongs to a create that is mid-flight on another machine -- this
# margin keeps the self-healing sweep from ever deleting an in-flight create's
# NIC.
_ORPHAN_RECLAIM_MIN_AGE_SECONDS: Final[float] = 240.0


def _make_vm_name(label: str, tags: Mapping[str, str]) -> str:
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
    return f"{stem}-{suffix}"[:_MAX_VM_NAME_LENGTH].rstrip("-")


def _computer_name(vm_name: str) -> str:
    """Derive a Linux computer-name (hostname) from the VM name.

    Linux hostnames are <= 64 chars; we cap at 63 and strip trailing dashes so
    the value is always a valid hostname even after truncation.
    """
    return vm_name[:63].rstrip("-")


class AzureVpsClient(VpsClientInterface):
    """Azure VM client implementing the VPS provider interface via the azure-mgmt SDK.

    Bound at construction to a single ``subscription_id`` + ``region`` +
    ``resource_group`` (analogous to ``AwsVpsClient`` being bound to a region).
    To target a different region, instantiate a separate client.

    The one-off infrastructure (resource group, vnet, subnet, NSG) is created by
    ``ensure_network`` (the privileged ``mngr azure prepare`` path); the hot
    ``create_instance`` path is lookup-only (``resolve_subnet_id``) so a
    restricted role with no network-write permission can still create VMs.
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
    image_publisher: str = Field(default="Canonical", description="Marketplace image publisher")
    image_offer: str = Field(default="ubuntu-24_04-lts", description="Marketplace image offer")
    image_sku: str = Field(default="server", description="Marketplace image SKU")
    image_version: str = Field(default="latest", description="Marketplace image version")
    admin_username: str = Field(default="azureuser", description="Admin user the SSH public key is attached to")
    os_disk_size_gb: int = Field(default=30, description="OS managed-disk size in GB")
    os_disk_type: str = Field(default="StandardSSD_LRS", description="OS managed-disk storage account type")
    allowed_ssh_cidrs: tuple[str, ...] = Field(
        default=(),
        description=(
            "CIDR blocks allowed inbound on tcp/22 and tcp/container_ssh_port of the NSG created by "
            "ensure_network. Empty by default (fail-closed): ensure_network raises rather than create "
            "a wide-open rule."
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

    # =========================================================================
    # Lazily-built management clients (overridden in tests to inject fakes)
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

    def _wait_for_provider_registered(self, namespace: str) -> None:
        start = time.monotonic()
        while time.monotonic() - start < _PROVIDER_REGISTRATION_TIMEOUT_SECONDS:
            with self._translate_azure_errors():
                provider = self._resource().providers.get(namespace)
            if provider.registration_state == "Registered":
                return
            time.sleep(_PROVIDER_REGISTRATION_POLL_SECONDS)
        raise MngrError(
            f"Azure resource provider {namespace!r} did not reach 'Registered' within "
            f"{_PROVIDER_REGISTRATION_TIMEOUT_SECONDS:.0f}s. Register it manually with "
            f"`az provider register --namespace {namespace}` and retry `mngr azure prepare`."
        )

    def ensure_network(self) -> str:
        """Create the mngr resource group + vnet/subnet/NSG if absent. Returns the RG name.

        Idempotent (``create_or_update`` throughout). Registers the required
        resource providers first, then creates the resource group (tagged
        ``managed-by=mngr`` so ``cleanup`` can prove ownership), the NSG (opening
        tcp/22 + tcp/``container_ssh_port`` to ``allowed_ssh_cidrs``), and the
        vnet whose subnet references that NSG.

        Fails closed if ``allowed_ssh_cidrs`` is empty: rather than create a
        wide-open NSG, raise so the caller makes an explicit decision. This is
        the privileged write path used by ``mngr azure prepare``; the hot path in
        ``create_instance`` uses ``resolve_subnet_id`` instead (lookup-only).
        """
        if not self.allowed_ssh_cidrs:
            raise MngrError(
                "Cannot auto-create an Azure NSG: allowed_ssh_cidrs is empty. "
                "Set allowed_ssh_cidrs to a tuple of CIDR blocks (e.g. ('203.0.113.4/32',) for your "
                "own IP), or pre-create the network targeting the configured subnet/NSG."
            )
        self._register_resource_providers()
        with self._translate_azure_errors():
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
        return self.resource_group

    def _ensure_nsg(self) -> str:
        """Create / update the NSG opening SSH ingress. Returns its resource id."""
        security_rules: list[Any] = []
        priority = 1000
        for port in ("22", str(self.container_ssh_port)):
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
                    f"Run `mngr azure prepare` once to create the resource group / vnet / subnet / NSG, "
                    "then retry the create."
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
        """
        if region != self.region:
            raise VpsApiError(
                400,
                f"Cross-region create not supported: client bound to {self.region!r}, "
                f"got region={region!r}. Instantiate a region-specific client.",
            )

        subnet_id = self.resolve_subnet_id()
        # Self-heal: reclaim NIC/public-IP orphans left by an earlier failed
        # create whose 180s NIC reservation has since expired. Done here (on the
        # next create) rather than by blocking the failed create for ~3 minutes.
        self._reclaim_orphaned_network_resources()
        vm_name = _make_vm_name(label, tags)
        public_key = self._resolve_ssh_public_key(ssh_key_ids)

        vm_tags: dict[str, str] = {**self._base_tags(), **dict(tags)}
        vm_tags["mngr-created-at"] = datetime.now(timezone.utc).isoformat()
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
        vm_name: str,
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
                # Azure requires custom_data base64-encoded; the Ubuntu cloud-init
                # Azure datasource decodes it and runs it as user-data on first boot.
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
        return compute_models.VirtualMachine(location=self.region, tags=dict(vm_tags), properties=properties)

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
        raises ``NicReservedForAnotherVm``. That is expected, not an error: the
        next ``create_instance`` reclaims it via ``_reclaim_orphaned_network_resources``
        once the reservation expires, so it is logged at info, not warning.
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
                "Orphaned {} for {} is still reserved by the failed VM; it will be reclaimed on the next create.",
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

    def _reclaim_orphaned_network_resources(self) -> None:
        """Best-effort delete unattached, aged mngr NICs / public IPs from failed creates.

        ``create_instance`` provisions a public IP + NIC before the VM; a VM
        create that fails (capacity / quota) leaves them orphaned, and Azure
        reserves the NIC for the would-be VM for 180s so immediate cleanup is
        blocked. Rather than block a failed create for ~3 minutes, the next create
        reclaims them here -- self-healing on the next operation, the same pattern
        discovery / GC use. Only resources older than the reservation window
        (``_ORPHAN_RECLAIM_MIN_AGE_SECONDS``) are touched, so an in-flight
        concurrent create is never disturbed. The whole sweep is best-effort: a
        list / delete failure is logged and never blocks the create that follows.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=_ORPHAN_RECLAIM_MIN_AGE_SECONDS)
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
            try:
                with self._translate_azure_errors():
                    self._network().network_interfaces.begin_delete(self.resource_group, nic.name).result()
                logger.info("Reclaimed orphaned NIC {}", nic.name)
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
            try:
                with self._translate_azure_errors():
                    self._network().public_ip_addresses.begin_delete(self.resource_group, public_ip.name).result()
                logger.info("Reclaimed orphaned public IP {}", public_ip.name)
            except VpsApiError as e:
                logger.warning("Failed to reclaim orphaned public IP {}: {}", public_ip.name, e)

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
        call (Azure's VM list does not include IPs). Returns ``[]`` when the
        resource group does not exist yet (pre-prepare), so discovery does not
        error on an unconfigured subscription.
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
        """List VMs in the resource group carrying any ``mngr-provider`` tag.

        Filters by tag-*key* presence (any value), so it spans every mngr
        provider config bound to this resource group, not just one provider name.
        Used by ``mngr azure cleanup`` to refuse deleting the shared resource
        group while any mngr-managed agent still exists.
        """
        return [
            instance
            for instance in self._list_vms_with_ips()
            if any(tag.startswith("mngr-provider=") for tag in instance["tags"])
        ]

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
