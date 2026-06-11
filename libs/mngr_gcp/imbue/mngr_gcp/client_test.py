"""Tests for the GCP Compute Engine client.

Rather than a botocore-style stubber (which google-cloud-compute does not
provide), these tests inject hand-written fake compute clients at the
``GcpVpsClient`` boundary via the test-only ``_StubbedGcpVpsClient`` subclass.
Each fake records the requests it received and returns canned responses, so the
tests exercise request-building and response-handling without real API calls.
"""

from datetime import datetime
from datetime import timezone

import pytest
from google.api_core import exceptions as google_api_exceptions
from google.auth.credentials import AnonymousCredentials
from google.cloud import compute_v1

from imbue.mngr.errors import MngrError
from imbue.mngr_gcp.client import GcpVpsClient
from imbue.mngr_gcp.client import to_gce_label_value
from imbue.mngr_gcp.testing import FakeFirewallsClient
from imbue.mngr_gcp.testing import FakeInstancesClient
from imbue.mngr_gcp.testing import FakeSnapshotsClient
from imbue.mngr_gcp.testing import _StubbedGcpVpsClient
from imbue.mngr_vps_docker.errors import VpsApiError
from imbue.mngr_vps_docker.errors import VpsProvisioningError
from imbue.mngr_vps_docker.primitives import VpsInstanceId
from imbue.mngr_vps_docker.primitives import VpsInstanceStatus
from imbue.mngr_vps_docker.primitives import VpsSnapshotId


def _present_firewalls() -> FakeFirewallsClient:
    """A FakeFirewallsClient whose rule already exists (the prepared state)."""
    firewalls = FakeFirewallsClient()
    firewalls.existing = compute_v1.Firewall(name="mngr-gcp-ssh")
    return firewalls


def _make_client(
    instances: FakeInstancesClient | None = None,
    firewalls: FakeFirewallsClient | None = None,
    snapshots: FakeSnapshotsClient | None = None,
    *,
    allowed_ssh_cidrs: tuple[str, ...] = ("203.0.113.4/32",),
    auto_shutdown_minutes: int | None = None,
) -> GcpVpsClient:
    # Default to a prepared (existing) firewall so create-path tests don't each
    # have to wire one up; firewall-specific tests pass their own.
    return _StubbedGcpVpsClient(
        credentials=AnonymousCredentials(),
        project_id="test-project",
        zone="us-west1-a",
        image="projects/debian-cloud/global/images/family/debian-12",
        machine_type="e2-small",
        allowed_ssh_cidrs=allowed_ssh_cidrs,
        auto_shutdown_minutes=auto_shutdown_minutes,
        stubbed_instances_client=instances or FakeInstancesClient(),
        stubbed_firewalls_client=firewalls if firewalls is not None else _present_firewalls(),
        stubbed_snapshots_client=snapshots or FakeSnapshotsClient(),
    )


def _running_instance(name: str = "mngr-test-host", nat_ip: str = "") -> compute_v1.Instance:
    access_configs = [compute_v1.AccessConfig(nat_i_p=nat_ip)] if nat_ip else []
    return compute_v1.Instance(
        name=name,
        status="RUNNING",
        network_interfaces=[compute_v1.NetworkInterface(access_configs=access_configs)],
    )


# =============================================================================
# Label / name sanitization
# =============================================================================


def test_to_gce_label_value_lowercases_and_replaces() -> None:
    assert to_gce_label_value("MyProvider") == "myprovider"
    assert to_gce_label_value("host-ABC_123") == "host-abc_123"
    assert to_gce_label_value("has space!") == "has-space-"


def test_to_gce_label_value_truncates_to_63() -> None:
    assert len(to_gce_label_value("a" * 100)) == 63


# =============================================================================
# create_instance
# =============================================================================


