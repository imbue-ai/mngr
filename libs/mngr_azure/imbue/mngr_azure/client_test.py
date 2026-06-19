import base64
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from typing import Any

import pytest

from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_azure.client import AzureVmName
from imbue.mngr_azure.client import LinuxHostname
from imbue.mngr_azure.client import SELF_DEALLOCATE_ROLE_ID
from imbue.mngr_azure.client import SELF_DEALLOCATE_ROLE_NAME
from imbue.mngr_azure.client import _computer_name
from imbue.mngr_azure.client import _make_vm_name
from imbue.mngr_azure.errors import InvalidAzureIdentifierError
from imbue.mngr_azure.testing import FakeAuthorizationClient
from imbue.mngr_azure.testing import FakeComputeClient
from imbue.mngr_azure.testing import FakeNetworkClient
from imbue.mngr_azure.testing import FakeResourceClient
from imbue.mngr_azure.testing import _StubbedAzureVpsClient
from imbue.mngr_azure.testing import make_azure_http_error
from imbue.mngr_vps.errors import VpsApiError
from imbue.mngr_vps.errors import VpsProvisioningError
from imbue.mngr_vps.primitives import VpsInstanceId
from imbue.mngr_vps.primitives import VpsInstanceStatus

_SUBSCRIPTION = "sub-123"
_REGION = "westus"


def _make_client(
    *,
    compute: FakeComputeClient | None = None,
    network: FakeNetworkClient | None = None,
    resource: FakeResourceClient | None = None,
    authorization: FakeAuthorizationClient | None = None,
    allowed_ssh_cidrs: tuple[str, ...] = ("203.0.113.4/32",),
    subnet_present: bool = True,
) -> _StubbedAzureVpsClient:
    """Build a stubbed client with fakes and (by default) a prepared subnet."""
    fake_network = network or FakeNetworkClient()
    if subnet_present and fake_network.subnets.get_result is None and fake_network.subnets.get_error is None:
        fake_network.subnets.get_result = SimpleNamespace(id="/subnets/mngr-subnet")
    return _StubbedAzureVpsClient(
        credential=object(),
        subscription_id=_SUBSCRIPTION,
        region=_REGION,
        allowed_ssh_cidrs=allowed_ssh_cidrs,
        stubbed_compute_client=compute or FakeComputeClient(),
        stubbed_network_client=fake_network,
        stubbed_resource_client=resource or FakeResourceClient(),
        stubbed_authorization_client=authorization or FakeAuthorizationClient(),
    )


def _created_vm(client: _StubbedAzureVpsClient) -> Any:
    """Return the single VirtualMachine model captured by the fake compute client."""
    compute = client.stubbed_compute_client
    assert len(compute.virtual_machines.created) == 1
    return compute.virtual_machines.created[0][1]


# =========================================================================
# VM naming
# =========================================================================


def test_make_vm_name_is_valid_and_typed() -> None:
    """_make_vm_name yields a well-formed, Azure-valid AzureVmName from a messy label."""
    name = _make_vm_name("My Agent!", {"mngr-host-id": "host-abc123def456"})
    assert isinstance(name, AzureVmName)
    assert name[0].isalnum()
    assert not name.endswith("-")
    assert len(name) <= 64


def test_make_vm_name_falls_back_to_mngr_stem_for_empty_label() -> None:
    """An all-invalid label still produces a valid name (the 'mngr' stem fallback)."""
    name = _make_vm_name("!!!", {"mngr-host-id": "host-abc123def456"})
    assert isinstance(name, AzureVmName)
    assert name.startswith("mngr-")


def test_azure_vm_name_rejects_invalid() -> None:
    """The VM-name type rejects strings that violate Azure's resource-name shape."""
    with pytest.raises(InvalidAzureIdentifierError):
        AzureVmName("")
    with pytest.raises(InvalidAzureIdentifierError):
        AzureVmName("ends-with-dash-")
    with pytest.raises(InvalidAzureIdentifierError):
        AzureVmName("Has-Upper")
    with pytest.raises(InvalidAzureIdentifierError):
        AzureVmName("has space")
    with pytest.raises(InvalidAzureIdentifierError):
        AzureVmName("a" * 65)


