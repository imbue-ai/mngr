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
from typing import Self
from uuid import uuid4

from google.api_core import exceptions as google_api_exceptions
from google.auth.credentials import Credentials
from google.cloud import compute_v1
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.mngr.errors import MngrError
from imbue.mngr_gcp.errors import InvalidGceIdentifierError
from imbue.mngr_vps_docker.errors import VpsApiError
from imbue.mngr_vps_docker.errors import VpsProvisioningError
from imbue.mngr_vps_docker.primitives import VpsInstanceId
from imbue.mngr_vps_docker.primitives import VpsInstanceStatus
from imbue.mngr_vps_docker.vps_client import VpsClientInterface

# Label key stamped on every mngr-managed instance (the provider-instance name
# is the value). Discovery filters on it, and ``mngr gcp cleanup`` uses its
# presence (any value) to find mngr-managed instances project-wide before
# deleting the shared firewall rule.
MNGR_PROVIDER_LABEL_KEY: Final[str] = "mngr-provider"

# Label key/value that ``create_instance`` adds to every GCE instance launched
# while ``PYTEST_CURRENT_TEST`` is set. The conftest session-end scanner uses
# this label (not the instance name) to find leaked instances, which means
# tests do not have to constrain host naming: any agent name works.
GCP_PYTEST_LAUNCHED_LABEL: Final[str] = "mngr-pytest-launched"

# SSH metadata is injected as ``<user>:<public-key>``. The google-guest-agent
# creates whatever user is named here, so ``ubuntu`` works on any image (including
# the default Debian 12) without pre-existing. The startup-script also writes the
# key into root's authorized_keys, where mngr actually connects.
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
_MAX_LABEL_VALUE_LENGTH: Final[int] = 63
# RFC1035 instance names: lowercase letter first, then lowercase alphanumerics
# and dashes, ending alphanumeric, 1-63 chars.
_INVALID_NAME_CHARS_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9-]")
_GCE_INSTANCE_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z]([-a-z0-9]*[a-z0-9])?$")
_MAX_INSTANCE_NAME_LENGTH: Final[int] = 63
# 32-hex host-id suffix (+ a separating dash) is appended to guarantee
# uniqueness, leaving this many characters for the human-readable label stem.
_INSTANCE_NAME_STEM_LENGTH: Final[int] = _MAX_INSTANCE_NAME_LENGTH - 33


class GceLabelValue(NonEmptyStr):
    """A GCE label value: non-empty, ``[a-z0-9_-]``, at most 63 chars.

    The codebase models identifier strings as ``NonEmptyStr`` subtypes
    (``SnapshotId``, ``ProviderInstanceName``, ...); this is the GCE-label
    analog. ``to_gce_label_value`` produces it, and the constructor re-asserts
    the coercion output is valid -- so a future regression in that coercion (or
    a pathological empty input that would otherwise yield an invalid empty
    label) fails fast here rather than at the GCE API.
    """

    def __new__(cls, value: str) -> Self:
        candidate = value.strip()
        if _INVALID_LABEL_CHARS_RE.search(candidate) or not 1 <= len(candidate) <= _MAX_LABEL_VALUE_LENGTH:
            raise InvalidGceIdentifierError(
                f"{candidate!r} is not a valid GCE label value "
                f"(must be 1 to {_MAX_LABEL_VALUE_LENGTH} chars matching [a-z0-9_-])"
            )
        return super().__new__(cls, candidate)


class GceInstanceName(NonEmptyStr):
    """A GCE instance name: RFC1035 (lowercase-letter first, ``[a-z0-9-]``,
    ending alphanumeric, at most 63 chars).

    The ``NonEmptyStr``-subtype analog of ``GceLabelValue`` for instance names.
    ``_make_instance_name`` produces it; the constructor re-asserts validity for
    the same fail-fast reason.
    """

    def __new__(cls, value: str) -> Self:
        candidate = value.strip()
        if not _GCE_INSTANCE_NAME_RE.match(candidate) or len(candidate) > _MAX_INSTANCE_NAME_LENGTH:
            raise InvalidGceIdentifierError(
                f"{candidate!r} is not a valid GCE instance name "
                f"(must be RFC1035: lowercase letter first, [a-z0-9-], ending alphanumeric, "
                f"at most {_MAX_INSTANCE_NAME_LENGTH} chars)"
            )
        return super().__new__(cls, candidate)