def test_create_instance_builds_expected_resource() -> None:
    instances = FakeInstancesClient()
    client = _make_client(instances)
    client.upload_ssh_key("key-1", "ssh-ed25519 AAAA test")

    instance_id = client.create_instance(
        label="mngr-my-agent",
        region="us-west1-a",
        plan="e2-medium",
        user_data="#cloud-config\n",
        ssh_key_ids=["key-1"],
        tags={"mngr-provider": "gcp", "mngr-host-id": "host-abcdef0123456789abcdef0123456789"},
    )
    assert len(instances.inserted) == 1
    built = instances.inserted[0]
    # Name is derived from the label stem + host-id hex suffix.
    assert built.name == str(instance_id)
    assert built.name.startswith("mngr-my-agent-")
    assert "e2-medium" in built.machine_type
    assert built.tags.items == ["mngr-ssh"]
    # Metadata carries user-data, oslogin/block-project-keys, and ssh-keys.
    metadata = {item.key: item.value for item in built.metadata.items}
    assert metadata["user-data"] == "#cloud-config\n"
    assert metadata["enable-oslogin"] == "FALSE"
    assert metadata["block-project-ssh-keys"] == "TRUE"
    assert metadata["ssh-keys"] == "ubuntu:ssh-ed25519 AAAA test"
    # Labels round-trip the provider/host tags (sanitized) plus created-at.
    assert built.labels["mngr-provider"] == "gcp"
    assert "mngr-created-at" in built.labels
    # External IP requested by default.
    assert built.network_interfaces[0].access_configs[0].type_ == "ONE_TO_ONE_NAT"


def test_create_instance_resolves_firewall_without_creating() -> None:
    firewalls = _present_firewalls()
    instances = FakeInstancesClient()
    client = _make_client(instances, firewalls=firewalls)
    client.upload_ssh_key("key-1", "ssh-ed25519 AAAA test")
    client.create_instance(
        label="mngr-host",
        region="us-west1-a",
        plan="e2-small",
        user_data="x",
        ssh_key_ids=["key-1"],
        tags={"mngr-host-id": "host-00000000000000000000000000000000"},
    )
    # Hot path only resolves (read-only) -- it must NOT create the firewall.
    assert firewalls.inserted == []
    assert len(instances.inserted) == 1


def test_create_instance_raises_when_firewall_missing() -> None:
    # Firewall absent -> the read-only resolve raises a prepare hint before any
    # instance is created.
    client = _make_client(firewalls=FakeFirewallsClient())
    client.upload_ssh_key("key-1", "ssh-ed25519 AAAA test")
    with pytest.raises(MngrError, match="mngr gcp prepare"):
        client.create_instance(
            label="mngr-host",
            region="us-west1-a",
            plan="e2-small",
            user_data="x",
            ssh_key_ids=["key-1"],
            tags={"mngr-host-id": "host-00000000000000000000000000000000"},
        )


def test_create_instance_sets_auto_delete_scheduling() -> None:
    instances = FakeInstancesClient()
    client = _make_client(instances, auto_shutdown_minutes=60)
    client.upload_ssh_key("key-1", "pub")
    client.create_instance(
        label="mngr-host",
        region="us-west1-a",
        plan="e2-small",
        user_data="x",
        ssh_key_ids=["key-1"],
        tags={"mngr-host-id": "host-00000000000000000000000000000000"},
    )
    scheduling = instances.inserted[0].scheduling
    assert scheduling.instance_termination_action == "DELETE"
    assert scheduling.max_run_duration.seconds == 3600


def test_create_instance_cross_zone_raises() -> None:
    client = _make_client()
    with pytest.raises(VpsApiError, match="Cross-zone create not supported"):
        client.create_instance(
            label="mngr-host",
            region="us-central1-a",
            plan="e2-small",
            user_data="x",
            ssh_key_ids=[],
            tags={},
        )


def test_create_instance_translates_api_error() -> None:
    instances = FakeInstancesClient()
    instances.insert_error = google_api_exceptions.Forbidden("quota exceeded")
    client = _make_client(instances)
    client.upload_ssh_key("key-1", "pub")
    with pytest.raises(VpsApiError, match="quota exceeded"):
        client.create_instance(
            label="mngr-host",
            region="us-west1-a",
            plan="e2-small",
            user_data="x",
            ssh_key_ids=["key-1"],
            tags={"mngr-host-id": "host-00000000000000000000000000000000"},
        )