def test_computer_name_caps_at_63_and_strips_trailing_dash() -> None:
    """_computer_name truncates a 64-char VM name to a valid 63-char LinuxHostname."""
    computer_name = _computer_name(AzureVmName("a" * 64))
    assert isinstance(computer_name, LinuxHostname)
    assert len(computer_name) == 63
    # Truncation that lands on a dash must not leave a trailing dash.
    trimmed = _computer_name(AzureVmName("a" * 62 + "-b"))
    assert trimmed == "a" * 62
    assert not trimmed.endswith("-")


def test_linux_hostname_rejects_invalid() -> None:
    """The hostname type rejects empty, over-long, and out-of-charset strings."""
    with pytest.raises(InvalidAzureIdentifierError):
        LinuxHostname("")
    with pytest.raises(InvalidAzureIdentifierError):
        LinuxHostname("a" * 64)
    with pytest.raises(InvalidAzureIdentifierError):
        LinuxHostname("ends-with-dash-")
    with pytest.raises(InvalidAzureIdentifierError):
        LinuxHostname("Has-Upper")


# =========================================================================
# create_instance
# =========================================================================


def test_create_instance_builds_public_ip_nic_and_vm() -> None:
    client = _make_client()
    client.upload_ssh_key("k1", "ssh-ed25519 AAAA")
    instance_id = client.create_instance(
        label="my-agent",
        region=_REGION,
        plan="Standard_B2s",
        user_data="#cloud-config\n",
        ssh_key_ids=["k1"],
        tags={"mngr-host-id": "host-abc", "mngr-provider": "azure"},
    )
    network = client.stubbed_network_client
    # A public IP and a NIC were created.
    assert len(network.public_ip_addresses.created) == 1
    assert len(network.network_interfaces.created) == 1
    nic = network.network_interfaces.created[0][1]
    ip_config = nic.ip_configurations[0]
    assert ip_config.subnet.id == "/subnets/mngr-subnet"
    assert ip_config.public_ip_address.delete_option == "Delete"
    # The VM references the NIC with delete_option=Delete (cascade on destroy).
    vm = _created_vm(client)
    assert vm.network_profile.network_interfaces[0].delete_option == "Delete"
    assert vm.storage_profile.os_disk.delete_option == "Delete"
    assert vm.hardware_profile.vm_size == "Standard_B2s"
    assert str(instance_id).startswith("my-agent-")


def test_create_instance_injects_ssh_key_and_base64_custom_data() -> None:
    client = _make_client()
    client.upload_ssh_key("k1", "ssh-ed25519 PUBKEY")
    client.create_instance(
        label="agent",
        region=_REGION,
        plan="Standard_B2s",
        user_data="#cloud-config\nruncmd: []\n",
        ssh_key_ids=["k1"],
        tags={"mngr-host-id": "host-xyz"},
    )
    vm = _created_vm(client)
    linux = vm.os_profile.linux_configuration
    assert linux.disable_password_authentication is True
    assert linux.ssh.public_keys[0].key_data == "ssh-ed25519 PUBKEY"
    decoded = base64.b64decode(vm.os_profile.custom_data).decode("utf-8")
    assert decoded == "#cloud-config\nruncmd: []\n"


def test_create_instance_tags_pytest_launched() -> None:
    # The test process runs under pytest (PYTEST_CURRENT_TEST is set), so every
    # VM created here is tagged for the session-end orphan scanner.
    client = _make_client()
    client.upload_ssh_key("k1", "ssh-ed25519 AAAA")
    client.create_instance(
        label="agent",
        region=_REGION,
        plan="Standard_B2s",
        user_data="#cloud-config\n",
        ssh_key_ids=["k1"],
        tags={"mngr-host-id": "host-abc"},
    )
    vm = _created_vm(client)
    assert vm.tags["mngr-pytest-launched"] == "true"
    assert vm.tags["managed-by"] == "mngr"
    assert "mngr-created-at" in vm.tags


