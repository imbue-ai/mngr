from abc import ABC
from abc import abstractmethod
from pathlib import Path

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.primitives import HostId
from imbue.mngr_imbue_cloud.data_types import BoxManagementTrust
from imbue.mngr_imbue_cloud.data_types import SliceProvisionResult
from imbue.mngr_imbue_cloud.data_types import StorageVolumeState
from imbue.mngr_imbue_cloud.slices.gen2_scripts.box_commands import SliceInstanceObservation
from imbue.mngr_vps.vps_client import VpsClientInterface


class SliceReconcilerState(FrozenModel):
    """What the in-VM key reconciler currently looks like, as read from the slice VM."""

    is_unit_enabled: bool = Field(description="Whether the reconciler systemd unit is installed and enabled")
    desired_authorized_keys: str | None = Field(
        description="Content of the root-owned desired-state authorized_keys file, or None when absent"
    )
    is_live_matching_desired: bool = Field(
        description="Whether /root/.ssh/authorized_keys currently equals the desired-state file"
    )
    installed_content_hash: str | None = Field(
        description=(
            "sha256 of the installed reconciler unit + script contents, or None when either file is absent. "
            "Compared against what the current client version would install, so the heal pass replaces stale "
            "reconciler content, not just a missing/disabled unit."
        )
    )


class SliceVmAccessInterface(MutableModel, ABC):
    """Operations adoption performs on a leased slice, over its VM-root SSH endpoint.

    Every mutation runs as root inside the slice VM (container-side changes go
    through ``docker exec`` from the VM), so the whole surface needs exactly one
    working credential: the per-host client key on the VM-root sshd. Host-key
    probes are unauthenticated TCP handshakes against the slice's box-forwarded
    ports.
    """

    @abstractmethod
    def read_vm_root_authorized_keys(self) -> str | None:
        """Read the VM root's authorized_keys content, or None when the file is absent."""

    @abstractmethod
    def install_reconciler(self, desired_authorized_keys: str) -> None:
        """Install (or refresh) the in-VM key reconciler and run it once, asserting the desired state now."""

    @abstractmethod
    def read_reconciler_state(self) -> SliceReconcilerState:
        """Read the reconciler unit's enablement and desired-vs-live authorized_keys state."""

    @abstractmethod
    def install_vm_host_key(self, private_key_pem: str, public_key: str) -> None:
        """Install a new sshd host key on the VM (desired copy + live /etc/ssh) and reload its sshd."""

    @abstractmethod
    def install_container_host_key(self, private_key_pem: str, public_key: str) -> None:
        """Install a new sshd host key inside the workspace container and reload its sshd."""

    @abstractmethod
    def append_container_authorized_key(self, public_key: str) -> None:
        """Idempotently append a client public key to the container root's authorized_keys."""

    @abstractmethod
    def remove_container_authorized_key(self, public_key: str) -> None:
        """Remove a client public key line from the container root's authorized_keys."""

    @abstractmethod
    def is_endpoint_serving_host_key(self, port: int, public_key: str) -> bool:
        """Whether the sshd at the slice's address on ``port`` currently presents ``public_key``."""

    @abstractmethod
    def can_authenticate(self, port: int, private_key_path: Path) -> bool:
        """Whether an SSH connection to the slice's address on ``port`` authenticates with this key."""