def test_create_instance_raises_clear_error_for_unknown_ssh_key() -> None:
    """A referenced ssh_key_id absent from the in-memory map raises VpsApiError, not KeyError.

    GCE keeps SSH keys only in per-instance metadata, so the public key must be
    stashed by upload_ssh_key in the same process. A missing id should surface a
    typed, actionable error rather than a bare builtin KeyError.
    """
    client = _make_client()
    # Note: no upload_ssh_key call, so "key-1" is unknown to this client.
    with pytest.raises(VpsApiError, match="No in-memory SSH public key for id 'key-1'"):
        client.create_instance(
            label="mngr-host",
            region="us-west1-a",
            plan="e2-small",
            user_data="x",
            ssh_key_ids=["key-1"],
            tags={"mngr-host-id": "host-00000000000000000000000000000000"},
        )


# =============================================================================
# ensure_firewall
# =============================================================================


def test_ensure_firewall_fails_closed_without_cidrs() -> None:
    client = _make_client(allowed_ssh_cidrs=())
    with pytest.raises(MngrError, match="allowed_ssh_cidrs is empty"):
        client.ensure_firewall()


def test_ensure_firewall_creates_when_missing() -> None:
    firewalls = FakeFirewallsClient()
    client = _make_client(firewalls=firewalls)
    tag = client.ensure_firewall()
    assert tag == "mngr-ssh"
    assert len(firewalls.inserted) == 1
    rule = firewalls.inserted[0]
    assert rule.target_tags == ["mngr-ssh"]
    assert rule.source_ranges == ["203.0.113.4/32"]
    assert rule.allowed[0].I_p_protocol == "tcp"
    assert "22" in rule.allowed[0].ports
    assert "2222" in rule.allowed[0].ports


def test_ensure_firewall_reuses_existing() -> None:
    firewalls = FakeFirewallsClient()
    firewalls.existing = compute_v1.Firewall(name="mngr-gcp-ssh")
    client = _make_client(firewalls=firewalls)
    tag = client.ensure_firewall()
    assert tag == "mngr-ssh"
    assert firewalls.inserted == []


def test_ensure_firewall_tolerates_create_race() -> None:
    firewalls = FakeFirewallsClient()
    firewalls.insert_error = google_api_exceptions.Conflict("already exists")
    client = _make_client(firewalls=firewalls)
    # A concurrent create wins the race -> treated as success, not an error.
    assert client.ensure_firewall() == "mngr-ssh"


def test_resolve_firewall_returns_tag_when_present() -> None:
    client = _make_client(firewalls=_present_firewalls())
    assert client.resolve_firewall() == "mngr-ssh"


def test_resolve_firewall_raises_prepare_hint_when_missing() -> None:
    # Read-only resolve never creates; a missing rule points the user at prepare.
    client = _make_client(firewalls=FakeFirewallsClient())
    with pytest.raises(MngrError, match="mngr gcp prepare"):
        client.resolve_firewall()


def test_delete_firewall_deletes_when_present() -> None:
    """An existing rule is deleted and its name returned (the cleanup happy path)."""
    firewalls = _present_firewalls()
    client = _make_client(firewalls=firewalls)
    assert client.delete_firewall() == "mngr-gcp-ssh"
    assert firewalls.deleted == ["mngr-gcp-ssh"]


def test_delete_firewall_noop_when_missing() -> None:
    """When the rule is already gone, delete is skipped and None is returned (idempotent)."""
    firewalls = FakeFirewallsClient()
    client = _make_client(firewalls=firewalls)
    assert client.delete_firewall() is None
    assert firewalls.deleted == []


def test_delete_firewall_tolerates_concurrent_delete() -> None:
    """A 404 on the delete (another cleanup won the race) is idempotent success."""
    firewalls = _present_firewalls()
    firewalls.delete_error = google_api_exceptions.NotFound("firewall already gone")
    client = _make_client(firewalls=firewalls)
    assert client.delete_firewall() == "mngr-gcp-ssh"