def test_create_instance_sets_spot_fields_when_requested() -> None:
    client = _make_client()
    client.upload_ssh_key("k1", "ssh-ed25519 AAAA")
    client.create_instance(
        label="agent",
        region=_REGION,
        plan="Standard_B2s",
        user_data="#cloud-config\n",
        ssh_key_ids=["k1"],
        tags={"mngr-host-id": "host-abc"},
        spot=True,
    )
    vm = _created_vm(client)
    assert vm.priority == "Spot"
    assert vm.eviction_policy == "Delete"
    assert vm.billing_profile.max_price == -1.0


def test_create_instance_omits_spot_fields_by_default() -> None:
    client = _make_client()
    client.upload_ssh_key("k1", "ssh-ed25519 AAAA")
    client.create_instance(
        label="agent",
        region=_REGION,
        plan="Standard_B2s",
        user_data="#cloud-config\n",
        ssh_key_ids=["k1"],
        tags={"mngr-host-id": "host-abc"},
    )
    vm = _created_vm(client)
    assert vm.priority is None


def test_create_instance_cross_region_raises() -> None:
    client = _make_client()
    client.upload_ssh_key("k1", "ssh-ed25519 AAAA")
    with pytest.raises(VpsApiError, match="Cross-region create not supported"):
        client.create_instance(
            label="agent",
            region="eastus",
            plan="Standard_B2s",
            user_data="#cloud-config\n",
            ssh_key_ids=["k1"],
            tags={},
        )


def test_create_instance_raises_when_subnet_missing() -> None:
    client = _make_client(subnet_present=False)
    client.upload_ssh_key("k1", "ssh-ed25519 AAAA")
    with pytest.raises(MngrError, match="mngr azure prepare"):
        client.create_instance(
            label="agent",
            region=_REGION,
            plan="Standard_B2s",
            user_data="#cloud-config\n",
            ssh_key_ids=["k1"],
            tags={},
        )


def test_create_instance_cleans_up_nic_and_ip_when_vm_create_fails() -> None:
    # When the VM create fails (e.g. SkuNotAvailable / quota), create_instance
    # raises before returning an instance id, so the create_host failure-cleanup
    # cannot reach the public IP + NIC made before the VM. They must be deleted
    # here so a failed create leaks nothing.
    compute = FakeComputeClient()
    compute.virtual_machines.create_error = make_azure_http_error(409, "SkuNotAvailable")
    client = _make_client(compute=compute)
    client.upload_ssh_key("k1", "ssh-ed25519 AAAA")
    with pytest.raises(VpsApiError, match="SkuNotAvailable"):
        client.create_instance(
            label="agent",
            region=_REGION,
            plan="Standard_B2s",
            user_data="#cloud-config\n",
            ssh_key_ids=["k1"],
            tags={"mngr-host-id": "host-abc"},
        )
    network = client.stubbed_network_client
    assert len(network.network_interfaces.deleted) == 1
    assert len(network.public_ip_addresses.deleted) == 1
    # NIC must be deleted before the public IP it references.
    assert network.network_interfaces.deleted[0].endswith("-nic")
    assert network.public_ip_addresses.deleted[0].endswith("-ip")


# =========================================================================
# reclaim_orphaned_network_resources (GC-time NIC/IP reclaim)
# =========================================================================


def _orphan_resource(name: str, *, age_seconds: float, attached: bool, attach_attr: str, tagged: bool = True) -> Any:
    created_at = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    tags = (
        {"mngr-provider": "azure", "mngr-created-at": created_at.isoformat(), "managed-by": "mngr"} if tagged else {}
    )
    return SimpleNamespace(
        name=name, tags=tags, **{attach_attr: SimpleNamespace(id="/attached") if attached else None}
    )


def test_reclaim_deletes_aged_orphan_nic_and_ip() -> None:
    # A NIC + public IP left unattached by an earlier failed create, now older
    # than the reservation window, are reclaimed at GC time and reported back.
    network = FakeNetworkClient()
    network.network_interfaces.list_result = [
        _orphan_resource("old-nic", age_seconds=600, attached=False, attach_attr="virtual_machine")
    ]
    network.public_ip_addresses.list_result = [
        _orphan_resource("old-ip", age_seconds=600, attached=False, attach_attr="ip_configuration")
    ]
    client = _make_client(network=network)
    reclaimed = client.reclaim_orphaned_network_resources(provider_name=ProviderInstanceName("azure"))
    assert "old-nic" in network.network_interfaces.deleted
    assert "old-ip" in network.public_ip_addresses.deleted
    assert {(r.kind, r.name) for r in reclaimed} == {("network_interface", "old-nic"), ("public_ip", "old-ip")}
    assert all(r.provider_name == "azure" for r in reclaimed)