class SliceVmClientInterface(VpsClientInterface, ABC):
    """VPS-client contract for slice VMs carved on a bare-metal box, driven over SSH.

    Implemented per box generation: :class:`~imbue.mngr_imbue_cloud.slices.lima_slice_client.LimaSliceVpsClient`
    (gen 1, lima/slirp) and :class:`~imbue.mngr_imbue_cloud.slices.qemu_slice_client.QemuSliceVpsClient`
    (gen 2, raw qemu with routed-tap networking). Both reach the box as its dedicated
    non-root slice user with the pool management key under strict host-key pinning.
    """

    box_address: str = Field(description="SSH-reachable address of the bare-metal box that hosts the slices")
    box_ssh_port: int = Field(
        default=22,
        description=(
            "Port the box's management sshd is dialed on. 22 except when the caller tunnels a "
            "locked-down box's :22 through a local forward (the dial is then 127.0.0.1:<port>)."
        ),
    )
    box_ssh_user: str = Field(description="Dedicated non-root user on the box that owns the slice VMs")
    private_key_path: str | None = Field(
        default=None,
        description="Path to the pool management private key used to SSH the box (None only in unit tests).",
    )
    box_host_public_key: str | None = Field(
        default=None,
        description=(
            "The box's sshd host public key, pinned for strict host-key checking (no trust-on-first-use). "
            "None only in unit tests that never SSH the box; an actual box SSH with no key fails closed."
        ),
    )

    @abstractmethod
    def provision_slice_vm(
        self,
        *,
        host_id: HostId,
        env_name: str | None,
        vcpus: int,
        memory_mib: int,
        disk_gib: int,
        host_dir: str,
        # A static key to authorize for VM root at carve (the gen-1 bake key);
        # None authorizes no static key -- the gen-2 norm, where the VM trusts
        # the tier's SSH CA instead.
        root_authorized_public_key: str | None,
        host_private_key_pem: str,
        host_public_key_openssh: str,
        boot_disk_gib: int,
        slot_count: int,
        port_range_start: int,
        port_range_end: int,
        extra_root_authorized_keys: tuple[str, ...] = (),
        # The tier's SSH CA public key VM root trusts for certificate logins
        # (gen-2 only; gen-1 ignores it).
        trusted_user_ca_public_key: str | None = None,
        # The box's declared uplink rate for gen-2 fair-share bandwidth shaping;
        # None disables shaping (and gen-1 ignores it entirely -- lima has no
        # per-VM traffic classes).
        uplink_mbps: int | None = None,
        # Gen-2 machine sizing (specs/slice-fleet): the machine's size in units
        # (1 unit = 1GiB guest RAM) and the box's two budgets. Required by the
        # gen-2 client (its reserve enforces the two-budget accounting from
        # them); ignored by gen-1, whose sizing rides memory_mib / disk_gib /
        # slot_count.
        units: int | None = None,
        box_total_units: int | None = None,
        box_disk_budget_gib: int | None = None,
    ) -> SliceProvisionResult:
        """Reserve a box slot + host ports, create the env-stamped slice VM, and boot it."""

    @abstractmethod
    def run_on_box(
        self, remote_command: str, *, timeout: float, label: str, is_streaming: bool = False
    ) -> tuple[int | None, str, str]:
        """Run a command on the box over SSH as the slice user; return (returncode, stdout, stderr)."""

    @abstractmethod
    def list_instance_names(self) -> set[str]:
        """Return the names of all slice VM instances currently on the box."""

    @abstractmethod
    def list_instance_observations(self) -> tuple[SliceInstanceObservation, ...]:
        """Return every slice VM instance on the box with its running state and age (one round-trip)."""

    @abstractmethod
    def list_disk_names(self) -> set[str]:
        """Return the identifiers of all slice data disks currently on the box."""

    @abstractmethod
    def destroy_disk(self, disk_name: str) -> None:
        """Delete a slice data disk on the box, tolerating it already being absent."""

    @abstractmethod
    def read_management_trust(self) -> BoxManagementTrust:
        """Read the service user's static authorized keys and the box's trusted SSH CA in one round trip."""

    @abstractmethod
    def read_box_health_texts(self) -> tuple[str, str]:
        """Return the box's ``/proc/mdstat`` and ``/proc/swaps`` contents in one round-trip."""

    @abstractmethod
    def read_storage_volume_state(self) -> StorageVolumeState:
        """Read what backs the gen-2 storage root (the opened LUKS mapper, a plain device, or nothing) in one round-trip."""