# =============================================================================
# destroy / status / ip / list
# =============================================================================


def test_destroy_instance() -> None:
    instances = FakeInstancesClient()
    client = _make_client(instances)
    client.destroy_instance(VpsInstanceId("mngr-host-1"))
    assert instances.deleted == ["mngr-host-1"]


def test_destroy_instance_tolerates_already_gone() -> None:
    instances = FakeInstancesClient()
    instances.delete_error = google_api_exceptions.NotFound("instance gone")
    client = _make_client(instances)
    # 404 on delete is idempotent success, not an error.
    client.destroy_instance(VpsInstanceId("mngr-host-1"))


@pytest.mark.parametrize(
    ("gce_status", "expected"),
    [
        ("PROVISIONING", VpsInstanceStatus.PENDING),
        ("STAGING", VpsInstanceStatus.PENDING),
        ("RUNNING", VpsInstanceStatus.ACTIVE),
        ("STOPPING", VpsInstanceStatus.HALTED),
        ("TERMINATED", VpsInstanceStatus.HALTED),
        ("SUSPENDED", VpsInstanceStatus.HALTED),
        ("DEPROVISIONING", VpsInstanceStatus.DESTROYING),
        ("REPAIRING", VpsInstanceStatus.UNKNOWN),
    ],
)
def test_get_instance_status_mapping(gce_status: str, expected: VpsInstanceStatus) -> None:
    instances = FakeInstancesClient()
    instances.get_result = compute_v1.Instance(name="i", status=gce_status)
    client = _make_client(instances)
    assert client.get_instance_status(VpsInstanceId("i")) == expected


def test_get_instance_status_not_found_returns_unknown() -> None:
    instances = FakeInstancesClient()
    instances.get_error = google_api_exceptions.NotFound("gone")
    client = _make_client(instances)
    assert client.get_instance_status(VpsInstanceId("i")) == VpsInstanceStatus.UNKNOWN


def test_get_instance_status_other_error_surfaces() -> None:
    instances = FakeInstancesClient()
    instances.get_error = google_api_exceptions.Forbidden("denied")
    client = _make_client(instances)
    with pytest.raises(VpsApiError, match="denied"):
        client.get_instance_status(VpsInstanceId("i"))


def test_get_instance_ip() -> None:
    instances = FakeInstancesClient()
    instances.get_result = _running_instance(nat_ip="1.2.3.4")
    client = _make_client(instances)
    assert client.get_instance_ip(VpsInstanceId("i")) == "1.2.3.4"


def test_get_instance_ip_not_ready() -> None:
    instances = FakeInstancesClient()
    instances.get_result = _running_instance(nat_ip="")
    client = _make_client(instances)
    with pytest.raises(VpsProvisioningError):
        client.get_instance_ip(VpsInstanceId("i"))


def test_list_instances_filters_and_normalizes() -> None:
    instances = FakeInstancesClient()
    listed = compute_v1.Instance(
        name="mngr-host-1",
        status="RUNNING",
        labels={"mngr-provider": "gcp", "mngr-host-id": "host-1"},
        network_interfaces=[compute_v1.NetworkInterface(access_configs=[compute_v1.AccessConfig(nat_i_p="10.0.0.1")])],
    )
    instances.list_result = [listed]
    client = _make_client(instances)
    result = client.list_instances(provider_tag="gcp")
    assert instances.last_list_filter == "labels.mngr-provider=gcp"
    assert len(result) == 1
    assert result[0]["id"] == "mngr-host-1"
    assert result[0]["main_ip"] == "10.0.0.1"
    assert result[0]["state"] == "RUNNING"
    assert "mngr-provider=gcp" in result[0]["tags"]


def test_list_instances_translates_api_error() -> None:
    instances = FakeInstancesClient()
    instances.list_error = google_api_exceptions.Forbidden("not authorized")
    client = _make_client(instances)
    with pytest.raises(VpsApiError, match="not authorized"):
        client.list_instances(provider_tag="gcp")