def test_reclaim_skips_recent_attached_and_untagged() -> None:
    network = FakeNetworkClient()
    network.network_interfaces.list_result = [
        # Too young: could be an in-flight concurrent create -- must not be touched.
        _orphan_resource("young-nic", age_seconds=10, attached=False, attach_attr="virtual_machine"),
        # Attached to a VM -- in use.
        _orphan_resource("attached-nic", age_seconds=600, attached=True, attach_attr="virtual_machine"),
        # Not mngr-tagged -- not ours.
        _orphan_resource("foreign-nic", age_seconds=600, attached=False, attach_attr="virtual_machine", tagged=False),
    ]
    client = _make_client(network=network)
    reclaimed = client.reclaim_orphaned_network_resources(provider_name=ProviderInstanceName("azure"))
    assert network.network_interfaces.deleted == []
    assert reclaimed == []


def test_reclaim_dry_run_reports_without_deleting() -> None:
    network = FakeNetworkClient()
    network.network_interfaces.list_result = [
        _orphan_resource("old-nic", age_seconds=600, attached=False, attach_attr="virtual_machine")
    ]
    network.public_ip_addresses.list_result = [
        _orphan_resource("old-ip", age_seconds=600, attached=False, attach_attr="ip_configuration")
    ]
    client = _make_client(network=network)
    reclaimed = client.reclaim_orphaned_network_resources(provider_name=ProviderInstanceName("azure"), dry_run=True)
    assert network.network_interfaces.deleted == []
    assert network.public_ip_addresses.deleted == []
    assert {(r.kind, r.name) for r in reclaimed} == {("network_interface", "old-nic"), ("public_ip", "old-ip")}


def test_create_instance_requires_uploaded_ssh_key() -> None:
    client = _make_client()
    with pytest.raises(VpsApiError, match="No in-memory SSH public key"):
        client.create_instance(
            label="agent",
            region=_REGION,
            plan="Standard_B2s",
            user_data="#cloud-config\n",
            ssh_key_ids=["missing"],
            tags={},
        )


# =========================================================================
# ensure_network / resolve_subnet_id
# =========================================================================


def test_ensure_network_skips_ssh_rule_and_warns_when_no_cidrs(log_warnings: list[str]) -> None:
    """Empty allowed_ssh_cidrs creates the NSG with no SSH allow rule and warns (fail-open).

    The NSG's implicit default-deny then leaves instances unreachable from
    outside the vnet -- the analog of AWS's zero-ingress security group. (An
    Azure SecurityRule with an empty source_address_prefixes is API-rejected, so
    "no ingress" is expressed as the absence of the rule.) ensure_network still
    succeeds; the empty case is an "I'll wire my own ingress later" signal, not a
    fail-closed gate. Mirrors the AWS / GCP providers.
    """
    client = _make_client(allowed_ssh_cidrs=())
    assert client.ensure_network().resource_group == "mngr"
    nsg = client.stubbed_network_client.network_security_groups.created[0][1]
    assert nsg.security_rules == []
    assert any("allowed_ssh_cidrs is empty" in msg for msg in log_warnings)


def test_ensure_network_warns_when_open_to_internet(log_warnings: list[str]) -> None:
    """0.0.0.0/0 is the default but should still produce a visible warning at prepare time."""
    client = _make_client(allowed_ssh_cidrs=("0.0.0.0/0",))
    assert client.ensure_network().resource_group == "mngr"
    nsg = client.stubbed_network_client.network_security_groups.created[0][1]
    assert nsg.security_rules[0].source_address_prefixes == ["0.0.0.0/0"]
    assert any("0.0.0.0/0" in msg for msg in log_warnings)


