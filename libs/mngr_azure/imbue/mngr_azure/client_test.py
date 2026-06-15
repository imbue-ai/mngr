import base64
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from typing import Any

import pytest

from imbue.mngr.errors import MngrError
from imbue.mngr_azure.client import AzureVmName
from imbue.mngr_azure.client import LinuxHostname
from imbue.mngr_azure.client import _computer_name
from imbue.mngr_azure.client import _make_vm_name
from imbue.mngr_azure.errors import InvalidAzureIdentifierError
from imbue.mngr_azure.testing import FakeComputeClient
from imbue.mngr_azure.testing import FakeNetworkClient
from imbue.mngr_azure.testing import FakeResourceClient
from imbue.mngr_azure.testing import _StubbedAzureVpsClient
from imbue.mngr_azure.testing import make_azure_http_error
from imbue.mngr_vps_docker.errors import VpsApiError
from imbue.mngr_vps_docker.errors import VpsProvisioningError
from imbue.mngr_vps_docker.primitives import VpsInstanceId
from imbue.mngr_vps_docker.primitives import VpsInstanceStatus

_SUBSCRIPTION = "sub-123"
_REGION = "westus"


def _make_client(
    *,
    compute: FakeComputeClient | None = None,
    network: FakeNetworkClient | None = None,
    resource: FakeResourceClient | None = None,
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


def _orphan_resource(name: str, *, age_seconds: float, attached: bool, attach_attr: str, tagged: bool = True) -> Any:
    created_at = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    tags = (
        {"mngr-provider": "azure", "mngr-created-at": created_at.isoformat(), "managed-by": "mngr"} if tagged else {}
    )
    return SimpleNamespace(
        name=name, tags=tags, **{attach_attr: SimpleNamespace(id="/attached") if attached else None}
    )


def test_create_instance_reclaims_aged_orphan_nic_and_ip() -> None:
    # A NIC + public IP left unattached by an earlier failed create, now older
    # than the reservation window, are reclaimed at the start of the next create.
    network = FakeNetworkClient()
    network.network_interfaces.list_result = [
        _orphan_resource("old-nic", age_seconds=600, attached=False, attach_attr="virtual_machine")
    ]
    network.public_ip_addresses.list_result = [
        _orphan_resource("old-ip", age_seconds=600, attached=False, attach_attr="ip_configuration")
    ]
    client = _make_client(network=network)
    client.upload_ssh_key("k1", "ssh-ed25519 AAAA")
    client.create_instance(
        label="agent",
        region=_REGION,
        plan="Standard_B2s",
        user_data="#cloud-config\n",
        ssh_key_ids=["k1"],
        tags={"mngr-host-id": "host-abc"},
    )
    assert "old-nic" in network.network_interfaces.deleted
    assert "old-ip" in network.public_ip_addresses.deleted


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
    client.upload_ssh_key("k1", "ssh-ed25519 AAAA")
    client.create_instance(
        label="agent",
        region=_REGION,
        plan="Standard_B2s",
        user_data="#cloud-config\n",
        ssh_key_ids=["k1"],
        tags={"mngr-host-id": "host-abc"},
    )
    assert network.network_interfaces.deleted == []


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
