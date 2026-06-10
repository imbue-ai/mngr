import os
import re
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Final
from uuid import uuid4

from google.api_core import exceptions as google_api_exceptions
from google.auth.credentials import Credentials
from google.cloud import compute_v1
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.mngr.errors import MngrError
from imbue.mngr_vps_docker.errors import VpsApiError
from imbue.mngr_vps_docker.errors import VpsProvisioningError
from imbue.mngr_vps_docker.primitives import VpsInstanceId
from imbue.mngr_vps_docker.primitives import VpsInstanceStatus
from imbue.mngr_vps_docker.primitives import VpsSnapshotId
from imbue.mngr_vps_docker.vps_client import VpsClientInterface
from imbue.mngr_vps_docker.vps_client import VpsSnapshotInfo
from imbue.mngr_vps_docker.vps_client import VpsSshKeyInfo

# Label key/value that ``create_instance`` adds to every GCE instance launched
# while ``PYTEST_CURRENT_TEST`` is set. The conftest session-end scanner uses
# this label (not the instance name) to find leaked instances, which means
# tests do not have to constrain host naming: any agent name works.
GCP_PYTEST_LAUNCHED_LABEL: Final[str] = "mngr-pytest-launched"

# SSH metadata is injected as ``<user>:<public-key>``. ``ubuntu`` is the default
# user on the GCE Ubuntu LTS images the provider targets (Ubuntu, not Debian,
# because the stock GCE Debian images do not run cloud-init). The shared
# ``generate_cloud_init_user_data`` writes the provider key straight into root's
# authorized_keys and also copies the ``ubuntu`` user's, so mngr's root SSH works
# regardless of which the guest agent provisions first.
GCE_SSH_USERNAME: Final[str] = "ubuntu"

_STATUS_MAP: Final[dict[str, VpsInstanceStatus]] = {
    "PROVISIONING": VpsInstanceStatus.PENDING,
    "STAGING": VpsInstanceStatus.PENDING,
    "PENDING": VpsInstanceStatus.PENDING,
    "RUNNING": VpsInstanceStatus.ACTIVE,
    "STOPPING": VpsInstanceStatus.HALTED,
    "SUSPENDING": VpsInstanceStatus.HALTED,
    "STOPPED": VpsInstanceStatus.HALTED,
    "SUSPENDED": VpsInstanceStatus.HALTED,
    "TERMINATED": VpsInstanceStatus.HALTED,
    "DEPROVISIONING": VpsInstanceStatus.DESTROYING,
}

# GCE label values must match ``[a-z0-9_-]{0,63}``; keys additionally must start
# with a lowercase letter. mngr tag keys (``mngr-host-id``, ``mngr-provider``)
# already conform; values can carry uppercase (e.g. a mixed-case provider
# instance name), so they are lowercased before use.
_INVALID_LABEL_CHARS_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9_-]")
# RFC1035 instance names: lowercase letter first, then lowercase alphanumerics
# and dashes, ending alphanumeric, 1-63 chars.
_INVALID_NAME_CHARS_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9-]")
_MAX_INSTANCE_NAME_LENGTH: Final[int] = 63
# 32-hex host-id suffix (+ a separating dash) is appended to guarantee
# uniqueness, leaving this many characters for the human-readable label stem.
_INSTANCE_NAME_STEM_LENGTH: Final[int] = _MAX_INSTANCE_NAME_LENGTH - 33


def to_gce_label_value(value: str) -> str:
    """Coerce an arbitrary tag value into a valid GCE label value.

    GCE label values are restricted to ``[a-z0-9_-]`` and 63 chars. The same
    transform is applied at write time (``create_instance`` labels) and at read
    time (the ``list_instances`` label filter) so the round-trip is exact. Two
    provider instances whose names differ only by case would collide here -- an
    acceptable, documented edge (name them distinctly).
    """
    return _INVALID_LABEL_CHARS_RE.sub("-", value.lower())[:63]