def test_ensure_network_creates_rg_nsg_vnet_and_registers_providers() -> None:
    resource = FakeResourceClient()
    resource.providers.registration_state = "NotRegistered"
    client = _make_client(resource=resource)
    returned = client.ensure_network()
    assert returned.resource_group == "mngr"
    assert returned.region == _REGION
    # The fake reports the RG as absent by default, so this is a first-run create.
    assert returned.was_created is True
    # Resource providers registered.
    assert set(resource.providers.registered) == {"Microsoft.Compute", "Microsoft.Network", "Microsoft.Storage"}
    # RG created with the managed-by tag.
    assert resource.resource_groups.created[0][1].tags["managed-by"] == "mngr"
    network = client.stubbed_network_client
    assert len(network.network_security_groups.created) == 1
    nsg = network.network_security_groups.created[0][1]
    assert {rule.destination_port_range for rule in nsg.security_rules} == {"22", "2222"}
    assert nsg.security_rules[0].source_address_prefixes == ["203.0.113.4/32"]
    # vnet+subnet created, subnet references the NSG.
    assert len(network.virtual_networks.created) == 1
    subnet = network.virtual_networks.created[0][1].subnets[0]
    assert subnet.network_security_group.id == "/nsg/mngr-nsg"


def test_ensure_network_reports_not_created_when_rg_already_exists() -> None:
    """An idempotent re-run (RG already present) reports was_created=False.

    The resource group / NSG / vnet are still create_or_update'd (idempotent), but
    the create-vs-reuse signal lets the CLI distinguish a first run from a no-op.
    """
    resource = FakeResourceClient()
    resource.resource_groups.exists = True
    client = _make_client(resource=resource)
    assert client.ensure_network().was_created is False


def test_resolve_subnet_id_returns_id_when_present() -> None:
    client = _make_client()
    assert client.resolve_subnet_id() == "/subnets/mngr-subnet"


# =========================================================================
# status / ip / listing
# =========================================================================


@pytest.mark.parametrize(
    ("power_code", "expected_active"),
    [
        ("PowerState/running", True),
        ("PowerState/starting", False),
        ("PowerState/stopped", False),
        ("PowerState/deallocated", False),
    ],
)
def test_get_instance_status_mapping(power_code: str, expected_active: bool) -> None:
    compute = FakeComputeClient()
    compute.virtual_machines.instance_view_result = SimpleNamespace(
        statuses=[SimpleNamespace(code="ProvisioningState/succeeded"), SimpleNamespace(code=power_code)]
    )
    client = _make_client(compute=compute)
    status = client.get_instance_status(VpsInstanceId("vm1"))
    assert (status == VpsInstanceStatus.ACTIVE) == expected_active


def test_get_instance_status_unknown_when_404() -> None:
    compute = FakeComputeClient()
    compute.virtual_machines.instance_view_error = make_azure_http_error(404, "not found")
    client = _make_client(compute=compute)
    assert client.get_instance_status(VpsInstanceId("vm1")) == VpsInstanceStatus.UNKNOWN


def test_get_instance_ip_returns_ip() -> None:
    network = FakeNetworkClient()
    network.public_ip_addresses.get_result = SimpleNamespace(ip_address="203.0.113.7")
    client = _make_client(network=network)
    assert client.get_instance_ip(VpsInstanceId("vm1")) == "203.0.113.7"


def test_get_instance_ip_raises_when_not_assigned() -> None:
    network = FakeNetworkClient()
    network.public_ip_addresses.get_result = SimpleNamespace(ip_address=None)
    client = _make_client(network=network)
    with pytest.raises(VpsProvisioningError, match="does not have a public IP yet"):
        client.get_instance_ip(VpsInstanceId("vm1"))


def test_list_instances_filters_by_provider_tag() -> None:
    compute = FakeComputeClient()
    compute.virtual_machines.list_result = [
        SimpleNamespace(name="vm-a", tags={"mngr-provider": "azure"}, instance_view=None),
        SimpleNamespace(name="vm-b", tags={"mngr-provider": "other"}, instance_view=None),
    ]
    network = FakeNetworkClient()
    network.public_ip_addresses.list_result = [SimpleNamespace(name="vm-a-ip", ip_address="203.0.113.7")]
    client = _make_client(compute=compute, network=network)
    instances = client.list_instances(provider_tag="azure")
    assert len(instances) == 1
    assert instances[0]["id"] == "vm-a"
    assert instances[0]["main_ip"] == "203.0.113.7"


