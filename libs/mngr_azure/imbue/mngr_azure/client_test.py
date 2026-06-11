import base64
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from typing import Any

import pytest

from imbue.mngr.errors import MngrError
from imbue.mngr_azure.testing import FakeComputeClient
from imbue.mngr_azure.testing import FakeNetworkClient
from imbue.mngr_azure.testing import FakeResourceClient
from imbue.mngr_azure.testing import _StubbedAzureVpsClient
from imbue.mngr_azure.testing import make_azure_http_error
from imbue.mngr_vps_docker.errors import VpsApiError
from imbue.mngr_vps_docker.errors import VpsProvisioningError
from imbue.mngr_vps_docker.primitives import VpsInstanceId
from imbue.mngr_vps_docker.primitives import VpsInstanceStatus
from imbue.mngr_vps_docker.primitives import VpsSnapshotId

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


def test_ensure_network_fail_closed_when_no_cidrs() -> None:
    client = _make_client(allowed_ssh_cidrs=())
    with pytest.raises(MngrError, match="allowed_ssh_cidrs is empty"):
        client.ensure_network()


def test_ensure_network_creates_rg_nsg_vnet_and_registers_providers() -> None:
    resource = FakeResourceClient()
    resource.providers.registration_state = "NotRegistered"
    client = _make_client(resource=resource)
    returned = client.ensure_network()
    assert returned == "mngr"
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
        SimpleNamespace(name="vm-a", tags={"mngr-provider": "azure"}),
        SimpleNamespace(name="vm-b", tags={"mngr-provider": "other"}),
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
    compute = FakeComputeClient()
    compute.virtual_machines.list_result = [
        SimpleNamespace(name="vm-a", tags={"mngr-provider": "azure-west"}),
        SimpleNamespace(name="vm-b", tags={"managed-by": "mngr"}),
    ]
    client = _make_client(compute=compute)
    managed = client.list_mngr_managed_vms()
    assert [vm["id"] for vm in managed] == ["vm-a"]


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
# destroy / snapshots / ssh keys
# =========================================================================


def test_destroy_instance_idempotent_on_404() -> None:
    compute = FakeComputeClient()
    compute.virtual_machines.delete_error = make_azure_http_error(404, "not found")
    client = _make_client(compute=compute)
    # Should not raise.
    client.destroy_instance(VpsInstanceId("vm1"))


def test_create_snapshot_from_os_disk() -> None:
    compute = FakeComputeClient()
    compute.virtual_machines.get_result = SimpleNamespace(
        storage_profile=SimpleNamespace(os_disk=SimpleNamespace(managed_disk=SimpleNamespace(id="/disks/os")))
    )
    client = _make_client(compute=compute)
    snapshot_id = client.create_snapshot(VpsInstanceId("vm1"), "my snapshot")
    assert len(compute.snapshots.created) == 1
    snapshot = compute.snapshots.created[0][1]
    assert snapshot.creation_data.source_resource_id == "/disks/os"
    assert snapshot.tags["description"] == "my snapshot"
    assert str(snapshot_id).startswith("mngr-snap-")


def test_list_snapshots_round_trips_description() -> None:
    compute = FakeComputeClient()
    compute.snapshots.list_result = [
        SimpleNamespace(
            name="mngr-snap-1",
            tags={"description": "backup"},
            time_created=datetime(2026, 6, 10, tzinfo=timezone.utc),
        )
    ]
    client = _make_client(compute=compute)
    snapshots = client.list_snapshots()
    assert snapshots[0].id == VpsSnapshotId("mngr-snap-1")
    assert snapshots[0].description == "backup"


def test_ssh_key_lifecycle_in_memory() -> None:
    client = _make_client()
    client.upload_ssh_key("k1", "pub1")
    client.upload_ssh_key("k2", "pub2")
    assert {key.id for key in client.list_ssh_keys()} == {"k1", "k2"}
    client.delete_ssh_key("k1")
    assert {key.id for key in client.list_ssh_keys()} == {"k2"}
    # Deleting an absent key is a tolerant no-op.
    client.delete_ssh_key("missing")