def _make_instance_name(label: str, tags: Mapping[str, str]) -> str:
    """Build a unique RFC1035-valid GCE instance name from the label and tags.

    GCE identifies instances by name (used for every get/delete/operation), so
    the name must be valid and unique. The human-readable ``label`` stem is
    sanitized and a 32-hex host-id suffix (from the ``mngr-host-id`` tag) is
    appended for uniqueness; absent that tag (direct-client use), a fresh uuid
    is used instead.
    """
    stem = _INVALID_NAME_CHARS_RE.sub("-", label.lower()).strip("-")[:_INSTANCE_NAME_STEM_LENGTH].strip("-")
    if not stem or not stem[0].isalpha():
        stem = f"mngr-{stem}" if stem else "mngr"
    host_id = tags.get("mngr-host-id", "")
    suffix = host_id.lower().rsplit("-", 1)[-1] if host_id else uuid4().hex
    suffix = _INVALID_NAME_CHARS_RE.sub("", suffix)
    return f"{stem}-{suffix}"[:_MAX_INSTANCE_NAME_LENGTH].rstrip("-")


class GcpVpsClient(VpsClientInterface):
    """GCE client implementing the VPS provider interface via google-cloud-compute.

    Bound at construction to a single ``project_id`` + ``zone`` (GCE VMs are
    zonal), analogous to ``AwsVpsClient`` being bound to a region. To target a
    different zone, instantiate a separate client.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # GCE boots are routinely faster than EC2 (Debian + Docker install on an
    # e2-small typically lands well under a minute), so the "slow" threshold is
    # lower than AWS's 90s.
    slow_provisioning_warning_threshold_seconds: float = Field(default=45.0)

    credentials: Credentials = Field(frozen=True, description="Resolved ADC credentials for the compute clients")
    project_id: str = Field(frozen=True, description="GCP project this client targets")
    zone: str = Field(frozen=True, description="GCE zone this client targets")
    image: str = Field(frozen=True, description="Default source image for boot disks created via this client")
    machine_type: str = Field(default="e2-small", description="Default machine type for instances")
    boot_disk_size_gb: int = Field(default=30, description="Boot disk size in GB")
    boot_disk_type: str = Field(default="pd-balanced", description="Boot disk type")
    network: str = Field(default="default", description="VPC network name")
    subnetwork: str | None = Field(default=None, description="Subnetwork name, or None to let GCE pick")
    allowed_ssh_cidrs: tuple[str, ...] = Field(
        default=(),
        description=(
            "CIDR blocks allowed inbound on tcp/22 and tcp/container_ssh_port of the auto-created "
            "firewall rule. Empty by default (fail-closed): ensure_firewall raises rather than creating "
            "a wide-open rule. Set to e.g. ('203.0.113.4/32',) to restrict to your own IP, or "
            "('0.0.0.0/0',) to expose to the public internet (NOT recommended for production)."
        ),
    )
    firewall_name: str = Field(default="mngr-gcp-ssh", description="Name of the auto-created SSH firewall rule")
    firewall_target_tag: str = Field(default="mngr-ssh", description="Network tag the firewall rule targets")
    associate_external_ip: bool = Field(default=True, description="Assign an ephemeral external IPv4 to instances")
    service_account_email: str | None = Field(default=None, description="Service account email to attach, or None")
    service_account_scopes: tuple[str, ...] = Field(
        default=("https://www.googleapis.com/auth/cloud-platform",),
        description="OAuth scopes for the attached service account",
    )
    auto_shutdown_minutes: int | None = Field(
        default=None,
        description=(
            "When set, instances are launched with scheduling.max_run_duration + "
            "instance_termination_action=DELETE so the VM self-deletes after N minutes -- the "
            "GCE-native analog of AWS InstanceInitiatedShutdownBehavior=terminate."
        ),
    )
    container_ssh_port: int = Field(
        default=2222, description="Port the container's sshd is exposed on (added to the firewall rule)"
    )

    # There is no per-key GCE resource (unlike an EC2 KeyPair); the public key
    # lives only in per-instance metadata. This in-memory map bridges the
    # base flow's upload_ssh_key -> create_instance(ssh_key_ids=[...]) handoff
    # within a single process. A later fresh-process delete is a tolerant no-op.
    _ssh_public_keys_by_id: dict[str, str] = PrivateAttr(default_factory=dict)
    _cached_instances_client: Any = PrivateAttr(default=None)
    _cached_firewalls_client: Any = PrivateAttr(default=None)
    _cached_snapshots_client: Any = PrivateAttr(default=None)

    # =========================================================================
    # Lazily-built compute clients (overridden in tests to inject fakes)
    # =========================================================================

    def _instances(self) -> Any:
        if self._cached_instances_client is None:
            self._cached_instances_client = compute_v1.InstancesClient(credentials=self.credentials)
        return self._cached_instances_client

    def _firewalls(self) -> Any:
        if self._cached_firewalls_client is None:
            self._cached_firewalls_client = compute_v1.FirewallsClient(credentials=self.credentials)
        return self._cached_firewalls_client

    def _snapshots(self) -> Any:
        if self._cached_snapshots_client is None:
            self._cached_snapshots_client = compute_v1.SnapshotsClient(credentials=self.credentials)
        return self._cached_snapshots_client

    @contextmanager
    def _translate_gcp_errors(self) -> Iterator[None]:
        """Translate ``google.api_core.exceptions.GoogleAPICallError`` into ``VpsApiError``."""
        try:
            yield
        except google_api_exceptions.GoogleAPICallError as e:
            status_code = e.code if isinstance(e.code, int) else 0
            raise VpsApiError(status_code, e.message or str(e)) from e

    def _region(self) -> str:
        """Derive the region from the bound zone (``us-west1-a`` -> ``us-west1``)."""
        return self.zone.rsplit("-", 1)[0]

    def _await_operation(self, operation: Any) -> None:
        """Block until an extended operation completes, translating any failure."""
        with self._translate_gcp_errors():
            operation.result()

    # =========================================================================
    # Firewall management (idempotent, tag-targeted, network-scoped)
    # =========================================================================

    def _firewall_exists(self) -> bool:
        """Return True iff the configured firewall rule exists (read-only lookup)."""
        try:
            with self._translate_gcp_errors():
                self._firewalls().get(project=self.project_id, firewall=self.firewall_name)
        except VpsApiError as e:
            if e.status_code == 404:
                return False
            raise
        return True

    def ensure_firewall(self) -> str:
        """Ensure the SSH firewall rule exists, creating it if absent. Returns the target tag.

        GCE firewalls are network-scoped and tag-targeted (not per-instance
        like an EC2 security group). One rule named ``firewall_name`` allows
        tcp/22 + tcp/``container_ssh_port`` from every CIDR in
        ``allowed_ssh_cidrs`` to instances carrying ``firewall_target_tag``.

        Fails closed if ``allowed_ssh_cidrs`` is empty: rather than create a
        wide-open rule, raise so the caller makes an explicit decision. A
        pre-existing rule is reused as-is (ingress is not re-patched); delete it
        manually to change the allowed CIDRs.

        This is the privileged write path, used by ``mngr gcp prepare`` (one-time
        admin setup). The hot path in ``create_instance`` uses
        ``resolve_firewall`` instead, which is lookup-only and needs only
        instance-create permissions (no ``compute.firewalls.create``).
        """
        if not self.allowed_ssh_cidrs:
            raise MngrError(
                "Cannot auto-create a GCP firewall rule: allowed_ssh_cidrs is empty. "
                "Set allowed_ssh_cidrs to a tuple of CIDR blocks (e.g. ('203.0.113.4/32',) for your "
                "own IP), or pre-create the firewall rule targeting the configured firewall_target_tag."
            )
        if self._firewall_exists():
            return self.firewall_target_tag

        firewall = compute_v1.Firewall(
            name=self.firewall_name,
            network=f"projects/{self.project_id}/global/networks/{self.network}",
            direction="INGRESS",
            source_ranges=list(self.allowed_ssh_cidrs),
            target_tags=[self.firewall_target_tag],
            allowed=[compute_v1.Allowed(I_p_protocol="tcp", ports=["22", str(self.container_ssh_port)])],
            description="Auto-created by mngr_gcp for SSH access to managed instances",
        )
        try:
            with self._translate_gcp_errors():
                operation = self._firewalls().insert(project=self.project_id, firewall_resource=firewall)
                operation.result()
        except VpsApiError as e:
            # A concurrent create (another host provisioning in parallel) wins
            # the race; the rule now exists, which is exactly what we wanted.
            if e.status_code != 409:
                raise
        logger.info(
            "Ensured firewall rule {} (tag {}) in project {}",
            self.firewall_name,
            self.firewall_target_tag,
            self.project_id,
        )
        return self.firewall_target_tag

    def resolve_firewall(self) -> str:
        """Look up the firewall rule without creating or modifying it. Returns the target tag.

        Mirrors ``ensure_firewall`` but with no write API calls -- the hot
        ``create_instance`` path needs only instance-create permissions. When
        the rule is missing, raises a ``MngrError`` pointing at
        ``mngr gcp prepare`` so a user with restricted IAM gets a clear next
        step rather than an opaque permission denial when the instance later
        proves unreachable.
        """
        if self._firewall_exists():
            return self.firewall_target_tag
        raise MngrError(
            f"GCP firewall rule {self.firewall_name!r} does not exist in project {self.project_id!r}. "
            f"Run `mngr gcp prepare --project {self.project_id}` once to create it "
            "(needs compute.firewalls.create), then retry the create with your usual "
            "instance-create-only credentials. The rule targets the configured firewall_target_tag "
            f"({self.firewall_target_tag!r}); every instance is tagged with it."
        )

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
    ) -> VpsInstanceId:
        """Provision a GCE instance in the client's bound zone.

        ``region`` is interpreted as the **zone** for GCP (GCE VMs are zonal);
        it must equal the zone this client is bound to.
        """
        if region != self.zone:
            raise VpsApiError(
                400,
                f"Cross-zone create not supported: client bound to zone {self.zone!r}, "
                f"got region={region!r} (for GCP, --gcp-zone is the placement knob). Instantiate a zone-specific client.",
            )

        # Read-only firewall resolve on the hot path (no compute.firewalls.create
        # needed); the privileged create lives in `mngr gcp prepare`.
        self.resolve_firewall()

        instance_name = _make_instance_name(label, tags)
        # GCE keeps SSH keys only in per-instance metadata (no provider key
        # resource), so the public key must have been stashed in this process by
        # a prior upload_ssh_key call. A missing id means the caller broke that
        # in-process handoff; surface a typed error rather than a bare KeyError.
        ssh_metadata_lines: list[str] = []
        for key_id in ssh_key_ids:
            public_key = self._ssh_public_keys_by_id.get(key_id)
            if public_key is None:
                raise VpsApiError(
                    400,
                    f"No in-memory SSH public key for id {key_id!r}; upload_ssh_key must be called "
                    "in the same process before create_instance (GCE keeps keys only in per-instance "
                    "metadata, not as a provider resource).",
                )
            ssh_metadata_lines.append(f"{GCE_SSH_USERNAME}:{public_key}")
        ssh_metadata_value = "\n".join(ssh_metadata_lines)
        metadata_items = [
            compute_v1.Items(key="user-data", value=user_data),
            # Disable OS Login and project-wide SSH keys so only the per-instance
            # ssh-keys metadata grants access (no inherited project keys).
            compute_v1.Items(key="enable-oslogin", value="FALSE"),
            compute_v1.Items(key="block-project-ssh-keys", value="TRUE"),
        ]
        if ssh_metadata_value:
            metadata_items.append(compute_v1.Items(key="ssh-keys", value=ssh_metadata_value))

        labels = {to_gce_label_value(k): to_gce_label_value(v) for k, v in tags.items()}
        labels["mngr-created-at"] = to_gce_label_value(datetime.now(timezone.utc).strftime("%Y-%m-%dt%H-%M-%S"))
        # Mark instances launched during pytest so the conftest session-end
        # orphan scanner can identify and force-delete any leaks without having
        # to constrain the agent / host name shape.
        if "PYTEST_CURRENT_TEST" in os.environ:
            labels[GCP_PYTEST_LAUNCHED_LABEL] = "true"

        network_interface = compute_v1.NetworkInterface(
            network=f"projects/{self.project_id}/global/networks/{self.network}",
        )
        if self.subnetwork is not None:
            network_interface.subnetwork = (
                f"projects/{self.project_id}/regions/{self._region()}/subnetworks/{self.subnetwork}"
            )
        if self.associate_external_ip:
            network_interface.access_configs = [compute_v1.AccessConfig(name="External NAT", type_="ONE_TO_ONE_NAT")]

        instance = compute_v1.Instance(
            name=instance_name,
            machine_type=f"projects/{self.project_id}/zones/{self.zone}/machineTypes/{plan}",
            disks=[
                compute_v1.AttachedDisk(
                    boot=True,
                    auto_delete=True,
                    initialize_params=compute_v1.AttachedDiskInitializeParams(
                        source_image=self.image,
                        disk_size_gb=self.boot_disk_size_gb,
                        disk_type=f"projects/{self.project_id}/zones/{self.zone}/diskTypes/{self.boot_disk_type}",
                    ),
                )
            ],
            network_interfaces=[network_interface],
            metadata=compute_v1.Metadata(items=metadata_items),
            labels=labels,
            tags=compute_v1.Tags(items=[self.firewall_target_tag]),
        )
        if self.service_account_email is not None:
            instance.service_accounts = [
                compute_v1.ServiceAccount(email=self.service_account_email, scopes=list(self.service_account_scopes))
            ]
        if self.auto_shutdown_minutes is not None and self.auto_shutdown_minutes > 0:
            # GCE-native auto-delete: the VM self-deletes after the deadline even
            # if the orchestrating process is killed. The analog of AWS
            # InstanceInitiatedShutdownBehavior=terminate.
            instance.scheduling = compute_v1.Scheduling(
                max_run_duration=compute_v1.Duration(seconds=self.auto_shutdown_minutes * 60),
                instance_termination_action="DELETE",
            )

        with self._translate_gcp_errors():
            operation = self._instances().insert(project=self.project_id, zone=self.zone, instance_resource=instance)
        self._await_operation(operation)
        logger.info(
            "Created GCE instance {} (label: {}, zone: {}, machine_type: {}, image: {})",
            instance_name,
            label,
            self.zone,
            plan,
            self.image,
        )
        return VpsInstanceId(instance_name)

    def destroy_instance(self, instance_id: VpsInstanceId) -> None:
        try:
            with self._translate_gcp_errors():
                operation = self._instances().delete(
                    project=self.project_id, zone=self.zone, instance=str(instance_id)
                )
            self._await_operation(operation)
        except VpsApiError as e:
            # Already gone (self-deleted via max_run_duration, or deleted by a
            # prior call) -- destroy is idempotent, so treat as success.
            if e.status_code != 404:
                raise
            logger.info("GCE instance {} already gone; treating destroy as success", instance_id)
            return
        logger.info("Deleted GCE instance {}", instance_id)

    def _get_instance(self, instance_id: VpsInstanceId) -> Any:
        with self._translate_gcp_errors():
            return self._instances().get(project=self.project_id, zone=self.zone, instance=str(instance_id))

    def get_instance_status(self, instance_id: VpsInstanceId) -> VpsInstanceStatus:
        try:
            instance = self._get_instance(instance_id)
        except VpsApiError as e:
            # A deleted instance returns 404 -> UNKNOWN (mirrors AWS's
            # InvalidInstanceID.NotFound handling). Other errors surface.
            if e.status_code == 404:
                return VpsInstanceStatus.UNKNOWN
            raise
        return _STATUS_MAP.get(instance.status, VpsInstanceStatus.UNKNOWN)

    def get_instance_ip(self, instance_id: VpsInstanceId) -> str:
        instance = self._get_instance(instance_id)
        for network_interface in instance.network_interfaces:
            for access_config in network_interface.access_configs:
                if access_config.nat_i_p:
                    return access_config.nat_i_p
        raise VpsProvisioningError(f"Instance {instance_id} does not have an external IP yet")

    def list_instances(self, provider_tag: str | None = None) -> list[dict[str, Any]]:
        """List instances in this zone. Optionally filtered by the ``mngr-provider`` label.

        Returns a normalized list of dicts with keys: ``id`` (the instance
        name), ``main_ip``, ``state``, and ``tags`` (a list of ``"key=value"``
        strings built from the instance's labels, to mirror Vultr's tag shape).
        """
        # ``filter`` is not a flattened kwarg on InstancesClient.list -- it lives
        # on the request object, so build a ListInstancesRequest explicitly.
        request = compute_v1.ListInstancesRequest(project=self.project_id, zone=self.zone)
        if provider_tag is not None:
            request.filter = f"labels.mngr-provider={to_gce_label_value(provider_tag)}"

        instances: list[dict[str, Any]] = []
        with self._translate_gcp_errors():
            page_result = self._instances().list(request=request)
            for instance in page_result:
                main_ip = ""
                for network_interface in instance.network_interfaces:
                    for access_config in network_interface.access_configs:
                        if access_config.nat_i_p:
                            main_ip = access_config.nat_i_p
                tag_kv = [f"{key}={value}" for key, value in instance.labels.items()]
                instances.append(
                    {
                        "id": instance.name,
                        "main_ip": main_ip,
                        "state": instance.status,
                        "tags": tag_kv,
                    }
                )
        return instances

    # =========================================================================
    # Snapshot Operations (boot persistent disk of an instance)
    # =========================================================================

    def _boot_disk_source(self, instance_id: VpsInstanceId) -> str:
        instance = self._get_instance(instance_id)
        for disk in instance.disks:
            if disk.boot and disk.source:
                return disk.source
        raise VpsApiError(500, f"Instance {instance_id} has no boot disk source")

    def create_snapshot(self, instance_id: VpsInstanceId, description: str) -> VpsSnapshotId:
        source_disk = self._boot_disk_source(instance_id)
        snapshot_name = f"mngr-snap-{uuid4().hex}"
        snapshot = compute_v1.Snapshot(name=snapshot_name, source_disk=source_disk, description=description)
        with self._translate_gcp_errors():
            operation = self._snapshots().insert(project=self.project_id, snapshot_resource=snapshot)
        self._await_operation(operation)
        logger.info("Created snapshot {} from boot disk of {}", snapshot_name, instance_id)
        return VpsSnapshotId(snapshot_name)

    def delete_snapshot(self, snapshot_id: VpsSnapshotId) -> None:
        with self._translate_gcp_errors():
            operation = self._snapshots().delete(project=self.project_id, snapshot=str(snapshot_id))
        self._await_operation(operation)
        logger.info("Deleted snapshot {}", snapshot_id)

    def list_snapshots(self) -> list[VpsSnapshotInfo]:
        snapshots: list[VpsSnapshotInfo] = []
        with self._translate_gcp_errors():
            page_result = self._snapshots().list(project=self.project_id)
            for snapshot in page_result:
                snapshots.append(
                    VpsSnapshotInfo(
                        id=VpsSnapshotId(snapshot.name),
                        description=snapshot.description or "",
                        created_at=datetime.fromisoformat(snapshot.creation_timestamp),
                    )
                )
        return snapshots

    # =========================================================================
    # SSH Key Operations (no native GCE per-key resource; in-memory map)
    # =========================================================================

    def upload_ssh_key(self, name: str, public_key: str) -> str:
        """Stash the public key in memory under ``name``; return ``name`` as the key ID.

        GCE has no per-key resource, so nothing is uploaded to the provider
        here -- ``create_instance`` writes the key into per-instance ``ssh-keys``
        metadata. The base flow uses one client instance for both calls, so the
        in-memory map bridges them.
        """
        self._ssh_public_keys_by_id[name] = public_key
        logger.debug("Stored SSH public key {} for per-instance metadata injection", name)
        return name

    def delete_ssh_key(self, key_id: str) -> None:
        """Drop the in-memory key entry. Tolerant of an absent key (fresh-process delete)."""
        self._ssh_public_keys_by_id.pop(key_id, None)
        logger.debug("Dropped in-memory SSH public key {}", key_id)

    def list_ssh_keys(self) -> list[VpsSshKeyInfo]:
        """List the in-memory keys. GCE keys live only in per-instance metadata, not as a resource."""
        return [VpsSshKeyInfo(id=key_id, name=key_id) for key_id in self._ssh_public_keys_by_id]