def test_list_instances_empty_when_rg_missing() -> None:
    compute = FakeComputeClient()
    compute.virtual_machines.list_error = make_azure_http_error(404, "resource group not found")
    client = _make_client(compute=compute)
    assert client.list_instances() == []


def test_list_mngr_managed_vms_spans_provider_names() -> None:
    """Returns every managed-by=mngr VM across provider names, excluding untagged VMs."""
    compute = FakeComputeClient()
    compute.virtual_machines.list_result = [
        SimpleNamespace(name="vm-a", tags={"managed-by": "mngr", "mngr-provider": "azure-west"}, instance_view=None),
        SimpleNamespace(name="vm-b", tags={"managed-by": "mngr", "mngr-provider": "azure-east"}, instance_view=None),
        SimpleNamespace(name="vm-c", tags={"team": "infra"}, instance_view=None),
    ]
    client = _make_client(compute=compute)
    managed = client.list_mngr_managed_vms()
    assert sorted(vm["id"] for vm in managed) == ["vm-a", "vm-b"]


# =========================================================================
# resource group cleanup
# =========================================================================


def test_delete_managed_resource_group_deletes_when_owned() -> None:
    resource = FakeResourceClient()
    resource.resource_groups.get_result = SimpleNamespace(name="mngr", tags={"managed-by": "mngr"})
    client = _make_client(resource=resource)
    assert client.delete_managed_resource_group() == "mngr"
    assert resource.resource_groups.deleted == ["mngr"]


def test_delete_managed_resource_group_refuses_when_not_owned() -> None:
    resource = FakeResourceClient()
    resource.resource_groups.get_result = SimpleNamespace(name="mngr", tags={})
    client = _make_client(resource=resource)
    with pytest.raises(MngrError, match="not tagged managed-by=mngr"):
        client.delete_managed_resource_group()
    assert resource.resource_groups.deleted == []


def test_delete_managed_resource_group_none_when_missing() -> None:
    resource = FakeResourceClient()
    # get_result left None -> the fake raises 404.
    client = _make_client(resource=resource)
    assert client.delete_managed_resource_group() is None


# =========================================================================
# destroy
# =========================================================================


def test_destroy_instance_idempotent_on_404() -> None:
    compute = FakeComputeClient()
    compute.virtual_machines.delete_error = make_azure_http_error(404, "not found")
    client = _make_client(compute=compute)
    # Should not raise.
    client.destroy_instance(VpsInstanceId("vm1"))


def test_set_instance_tags_merges_into_existing_tags() -> None:
    """set_instance_tags upserts via read-merge-write, preserving the VM's other tags.

    Azure's VM update replaces the whole tags dict, so the merge (not replace) is the
    key correctness property: the renamed host-name tag lands without clobbering tags
    like ``mngr-host-id`` that offline discovery also relies on.
    """
    compute = FakeComputeClient()
    compute.virtual_machines.get_result = SimpleNamespace(
        tags={"mngr-host-id": "host-1", "mngr-host-name": "mngr-old"}
    )
    client = _make_client(compute=compute)
    client.set_instance_tags(VpsInstanceId("vm1"), {"mngr-host-name": "mngr-new"})
    assert len(compute.virtual_machines.updated) == 1
    vm_name, parameters = compute.virtual_machines.updated[0]
    assert vm_name == "vm1"
    assert parameters.tags == {"mngr-host-id": "host-1", "mngr-host-name": "mngr-new"}


# =========================================================================
# deallocate_instance / start_instance (Azure-only idle-pause + resume)
# =========================================================================


def test_deallocate_instance_records_the_deallocate() -> None:
    """deallocate_instance issues begin_deallocate (halting compute billing) and awaits it."""
    compute = FakeComputeClient()
    client = _make_client(compute=compute)
    client.deallocate_instance(VpsInstanceId("vm1"))
    assert compute.virtual_machines.deallocated == ["vm1"]