def to_gce_label_value(value: str) -> GceLabelValue:
    """Coerce an arbitrary tag value into a valid GCE label value.

    GCE label values are restricted to ``[a-z0-9_-]`` and 63 chars. The same
    transform is applied at write time (``create_instance`` labels) and at read
    time (the ``list_instances`` label filter) so the round-trip is exact. Two
    provider instances whose names differ only by case would collide here -- an
    acceptable, documented edge (name them distinctly).
    """
    return GceLabelValue(_INVALID_LABEL_CHARS_RE.sub("-", value.lower())[:_MAX_LABEL_VALUE_LENGTH])


def _make_instance_name(label: str, tags: Mapping[str, str]) -> GceInstanceName:
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
    return GceInstanceName(f"{stem}-{suffix}"[:_MAX_INSTANCE_NAME_LENGTH].rstrip("-"))


class FirewallPrepareResult(FrozenModel):
    """Outcome of ``GcpVpsClient.ensure_firewall`` / ``mngr gcp prepare``."""

    target_tag: str = Field(description="Network tag an instance must carry to receive the rule's SSH ingress")
    was_created: bool = Field(
        description=(
            "True if a new firewall rule was created by this call; False if it already existed "
            "(idempotent re-run, or a concurrent create won the race) or if no rule was needed "
            "(empty allowed_ssh_cidrs)"
        )
    )


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
    image: str | None = Field(
        default=None,
        frozen=True,
        description=(
            "Source image for boot disks created via this client. Required for create_instance; "
            "the operator commands (mngr gcp prepare/cleanup) leave it None because they only "
            "touch firewall rules and never launch an instance."
        ),
    )
    machine_type: str = Field(default="e2-small", description="Default machine type for instances")
    boot_disk_size_gb: int = Field(default=30, description="Boot disk size in GB")
    boot_disk_type: str = Field(default="pd-balanced", description="Boot disk type")
    network: str = Field(default="default", description="VPC network name")
    subnetwork: str | None = Field(default=None, description="Subnetwork name, or None to let GCE pick")
    allowed_ssh_cidrs: tuple[str, ...] = Field(
        default=("0.0.0.0/0",),
        description=(
            "CIDR blocks allowed inbound on tcp/22 and tcp/container_ssh_port of the mngr-managed SSH "
            "firewall rule. Default ('0.0.0.0/0',) allows any IP; set to e.g. ('203.0.113.4/32',) to "
            "restrict to your own IP, or () for no ingress (no firewall rule is created and the "
            "instance is unreachable from outside its VPC). A warning is logged when the effective "
            "range is 0.0.0.0/0 or empty."
        ),
    )
    firewall_name: str = Field(default="mngr-gcp-ssh", description="Name of the mngr-managed SSH firewall rule")
    firewall_target_tag: str = Field(default="mngr-ssh", description="Network tag the firewall rule targets")
    associate_external_ip: bool = Field(default=True, description="Assign an ephemeral external IPv4 to instances")
    service_account_email: str | None = Field(default=None, description="Service account email to attach, or None")
    service_account_scopes: tuple[str, ...] = Field(
        default=("https://www.googleapis.com/auth/cloud-platform",),
        description="OAuth scopes for the attached service account",
    )
    auto_shutdown_seconds: int | None = Field(
        default=None,
        description=(
            "When set, instances are launched with scheduling.max_run_duration + "
            "instance_termination_action=DELETE so the VM self-deletes after N seconds -- the "
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

    def _warn_about_cidrs_if_needed(self) -> None:
        """Emit a one-line warning when the effective CIDR set is empty or 0.0.0.0/0.

        The two cases need different wording: empty means "no usable ingress"
        (no firewall rule is created, so the instance is unreachable from
        outside its VPC), whereas 0.0.0.0/0 means "open to the internet"
        (default but worth flagging). Anything else is silent. Mirrors
        ``AwsVpsClient._warn_about_cidrs_if_needed``.
        """
        if not self.allowed_ssh_cidrs:
            logger.warning(
                "GCP allowed_ssh_cidrs is empty; no SSH firewall rule will be created and instances "
                "tagged {!r} will be unreachable from outside the VPC unless another rule grants "
                "ingress. Set allowed_ssh_cidrs on the provider config (e.g. ('203.0.113.4/32',)) to fix.",
                self.firewall_target_tag,
            )
            return
        if "0.0.0.0/0" in self.allowed_ssh_cidrs:
            logger.warning(
                "GCP allowed_ssh_cidrs includes 0.0.0.0/0; firewall rule {!r} will "
                "permit SSH from the public internet.",
                self.firewall_name,
            )

    def ensure_firewall(self) -> FirewallPrepareResult:
        """Ensure the SSH firewall rule exists, creating it if absent.

        Returns a ``FirewallPrepareResult`` carrying the target tag and whether
        a rule was newly created (False on idempotent re-run or empty ingress).

        GCE firewalls are network-scoped and tag-targeted (not per-instance
        like an EC2 security group). One rule named ``firewall_name`` allows
        tcp/22 + tcp/``container_ssh_port`` from every CIDR in
        ``allowed_ssh_cidrs`` to instances carrying ``firewall_target_tag``.

        Fails open (mirrors ``AwsVpsClient.ensure_security_group``): the default
        ``allowed_ssh_cidrs`` of ('0.0.0.0/0',) is created and logged as a
        warning. An empty ``allowed_ssh_cidrs`` creates no rule at all -- GCE
        rejects an INGRESS rule with no ``source_ranges``, so the analog of
        AWS's zero-ingress security group is simply the absence of a rule; this
        is warned and the target tag returned, leaving instances unreachable
        until some rule grants ingress. A pre-existing rule is reused as-is
        (ingress is not re-patched); delete it manually to change the allowed
        CIDRs.

        This is the privileged write path, used by ``mngr gcp prepare`` (one-time
        admin setup). The hot path in ``create_instance`` uses
        ``resolve_firewall`` instead, which is lookup-only and needs only
        instance-create permissions (no ``compute.firewalls.create``).
        """
        self._warn_about_cidrs_if_needed()
        if not self.allowed_ssh_cidrs:
            return FirewallPrepareResult(target_tag=self.firewall_target_tag, was_created=False)
        if self._firewall_exists():
            return FirewallPrepareResult(target_tag=self.firewall_target_tag, was_created=False)

        firewall = compute_v1.Firewall(
            name=self.firewall_name,
            network=f"projects/{self.project_id}/global/networks/{self.network}",
            direction="INGRESS",
            source_ranges=list(self.allowed_ssh_cidrs),
            target_tags=[self.firewall_target_tag],
            allowed=[compute_v1.Allowed(I_p_protocol="tcp", ports=["22", str(self.container_ssh_port)])],
            description="Created by mngr gcp prepare for SSH access to mngr-managed instances",
        )
        was_created = True
        try:
            with self._translate_gcp_errors():
                operation = self._firewalls().insert(project=self.project_id, firewall_resource=firewall)
                operation.result()
        except VpsApiError as e:
            # A concurrent create (another host provisioning in parallel) wins
            # the race; the rule now exists, which is exactly what we wanted.
            if e.status_code != 409:
                raise
            was_created = False
        logger.info(
            "Ensured firewall rule {} (tag {}) in project {}",
            self.firewall_name,
            self.firewall_target_tag,
            self.project_id,
        )
        return FirewallPrepareResult(target_tag=self.firewall_target_tag, was_created=was_created)

    def resolve_firewall(self) -> str:
        """Look up the firewall rule without creating or modifying it. Returns the target tag.

        Mirrors ``ensure_firewall`` but with no write API calls -- the hot
        ``create_instance`` path needs only instance-create permissions. When
        the rule is missing, raises a ``MngrError`` pointing at
        ``mngr gcp prepare`` so a user with restricted IAM gets a clear next
        step rather than an opaque permission denial when the instance later
        proves unreachable.

        Empty ``allowed_ssh_cidrs`` short-circuits to the target tag without a
        lookup: ``ensure_firewall`` / ``mngr gcp prepare`` creates no rule in
        that case (GCE rejects an empty-source INGRESS rule), so there is
        nothing to resolve and pointing the user at ``prepare`` would be wrong.
        The instance launches intentionally unreachable, matching the
        fail-open AWS behavior.
        """
        if not self.allowed_ssh_cidrs:
            return self.firewall_target_tag
        if self._firewall_exists():
            return self.firewall_target_tag
        raise MngrError(
            f"GCP firewall rule {self.firewall_name!r} does not exist in project {self.project_id!r}. "
            f"Run `mngr gcp prepare --project {self.project_id}` once to create it "
            "(needs compute.firewalls.create), then retry the create."
        )

    def delete_firewall(self) -> str | None:
        """Delete the SSH firewall rule, undoing ``ensure_firewall`` / ``mngr gcp prepare``.

        The inverse of the privileged prepare path, used by ``mngr gcp cleanup``.
        Returns the deleted rule name, or ``None`` when no rule named
        ``firewall_name`` exists (idempotent: cleaning an already-clean project is
        a no-op). Needs ``compute.firewalls.get`` + ``compute.firewalls.delete``.

        Unlike instances, a GCE firewall rule has no resource that blocks its
        deletion -- removing it simply drops the SSH ingress for every tagged
        instance in the network. That is exactly why ``mngr gcp cleanup`` checks
        for live mngr-managed instances first (see ``list_mngr_managed_instances``),
        so it never strands a running agent's SSH access.
        """
        if not self._firewall_exists():
            return None
        try:
            with self._translate_gcp_errors():
                operation = self._firewalls().delete(project=self.project_id, firewall=self.firewall_name)
                operation.result()
        except VpsApiError as e:
            # A concurrent delete won the race; the rule is already gone, which
            # is the desired end state.
            if e.status_code != 404:
                raise
        logger.info("Deleted firewall rule {} in project {}", self.firewall_name, self.project_id)
        return self.firewall_name

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
        image: str | None = None,
    ) -> VpsInstanceId:
        """Provision a GCE instance in the client's bound zone.

        ``region`` is interpreted as the **zone** for GCP (GCE VMs are zonal);
        it must equal the zone this client is bound to.

        ``spot`` (from the per-host ``--gcp-spot`` build arg) launches the VM on
        GCE Spot capacity (``scheduling.provisioning_model=SPOT``).

        ``image`` (from the per-host ``--gcp-image`` build arg) overrides the
        client's configured source image for this VM only; when None the
        client's ``image`` (``config.default_source_image``) is used.
        """
        if region != self.zone:
            raise VpsApiError(
                400,
                f"Cross-zone create not supported: client bound to zone {self.zone!r}, "
                f"got region={region!r} (for GCP, --gcp-zone is the placement knob). Instantiate a zone-specific client.",
            )
        # The per-host --gcp-image override wins over the client's configured
        # image; capture the resolved value into a local so the type checker sees
        # it stay non-None through the later proto construction.
        source_image = image or self.image
        if source_image is None:
            raise VpsApiError(
                400,
                "create_instance requires a source image, but none was supplied: no --gcp-image override "
                "and this client was constructed without one (image=None). The backend always supplies "
                "config.default_source_image; only the operator commands (mngr gcp prepare/cleanup) build "
                "an image-less client, and those never create instances.",
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
            # GCP bootstraps via the ``startup-script`` metadata (run by the
            # google-guest-agent on every image), not cloud-init ``user-data``,
            # which stock GCE Debian images ignore. ``user_data`` is the
            # startup-script bash (see ``GcpProvider._generate_bootstrap_payload``).
            compute_v1.Items(key="startup-script", value=user_data),
            # Disable OS Login and project-wide SSH keys so only the per-instance
            # ssh-keys metadata grants access (no inherited project keys).
            compute_v1.Items(key="enable-oslogin", value="FALSE"),
            compute_v1.Items(key="block-project-ssh-keys", value="TRUE"),
        ]
        if ssh_metadata_value:
            metadata_items.append(compute_v1.Items(key="ssh-keys", value=ssh_metadata_value))

        labels: dict[str, str] = {to_gce_label_value(k): to_gce_label_value(v) for k, v in tags.items()}
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
                        source_image=source_image,
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
        # Scheduling composes two independent opt-ins onto one Scheduling object:
        #   - auto_shutdown_seconds -> max_run_duration (GCE-native auto-delete:
        #     the VM self-deletes after the deadline even if the orchestrating
        #     process is killed -- the analog of AWS
        #     InstanceInitiatedShutdownBehavior=terminate).
        #   - spot -> provisioning_model=SPOT (run on preemptible Spot capacity).
        # Both want instance_termination_action=DELETE: the run-duration deadline
        # AND a Spot preemption should delete the (ephemeral) VM, not leave a
        # stopped instance behind -- mngr has no VM-level resume yet, so a stopped
        # VM would just be cost/cruft.
        scheduling = compute_v1.Scheduling()
        has_scheduling = False
        if self.auto_shutdown_seconds is not None and self.auto_shutdown_seconds > 0:
            scheduling.max_run_duration = compute_v1.Duration(seconds=self.auto_shutdown_seconds)
            scheduling.instance_termination_action = "DELETE"
            has_scheduling = True
        if spot:
            scheduling.provisioning_model = "SPOT"
            scheduling.instance_termination_action = "DELETE"
            has_scheduling = True
        if has_scheduling:
            instance.scheduling = scheduling

        with self._translate_gcp_errors():
            operation = self._instances().insert(project=self.project_id, zone=self.zone, instance_resource=instance)
        self._await_operation(operation)
        logger.info(
            "Created GCE instance {} (label: {}, zone: {}, machine_type: {}, image: {})",
            instance_name,
            label,
            self.zone,
            plan,
            source_image,
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
            request.filter = f"labels.{MNGR_PROVIDER_LABEL_KEY}={to_gce_label_value(provider_tag)}"

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

    def list_mngr_managed_instances(self) -> list[dict[str, Any]]:
        """List mngr-managed instances across ALL zones in the project.

        Project-wide via ``aggregatedList``, not zone-bound like
        ``list_instances``, because the firewall rule is network-global:
        deleting it drops SSH ingress for every tagged instance in the project,
        regardless of zone. ``mngr gcp cleanup`` uses this to refuse deleting the
        rule while any mngr-managed agent still exists, so cleanup never strands
        one. Matches by the ``mngr-provider`` label key (any value), spanning
        every mngr provider config bound to the project. Instances that no longer
        exist do not appear, so anything returned is live (a stopped/``TERMINATED``
        VM still exists and still counts). Returns dicts with ``id``, ``state``,
        and ``zone``.
        """
        request = compute_v1.AggregatedListInstancesRequest(project=self.project_id)
        managed: list[dict[str, Any]] = []
        with self._translate_gcp_errors():
            for zone_scope, scoped_list in self._instances().aggregated_list(request=request):
                for instance in scoped_list.instances:
                    if MNGR_PROVIDER_LABEL_KEY in instance.labels:
                        managed.append(
                            {
                                "id": instance.name,
                                "state": instance.status,
                                "zone": zone_scope.removeprefix("zones/"),
                            }
                        )
        return managed

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