def test_list_mngr_managed_instances_spans_zones_and_filters_by_label() -> None:
    """Aggregated across zones; only instances carrying the mngr-provider label count."""
    managed_west = compute_v1.Instance(name="mngr-host-west", status="RUNNING", labels={"mngr-provider": "gcp"})
    managed_central = compute_v1.Instance(
        name="mngr-host-central", status="TERMINATED", labels={"mngr-provider": "gcp-central"}
    )
    unmanaged = compute_v1.Instance(name="someone-elses-vm", status="RUNNING", labels={"team": "data"})
    instances = FakeInstancesClient()
    instances.aggregated_result = [
        ("zones/us-west1-a", [managed_west, unmanaged]),
        ("zones/us-central1-b", [managed_central]),
    ]
    client = _make_client(instances)

    result = client.list_mngr_managed_instances()

    # The unlabeled VM is excluded; both mngr-managed instances are returned with
    # their zone (prefix stripped) regardless of which zone they live in.
    assert result == [
        {"id": "mngr-host-west", "state": "RUNNING", "zone": "us-west1-a"},
        {"id": "mngr-host-central", "state": "TERMINATED", "zone": "us-central1-b"},
    ]


def test_list_mngr_managed_instances_empty_when_none_managed() -> None:
    instances = FakeInstancesClient()
    instances.aggregated_result = [
        ("zones/us-west1-a", [compute_v1.Instance(name="other", status="RUNNING", labels={})]),
    ]
    client = _make_client(instances)
    assert client.list_mngr_managed_instances() == []


def test_list_mngr_managed_instances_translates_api_error() -> None:
    instances = FakeInstancesClient()
    instances.aggregated_list_error = google_api_exceptions.Forbidden("not authorized")
    client = _make_client(instances)
    with pytest.raises(VpsApiError, match="not authorized"):
        client.list_mngr_managed_instances()


# =============================================================================
# SSH keys (in-memory map; no native GCE resource)
# =============================================================================


def test_ssh_key_lifecycle_in_memory() -> None:
    client = _make_client()
    assert client.upload_ssh_key("k1", "pub1") == "k1"
    assert client.upload_ssh_key("k2", "pub2") == "k2"
    keys = client.list_ssh_keys()
    assert {k.id for k in keys} == {"k1", "k2"}
    client.delete_ssh_key("k1")
    assert {k.id for k in client.list_ssh_keys()} == {"k2"}
    # Deleting an absent key is a tolerant no-op (fresh-process delete).
    client.delete_ssh_key("nonexistent")


# =============================================================================
# Snapshots
# =============================================================================


def test_create_snapshot() -> None:
    instances = FakeInstancesClient()
    instances.get_result = compute_v1.Instance(
        name="i",
        disks=[compute_v1.AttachedDisk(boot=True, source="projects/p/zones/us-west1-a/disks/i")],
    )
    snapshots = FakeSnapshotsClient()
    client = _make_client(instances, snapshots=snapshots)
    snapshot_id = client.create_snapshot(VpsInstanceId("i"), "my snapshot")
    assert len(snapshots.inserted) == 1
    assert snapshots.inserted[0].source_disk == "projects/p/zones/us-west1-a/disks/i"
    assert str(snapshot_id) == snapshots.inserted[0].name


def test_delete_snapshot() -> None:
    snapshots = FakeSnapshotsClient()
    client = _make_client(snapshots=snapshots)
    client.delete_snapshot(VpsSnapshotId("mngr-snap-1"))
    assert snapshots.deleted == ["mngr-snap-1"]


def test_list_snapshots() -> None:
    snapshots = FakeSnapshotsClient()
    snapshots.list_result = [
        compute_v1.Snapshot(
            name="mngr-snap-1",
            description="test snapshot",
            creation_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
        )
    ]
    client = _make_client(snapshots=snapshots)
    result = client.list_snapshots()
    assert len(result) == 1
    assert result[0].id == VpsSnapshotId("mngr-snap-1")
    assert result[0].description == "test snapshot"