def test_start_instance_returns_preserved_public_ip_and_records_start() -> None:
    """start_instance issues begin_start, then returns the VM's preserved static public IP.

    The IP is allocated Static, so it is unchanged across deallocate/start (this is
    why AzureProvider.start_host needs no known_hosts rebind). The returned address
    is whatever ``get_instance_ip`` reports for the started VM.
    """
    compute = FakeComputeClient()
    network = FakeNetworkClient()
    network.public_ip_addresses.get_result = SimpleNamespace(ip_address="203.0.113.7")
    client = _make_client(compute=compute, network=network)
    assert client.start_instance(VpsInstanceId("vm1")) == "203.0.113.7"
    assert compute.virtual_machines.started == ["vm1"]


def test_deallocate_instance_raises_when_the_operation_outlasts_the_timeout() -> None:
    """A deallocate long-running operation still in flight at ``timeout_seconds`` raises VpsProvisioningError."""
    compute = FakeComputeClient()
    compute.virtual_machines.deallocate_completes = False
    client = _make_client(compute=compute)
    with pytest.raises(VpsProvisioningError, match="did not finish within"):
        client.deallocate_instance(VpsInstanceId("vm1"), timeout_seconds=0.01)


def test_start_instance_raises_when_the_operation_outlasts_the_timeout() -> None:
    """A start long-running operation still in flight at ``timeout_seconds`` raises VpsProvisioningError."""
    compute = FakeComputeClient()
    compute.virtual_machines.start_completes = False
    client = _make_client(compute=compute)
    with pytest.raises(VpsProvisioningError, match="did not finish within"):
        client.start_instance(VpsInstanceId("vm1"), timeout_seconds=0.01)


# =========================================================================
# list power-state population (deallocated VM surfaces state="deallocated")
# =========================================================================


def test_list_instances_does_not_request_instance_view_expand() -> None:
    """The VM list must NOT pass expand=instanceView (Azure 400s it on a resource-group list).

    Regression guard: ``expand=instanceView`` is only valid with a VM Scale Set
    filter, so requesting it on the resource-group VM list breaks every Azure
    operation (create/list/stop/start). The list therefore carries no power state
    (``state`` is always empty); live power state is fetched per-VM via
    ``get_instance_status``.
    """
    compute = FakeComputeClient()
    compute.virtual_machines.list_result = [
        SimpleNamespace(
            name="vm-a",
            tags={"mngr-provider": "azure"},
            # Even if an instance view is present on the object, the list must not
            # surface it as state (and must not have requested the expand).
            instance_view=SimpleNamespace(statuses=[SimpleNamespace(code="PowerState/deallocated")]),
        )
    ]
    client = _make_client(compute=compute)
    instances = client.list_instances()
    assert compute.virtual_machines.last_list_expand is None
    assert len(instances) == 1
    assert instances[0]["state"] == ""


# =========================================================================
# ensure_self_deallocate_role / assign_self_deallocate_role (graceful fallback)
# =========================================================================


def _self_vm_scope(vm_name: str) -> str:
    return f"/subscriptions/{_SUBSCRIPTION}/resourceGroups/mngr/providers/Microsoft.Compute/virtualMachines/{vm_name}"


def test_ensure_self_deallocate_role_creates_role_and_returns_id() -> None:
    """ensure_self_deallocate_role records the custom role definition and returns its id."""
    authorization = FakeAuthorizationClient()
    client = _make_client(authorization=authorization)
    role_id = client.ensure_self_deallocate_role()
    assert role_id is not None
    assert len(authorization.role_definitions.created) == 1
    scope, role_definition_id, role_definition = authorization.role_definitions.created[0]
    assert scope == f"/subscriptions/{_SUBSCRIPTION}"
    assert role_definition_id == SELF_DEALLOCATE_ROLE_ID
    assert role_definition.role_name == SELF_DEALLOCATE_ROLE_NAME
    assert "Microsoft.Compute/virtualMachines/deallocate/action" in role_definition.permissions[0].actions


