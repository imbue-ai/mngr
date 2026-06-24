"""Tests for the GCP Compute Engine client.

Rather than a botocore-style stubber (which google-cloud-compute does not
provide), these tests inject hand-written fake compute clients at the
``GcpVpsClient`` boundary via the test-only ``_StubbedGcpVpsClient`` subclass.
Each fake records the requests it received and returns canned responses, so the
tests exercise request-building and response-handling without real API calls.
"""

from datetime import datetime

import pytest
from google.api_core import exceptions as google_api_exceptions
from google.auth.credentials import AnonymousCredentials
from google.cloud import compute_v1

from imbue.mngr.errors import MngrError
from imbue.mngr_gcp.client import GceInstanceName
from imbue.mngr_gcp.client import GceLabelValue
from imbue.mngr_gcp.client import GcpVpsClient
from imbue.mngr_gcp.client import HOST_NAME_METADATA_KEY
from imbue.mngr_gcp.client import ISOLATION_METADATA_KEY
from imbue.mngr_gcp.client import _make_instance_name
from imbue.mngr_gcp.client import to_gce_label_value
from imbue.mngr_gcp.errors import InvalidGceIdentifierError
from imbue.mngr_gcp.testing import FakeFirewallsClient
from imbue.mngr_gcp.testing import FakeInstancesClient
from imbue.mngr_gcp.testing import _StubbedGcpVpsClient
from imbue.mngr_vps.errors import VpsApiError
from imbue.mngr_vps.errors import VpsProvisioningError
from imbue.mngr_vps.primitives import VpsInstanceId
from imbue.mngr_vps.primitives import VpsInstanceStatus


def _present_firewalls() -> FakeFirewallsClient:
    """A FakeFirewallsClient whose rule already exists (the prepared state)."""
    firewalls = FakeFirewallsClient()
    firewalls.existing = compute_v1.Firewall(name="mngr-gcp-ssh")
    return firewalls