def test_ensure_self_deallocate_role_returns_none_on_403(log_warnings: list[str]) -> None:
    """A 403 (operator lacks roleDefinitions/write) degrades to None with nothing recorded."""
    authorization = FakeAuthorizationClient()
    authorization.role_definitions.create_error = make_azure_http_error(403, "AuthorizationFailed")
    client = _make_client(authorization=authorization)
    assert client.ensure_self_deallocate_role() is None
    assert authorization.role_definitions.created == []
    assert any("custom role" in w for w in log_warnings), log_warnings


def _vm_with_principal(principal_id: str | None) -> SimpleNamespace:
    identity = SimpleNamespace(principal_id=principal_id) if principal_id is not None else None
    return SimpleNamespace(identity=identity)


def test_assign_self_deallocate_role_creates_assignment_scoped_to_vm() -> None:
    """A successful assignment is scoped to the VM and carries the VM's identity principal id."""
    compute = FakeComputeClient()
    compute.virtual_machines.get_result = _vm_with_principal("principal-123")
    authorization = FakeAuthorizationClient()
    client = _make_client(compute=compute, authorization=authorization)
    assert client.assign_self_deallocate_role("vm1") is True
    assert len(authorization.role_assignments.created) == 1
    scope, _name, parameters = authorization.role_assignments.created[0]
    assert scope == _self_vm_scope("vm1")
    assert parameters.principal_id == "principal-123"
    assert SELF_DEALLOCATE_ROLE_ID in parameters.role_definition_id


def test_assign_self_deallocate_role_false_when_no_principal(log_warnings: list[str]) -> None:
    """A VM with no system-assigned identity principal cannot be assigned the role -> False."""
    compute = FakeComputeClient()
    compute.virtual_machines.get_result = _vm_with_principal(None)
    authorization = FakeAuthorizationClient()
    client = _make_client(compute=compute, authorization=authorization)
    assert client.assign_self_deallocate_role("vm1") is False
    assert authorization.role_assignments.created == []
    assert any("no system-assigned identity principal" in w for w in log_warnings), log_warnings


def test_assign_self_deallocate_role_false_on_403(log_warnings: list[str]) -> None:
    """A 403 (operator lacks roleAssignments/write) degrades to False with a warning."""
    compute = FakeComputeClient()
    compute.virtual_machines.get_result = _vm_with_principal("principal-123")
    authorization = FakeAuthorizationClient()
    authorization.role_assignments.create_error = make_azure_http_error(403, "AuthorizationFailed")
    client = _make_client(compute=compute, authorization=authorization)
    assert client.assign_self_deallocate_role("vm1") is False
    assert any("Could not assign the self-deallocate role" in w for w in log_warnings), log_warnings


def test_assign_self_deallocate_role_true_when_assignment_already_exists() -> None:
    """A 409 RoleAssignmentExists is treated as success (idempotent re-assign) -> True."""
    compute = FakeComputeClient()
    compute.virtual_machines.get_result = _vm_with_principal("principal-123")
    authorization = FakeAuthorizationClient()
    authorization.role_assignments.create_error = make_azure_http_error(409, "RoleAssignmentExists")
    client = _make_client(compute=compute, authorization=authorization)
    assert client.assign_self_deallocate_role("vm1") is True


# =========================================================================
# _is_authorization_error / _is_role_assignment_exists (error classifiers)
# =========================================================================


def test_is_authorization_error_classifies_403_and_message() -> None:
    client = _make_client()
    assert client._is_authorization_error(VpsApiError(403, "denied")) is True
    assert client._is_authorization_error(VpsApiError(400, "AuthorizationFailed for scope")) is True
    assert client._is_authorization_error(VpsApiError(404, "not found")) is False


def test_is_role_assignment_exists_classifies_409_and_message() -> None:
    client = _make_client()
    assert client._is_role_assignment_exists(VpsApiError(409, "conflict")) is True
    assert client._is_role_assignment_exists(VpsApiError(400, "RoleAssignmentExists")) is True
    # Whitespace-insensitive match (the message can be spaced "Role Assignment Exists").
    assert client._is_role_assignment_exists(VpsApiError(400, "Role Assignment Exists")) is True
    assert client._is_role_assignment_exists(VpsApiError(400, "some other error")) is False