def _make_client(
    instances: FakeInstancesClient | None = None,
    firewalls: FakeFirewallsClient | None = None,
    *,
    allowed_ssh_cidrs: tuple[str, ...] = ("203.0.113.4/32",),
    auto_shutdown_seconds: int | None = None,
    image: str | None = "projects/debian-cloud/global/images/family/debian-12",
) -> GcpVpsClient:
    # Default to a prepared (existing) firewall so create-path tests don't each
    # have to wire one up; firewall-specific tests pass their own.
    return _StubbedGcpVpsClient(
        credentials=AnonymousCredentials(),
        project_id="test-project",
        zone="us-west1-a",
        image=image,
        machine_type="e2-small",
        allowed_ssh_cidrs=allowed_ssh_cidrs,
        auto_shutdown_seconds=auto_shutdown_seconds,
        stubbed_instances_client=instances or FakeInstancesClient(),
        stubbed_firewalls_client=firewalls if firewalls is not None else _present_firewalls(),
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


def test_to_gce_label_value_returns_validated_type() -> None:
    """The coercion output is a GceLabelValue, not a bare str, so its validity is type-asserted."""
    assert isinstance(to_gce_label_value("MyProvider"), GceLabelValue)


def test_gce_label_value_rejects_empty_and_invalid() -> None:
    """Constructing the type directly fails fast on empty or out-of-charset input.

    Guards the latent edge where an all-invalid or empty coercion input would
    otherwise yield an invalid empty label silently shipped to GCE.
    """
    with pytest.raises(InvalidGceIdentifierError):
        GceLabelValue("")
    with pytest.raises(InvalidGceIdentifierError):
        GceLabelValue("has space")
    with pytest.raises(InvalidGceIdentifierError):
        GceLabelValue("a" * 64)


def test_make_instance_name_is_valid_and_typed() -> None:
    """_make_instance_name yields a well-formed, RFC1035-valid GceInstanceName."""
    name = _make_instance_name("My Agent!", {"mngr-host-id": "host-abc123def456"})
    assert isinstance(name, GceInstanceName)
    assert name[0].isalpha()
    assert len(name) <= 63


def test_gce_instance_name_rejects_invalid() -> None:
    """The instance-name type rejects strings that violate RFC1035."""
    with pytest.raises(InvalidGceIdentifierError):
        GceInstanceName("1-starts-with-digit")
    with pytest.raises(InvalidGceIdentifierError):
        GceInstanceName("ends-with-dash-")
    with pytest.raises(InvalidGceIdentifierError):
        GceInstanceName("Has-Upper")


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
        user_data="#!/bin/bash\n",
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
    # Bootstrap is the GCE startup-script (not cloud-init user-data); metadata also
    # carries oslogin/block-project-keys and ssh-keys.
    metadata = {item.key: item.value for item in built.metadata.items}
    assert metadata["startup-script"] == "#!/bin/bash\n"
    assert "user-data" not in metadata
    assert metadata["enable-oslogin"] == "FALSE"
    assert metadata["block-project-ssh-keys"] == "TRUE"
    assert metadata["ssh-keys"] == "ubuntu:ssh-ed25519 AAAA test"
    # mngr host identity lives in metadata: host id verbatim and created-at as ISO-8601.
    assert metadata["mngr-host-id"] == "host-abcdef0123456789abcdef0123456789"
    assert datetime.fromisoformat(metadata["mngr-created-at"]).tzinfo is not None
    assert metadata[HOST_NAME_METADATA_KEY] == "mngr-my-agent"
    # The only mngr label is mngr-provider (sanitized), the server-side discovery filter.
    assert built.labels["mngr-provider"] == "gcp"
    assert "mngr-host-id" not in built.labels
    assert "mngr-created-at" not in built.labels
    # No mngr-isolation tag passed in -> no isolation metadata stamped.
    assert ISOLATION_METADATA_KEY not in metadata
    # External IP requested by default.
    assert built.network_interfaces[0].access_configs[0].type_ == "ONE_TO_ONE_NAT"


def test_create_instance_stamps_isolation_marker_into_metadata() -> None:
    """The ``mngr-isolation`` placement marker rides in GCE metadata (not a label).

    GCP stores mngr identity in metadata (labels are too restricted), so offline
    discovery reads the placement back from metadata to pick the bare realizer for
    a STOPPED instance without SSH.
    """
    instances = FakeInstancesClient()
    client = _make_client(instances)
    client.upload_ssh_key("key-1", "ssh-ed25519 AAAA test")
    client.create_instance(
        label="mngr-bare-agent",
        region="us-west1-a",
        plan="e2-medium",
        user_data="#!/bin/bash\n",
        ssh_key_ids=["key-1"],
        tags={"mngr-provider": "gcp", "mngr-host-id": "host-1", ISOLATION_METADATA_KEY: "none"},
    )
    built = instances.inserted[0]
    metadata = {item.key: item.value for item in built.metadata.items}
    assert metadata[ISOLATION_METADATA_KEY] == "none"
    # The marker stays out of labels (which are charset-restricted).
    assert ISOLATION_METADATA_KEY not in built.labels


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
    client = _make_client(instances, auto_shutdown_seconds=3600)
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
    # Without --gcp-spot the provisioning model stays default (on-demand).
    assert scheduling.provisioning_model != "SPOT"


def test_create_instance_spot_sets_provisioning_model() -> None:
    """``spot=True`` launches on GCE Spot capacity, deleting the VM on preemption."""
    instances = FakeInstancesClient()
    client = _make_client(instances)
    client.upload_ssh_key("key-1", "pub")
    client.create_instance(
        label="mngr-host",
        region="us-west1-a",
        plan="e2-small",
        user_data="x",
        ssh_key_ids=["key-1"],
        tags={"mngr-host-id": "host-00000000000000000000000000000000"},
        spot=True,
    )
    scheduling = instances.inserted[0].scheduling
    assert scheduling.provisioning_model == "SPOT"
    # A preempted Spot VM is deleted (not left stopped) -- mngr has no VM-level resume.
    assert scheduling.instance_termination_action == "DELETE"


def test_create_instance_spot_composes_with_auto_shutdown() -> None:
    """spot + auto_shutdown both land on one Scheduling: SPOT model and the max-run-duration deadline."""
    instances = FakeInstancesClient()
    client = _make_client(instances, auto_shutdown_seconds=3600)
    client.upload_ssh_key("key-1", "pub")
    client.create_instance(
        label="mngr-host",
        region="us-west1-a",
        plan="e2-small",
        user_data="x",
        ssh_key_ids=["key-1"],
        tags={"mngr-host-id": "host-00000000000000000000000000000000"},
        spot=True,
    )
    scheduling = instances.inserted[0].scheduling
    assert scheduling.provisioning_model == "SPOT"
    assert scheduling.max_run_duration.seconds == 3600
    assert scheduling.instance_termination_action == "DELETE"


def test_create_instance_uses_configured_image_by_default() -> None:
    instances = FakeInstancesClient()
    client = _make_client(instances)
    client.upload_ssh_key("key-1", "pub")
    client.create_instance(
        label="mngr-host",
        region="us-west1-a",
        plan="e2-small",
        user_data="x",
        ssh_key_ids=["key-1"],
        tags={"mngr-host-id": "host-00000000000000000000000000000000"},
    )
    # With no override the boot disk uses the client's configured image.
    assert (
        instances.inserted[0].disks[0].initialize_params.source_image
        == "projects/debian-cloud/global/images/family/debian-12"
    )


def test_create_instance_image_override_wins_over_configured() -> None:
    """The per-host ``--gcp-image`` override boots from the given image, not the client default."""
    instances = FakeInstancesClient()
    client = _make_client(instances)
    client.upload_ssh_key("key-1", "pub")
    client.create_instance(
        label="mngr-host",
        region="us-west1-a",
        plan="e2-small",
        user_data="x",
        ssh_key_ids=["key-1"],
        tags={"mngr-host-id": "host-00000000000000000000000000000000"},
        image="projects/my-proj/global/images/family/custom",
    )
    assert (
        instances.inserted[0].disks[0].initialize_params.source_image == "projects/my-proj/global/images/family/custom"
    )


def test_create_instance_image_override_works_without_configured_image() -> None:
    """An override lets an image-less client (e.g. operator-style) still create a VM."""
    instances = FakeInstancesClient()
    client = _make_client(instances, image=None)
    client.upload_ssh_key("key-1", "pub")
    client.create_instance(
        label="mngr-host",
        region="us-west1-a",
        plan="e2-small",
        user_data="x",
        ssh_key_ids=["key-1"],
        tags={"mngr-host-id": "host-00000000000000000000000000000000"},
        image="projects/my-proj/global/images/family/custom",
    )
    assert (
        instances.inserted[0].disks[0].initialize_params.source_image == "projects/my-proj/global/images/family/custom"
    )


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


def test_ensure_firewall_skips_rule_and_warns_when_no_cidrs(log_warnings: list[str]) -> None:
    """Empty allowed_ssh_cidrs creates no rule and warns (fail-open, mirrors AWS).

    GCE rejects an INGRESS rule with no source_ranges, so the analog of AWS's
    zero-ingress security group is simply the absence of a rule: ensure_firewall
    returns the target tag without inserting anything and logs a warning. The
    empty case is an "I'll wire my own ingress later" signal, not a fail-closed gate.
    """
    firewalls = FakeFirewallsClient()
    client = _make_client(firewalls=firewalls, allowed_ssh_cidrs=())
    result = client.ensure_firewall()
    assert result.target_tag == "mngr-ssh"
    assert result.was_created is False
    assert firewalls.inserted == []
    assert any("allowed_ssh_cidrs is empty" in msg for msg in log_warnings)


def test_ensure_firewall_warns_when_open_to_internet(log_warnings: list[str]) -> None:
    """0.0.0.0/0 is the default but should still produce a visible warning at prepare time."""
    firewalls = FakeFirewallsClient()
    client = _make_client(firewalls=firewalls, allowed_ssh_cidrs=("0.0.0.0/0",))
    result = client.ensure_firewall()
    assert result.target_tag == "mngr-ssh"
    assert result.was_created is True
    assert firewalls.inserted[0].source_ranges == ["0.0.0.0/0"]
    assert any("0.0.0.0/0" in msg for msg in log_warnings)


def test_ensure_firewall_creates_when_missing() -> None:
    firewalls = FakeFirewallsClient()
    client = _make_client(firewalls=firewalls)
    result = client.ensure_firewall()
    assert result.target_tag == "mngr-ssh"
    assert result.was_created is True
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
    result = client.ensure_firewall()
    assert result.target_tag == "mngr-ssh"
    assert result.was_created is False
    assert firewalls.inserted == []


def test_ensure_firewall_tolerates_create_race() -> None:
    firewalls = FakeFirewallsClient()
    firewalls.insert_error = google_api_exceptions.Conflict("already exists")
    client = _make_client(firewalls=firewalls)
    # A concurrent create wins the race -> treated as success, not an error.
    result = client.ensure_firewall()
    assert result.target_tag == "mngr-ssh"
    # The rule already existed (the racing creator made it), so this call did not create it.
    assert result.was_created is False


def test_resolve_firewall_returns_tag_when_present() -> None:
    client = _make_client(firewalls=_present_firewalls())
    assert client.resolve_firewall() == "mngr-ssh"


def test_resolve_firewall_raises_prepare_hint_when_missing() -> None:
    # Read-only resolve never creates; a missing rule points the user at prepare.
    client = _make_client(firewalls=FakeFirewallsClient())
    with pytest.raises(MngrError, match="mngr gcp prepare"):
        client.resolve_firewall()


def test_resolve_firewall_returns_tag_when_cidrs_empty() -> None:
    """Empty cidrs means no rule is expected, so resolve short-circuits to the tag.

    ensure_firewall / `mngr gcp prepare` creates no rule when cidrs is empty, so
    there is nothing to look up and pointing the user at prepare would be wrong.
    The lookup is skipped entirely (no rule present, yet no raise).
    """
    client = _make_client(firewalls=FakeFirewallsClient(), allowed_ssh_cidrs=())
    assert client.resolve_firewall() == "mngr-ssh"


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
        labels={"mngr-provider": "gcp"},
        metadata=compute_v1.Metadata(items=[compute_v1.Items(key="mngr-host-id", value="host-1")]),
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
    assert result[0]["metadata"]["mngr-host-id"] == "host-1"


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
# stop_instance / start_instance (GCP-only idle-pause + resume)
# =============================================================================


def test_stop_instance_calls_stop_and_polls_to_terminated() -> None:
    """stop_instance issues instances.stop and waits for the terminal TERMINATED status."""
    instances = FakeInstancesClient()
    # The post-stop poll reads the instance status; TERMINATED is GCE's name for a
    # stopped (not deleted) instance.
    instances.get_result = compute_v1.Instance(name="mngr-host-1", status="TERMINATED")
    client = _make_client(instances)
    client.stop_instance(VpsInstanceId("mngr-host-1"))
    assert instances.stopped == ["mngr-host-1"]


def test_stop_instance_times_out_if_not_terminated() -> None:
    """A zero timeout means the wait never observes TERMINATED and raises VpsProvisioningError."""
    instances = FakeInstancesClient()
    instances.get_result = compute_v1.Instance(name="mngr-host-1", status="STOPPING")
    client = _make_client(instances)
    with pytest.raises(VpsProvisioningError, match="did not reach status 'TERMINATED'"):
        client.stop_instance(VpsInstanceId("mngr-host-1"), timeout_seconds=0.0)


def test_start_instance_calls_start_and_returns_external_ip() -> None:
    """start_instance issues instances.start, polls to RUNNING, and returns the fresh external IP."""
    instances = FakeInstancesClient()
    # Already RUNNING with a NAT IP: the status poll and the external-IP poll both
    # read this same instance, so start returns the access config's address.
    instances.get_result = _running_instance(nat_ip="5.6.7.8")
    client = _make_client(instances)
    assert client.start_instance(VpsInstanceId("mngr-host-1")) == "5.6.7.8"
    assert instances.started == ["mngr-host-1"]


def test_start_instance_times_out_if_not_running() -> None:
    """A zero timeout means the RUNNING wait never succeeds and raises."""
    instances = FakeInstancesClient()
    instances.get_result = compute_v1.Instance(name="mngr-host-1", status="STAGING")
    client = _make_client(instances)
    with pytest.raises(VpsProvisioningError, match="did not reach status 'RUNNING'"):
        client.start_instance(VpsInstanceId("mngr-host-1"), timeout_seconds=0.0)


# =============================================================================
# set_instance_metadata / get_instance_metadata (offline-discovery mirror)
# =============================================================================


def test_set_instance_metadata_upsert_merges_with_existing() -> None:
    """An upsert preserves existing items (e.g. startup-script) and adds/overwrites the given keys."""
    instances = FakeInstancesClient()
    instances.get_result = compute_v1.Instance(
        name="mngr-host-1",
        metadata=compute_v1.Metadata(
            fingerprint="fp-1",
            items=[
                compute_v1.Items(key="startup-script", value="#!/bin/bash\n"),
                compute_v1.Items(key="mngr-agent-a-name", value="old"),
            ],
        ),
    )
    client = _make_client(instances)
    client.set_instance_metadata(
        VpsInstanceId("mngr-host-1"), {"mngr-agent-a-name": "new", "mngr-agent-a-type": "command"}
    )
    assert len(instances.set_metadata_calls) == 1
    written = {item.key: item.value for item in instances.set_metadata_calls[0].items}
    # Untouched key preserved, existing key overwritten, new key added.
    assert written["startup-script"] == "#!/bin/bash\n"
    assert written["mngr-agent-a-name"] == "new"
    assert written["mngr-agent-a-type"] == "command"
    # The current fingerprint is echoed back for optimistic concurrency.
    assert instances.set_metadata_calls[0].fingerprint == "fp-1"


def test_set_instance_metadata_delete_removes_key() -> None:
    """A delete drops the named key while leaving the rest of the metadata intact."""
    instances = FakeInstancesClient()
    instances.get_result = compute_v1.Instance(
        name="mngr-host-1",
        metadata=compute_v1.Metadata(
            fingerprint="fp-1",
            items=[
                compute_v1.Items(key="startup-script", value="#!/bin/bash\n"),
                compute_v1.Items(key="mngr-agent-a-name", value="a1"),
            ],
        ),
    )
    client = _make_client(instances)
    client.set_instance_metadata(VpsInstanceId("mngr-host-1"), {}, delete_keys=["mngr-agent-a-name"])
    written = {item.key: item.value for item in instances.set_metadata_calls[0].items}
    assert "mngr-agent-a-name" not in written
    assert written["startup-script"] == "#!/bin/bash\n"


def test_set_instance_metadata_retries_once_on_fingerprint_conflict() -> None:
    """A 412 PRECONDITION_FAILED on the first setMetadata triggers exactly one retry that succeeds.

    GCE setMetadata is a fingerprint-guarded whole-object write; a concurrent
    metadata write between the GET and the setMetadata returns 412. The client
    refetches and retries once, so the upsert still lands.
    """
    instances = FakeInstancesClient()
    instances.get_result = compute_v1.Instance(
        name="mngr-host-1",
        metadata=compute_v1.Metadata(fingerprint="fp-1", items=[]),
    )
    # First setMetadata raises a 412; the second (the single retry) succeeds.
    instances.set_metadata_errors = [google_api_exceptions.PreconditionFailed("fingerprint conflict")]
    client = _make_client(instances)
    client.set_instance_metadata(VpsInstanceId("mngr-host-1"), {"mngr-agent-a-name": "a1"})
    # Two attempts total: the conflicting one and the successful retry.
    assert len(instances.set_metadata_calls) == 2
    written = {item.key: item.value for item in instances.set_metadata_calls[1].items}
    assert written["mngr-agent-a-name"] == "a1"


def test_set_instance_metadata_noop_when_nothing_to_change() -> None:
    """No updates and no deletes means zero API calls (not even a GET)."""
    instances = FakeInstancesClient()
    client = _make_client(instances)
    client.set_instance_metadata(VpsInstanceId("mngr-host-1"), {}, delete_keys=[])
    assert instances.set_metadata_calls == []
    # get_result was never set; a stray GET would have tripped its assertion.


def test_get_instance_metadata_returns_items_dict() -> None:
    instances = FakeInstancesClient()
    instances.get_result = compute_v1.Instance(
        name="mngr-host-1",
        metadata=compute_v1.Metadata(
            items=[
                compute_v1.Items(key="mngr-host-name", value="mngr-myhost"),
                compute_v1.Items(key="mngr-agent-a-name", value="a1"),
            ]
        ),
    )
    client = _make_client(instances)
    assert client.get_instance_metadata(VpsInstanceId("mngr-host-1")) == {
        "mngr-host-name": "mngr-myhost",
        "mngr-agent-a-name": "a1",
    }


def test_get_instance_metadata_returns_empty_when_instance_gone() -> None:
    """A 404 (instance deleted) yields {} rather than surfacing the error."""
    instances = FakeInstancesClient()
    instances.get_error = google_api_exceptions.NotFound("gone")
    client = _make_client(instances)
    assert client.get_instance_metadata(VpsInstanceId("mngr-host-1")) == {}


# =============================================================================
# SSH keys (in-memory map; no native GCE resource)
# =============================================================================


def test_delete_ssh_key_is_tolerant_of_absent_key() -> None:
    client = _make_client()
    client.upload_ssh_key("k1", "pub1")
    client.delete_ssh_key("k1")
    # Deleting an absent key is a tolerant no-op (fresh-process delete).
    client.delete_ssh_key("nonexistent")
