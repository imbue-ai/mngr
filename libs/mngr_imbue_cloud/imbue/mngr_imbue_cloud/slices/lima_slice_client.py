import base64
import hashlib
import json
import shlex
import tempfile
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.primitives import HostId
from imbue.mngr.providers.ssh_utils import add_host_to_known_hosts
from imbue.mngr_imbue_cloud.errors import SliceCapacityError
from imbue.mngr_imbue_cloud.slices.bare_metal import slice_lima_disk_name
from imbue.mngr_imbue_cloud.slices.bare_metal import slice_lima_instance_name
from imbue.mngr_imbue_cloud.slices.lima_slice import CONTAINER_SSH_PORT_PLACEHOLDER
from imbue.mngr_imbue_cloud.slices.lima_slice import SLICE_BOX_FULL_MARKER
from imbue.mngr_imbue_cloud.slices.lima_slice import SLICE_NO_PORTS_MARKER
from imbue.mngr_imbue_cloud.slices.lima_slice import VM_SSH_PORT_PLACEHOLDER
from imbue.mngr_imbue_cloud.slices.lima_slice import build_slice_lima_yaml
from imbue.mngr_imbue_cloud.slices.lima_slice import build_slice_reserve_script
from imbue.mngr_imbue_cloud.slices.lima_slice import parse_reserved_ports
from imbue.mngr_lima.errors import LimaCommandError
from imbue.mngr_lima.lima_yaml import write_lima_yaml
from imbue.mngr_vps.primitives import VpsInstanceId
from imbue.mngr_vps.primitives import VpsInstanceStatus
from imbue.mngr_vps.vps_client import VpsClientInterface

# Lima "Status" strings (from `limactl list --json`) mapped to VPS statuses.
_LIMA_STATUS_MAP: Final[dict[str, VpsInstanceStatus]] = {
    "Running": VpsInstanceStatus.ACTIVE,
    "Stopped": VpsInstanceStatus.HALTED,
}

_DISK_NAME_SUFFIX: Final[str] = "-data"

# limactl is extracted to /usr/local/bin and uv to ~/.local/bin by the box prep
# (``bare_metal_prep.build_box_prep_script``). A non-interactive SSH shell may
# not source the lima user's profile, so we set PATH explicitly. limactl refuses
# to run as root, so the box is always reached as the dedicated lima user.
_BOX_PATH_PREFIX: Final[str] = "PATH=/usr/local/bin:$HOME/.local/bin:$PATH"
# A slice VM boots in a few minutes; give limactl start a generous cap.
_LIMA_START_TIMEOUT_SECONDS: Final[float] = 1800.0
_LIMA_SHORT_TIMEOUT_SECONDS: Final[float] = 120.0
# The reservation (under the box lock) is short but includes ``limactl create``,
# which materializes the instance's boot disk from the staged base image -- give it
# more room than a plain limactl call while keeping the lock hold bounded.
_LIMA_RESERVE_TIMEOUT_SECONDS: Final[float] = 600.0
_BOX_CONNECT_TIMEOUT_SECONDS: Final[int] = 30


def _is_already_absent_error(stderr: str) -> bool:
    """True if a limactl delete failure stderr indicates the target was already gone.

    Used to make teardown idempotent: a slice carve can fail after the data disk
    was created but before the VM was registered, so both the instance and the
    disk deletes must tolerate the target already being absent.
    """
    stderr_lower = stderr.lower()
    return "not found" in stderr_lower or "does not exist" in stderr_lower


class SliceProvisionResult(FrozenModel):
    """What a slice provision produced: the lima instance/disk and the two host ports."""

    instance_name: str = Field(description="Lima instance name (also the VpsInstanceId)")
    disk_name: str = Field(description="Lima additional-disk name backing the slice's btrfs data")
    vm_ssh_host_port: int = Field(description="Box host port forwarded to the VM's root sshd")
    container_ssh_host_port: int = Field(description="Box host port forwarded to the inner container sshd")


class LimaSliceVpsClient(VpsClientInterface):
    """VpsClientInterface backed by a lima VM -- a 'slice' -- carved on a bare-metal box.

    Drives ``limactl`` **over SSH on the box** (as the dedicated lima user, using
    the pool management key), so the whole bake runs from the operator's laptop
    exactly like an OVH VPS bake: this carves the bare VM (the "OS reinstall"
    equivalent), then the shared ``VpsProvider`` reaches the VM's
    box-forwarded ports to build the container. Carving is multi-step (ship YAML
    -> create disk -> start VM), so ``create_instance`` raises like the OVH client
    and ``SliceVpsDockerProvider`` calls :meth:`provision_slice_vm` directly.
    Ordering / snapshot / ssh-key API operations are unavailable.

    Does NOT keep ``mngr_lima`` in the loop for the remote calls: it renders the
    handful of ``limactl`` invocations itself and runs them over SSH (``mngr_lima``
    only knows how to run limactl locally), so the box needs nothing but limactl.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    box_address: str = Field(description="SSH-reachable address of the bare-metal box that hosts the slices")
    box_ssh_user: str = Field(description="Dedicated non-root lima user on the box (owns the VMs)")
    private_key_path: str | None = Field(
        default=None,
        description="Path to the pool management private key used to SSH the box (None only in unit tests).",
    )
    vm_image_url: str | None = Field(
        default=None,
        description="Optional override of the lima guest image URL (defaults to mngr_lima's Debian image).",
    )
    box_host_public_key: str | None = Field(
        default=None,
        description=(
            "The box's sshd host public key, pinned for strict host-key checking (no trust-on-first-use). "
            "None only in unit tests that never SSH the box; an actual box SSH with no key fails closed."
        ),
    )

    def _box_known_hosts_file(self) -> str:
        """Write the box's pinned host key to a known_hosts file and return its path.

        Fails closed when no pinned key is configured -- we never fall back to
        trust-on-first-use. The file is idempotently (re)written next to the pool
        key (or a temp dir) keyed by box address.
        """
        if not self.box_host_public_key:
            raise LimaCommandError(
                "ssh", 1, f"no pinned host key configured for box {self.box_address}; run the host-key backfill"
            )
        base_dir = Path(self.private_key_path).parent if self.private_key_path else Path(tempfile.gettempdir())
        box_digest = hashlib.sha256(self.box_address.encode()).hexdigest()[:16]
        known_hosts_path = base_dir / f".box_known_hosts_{box_digest}"
        add_host_to_known_hosts(known_hosts_path, self.box_address, 22, self.box_host_public_key)
        return str(known_hosts_path)

    def _unavailable(self, operation: str) -> NotImplementedError:
        return NotImplementedError(
            f"LimaSliceVpsClient does not support '{operation}': slice VMs are provisioned via "
            "provision_slice_vm() and torn down via destroy_instance(); they have no cloud ordering, "
            "snapshot, or ssh-key API."
        )

    def _box_ssh_command(self, remote_command: str) -> list[str]:
        """Build the argv that runs ``remote_command`` on the box as the lima user."""
        if not self.private_key_path:
            raise LimaCommandError("ssh", 1, "no pool private key configured for the slice box")
        return [
            "ssh",
            "-i",
            self.private_key_path,
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={self._box_known_hosts_file()}",
            "-o",
            f"ConnectTimeout={_BOX_CONNECT_TIMEOUT_SECONDS}",
            "-o",
            "ServerAliveInterval=30",
            f"{self.box_ssh_user}@{self.box_address}",
            f"{_BOX_PATH_PREFIX} {remote_command}",
        ]

    def _run_on_box(
        self, remote_command: str, *, timeout: float, label: str, is_streaming: bool = False
    ) -> tuple[int | None, str, str]:
        """Run a command on the box over SSH; return (returncode, stdout, stderr)."""
        on_output = (lambda line, _is_stdout: logger.info("  [{}] {}", label, line.rstrip())) if is_streaming else None
        cg = ConcurrencyGroup(name=f"slice-box-{label}")
        with cg:
            result = cg.run_process_to_completion(
                command=self._box_ssh_command(remote_command),
                timeout=timeout,
                is_checked_after=False,
                on_output=on_output,
            )
        return result.returncode, result.stdout, result.stderr

    def _best_effort_destroy(self, instance_name: str) -> None:
        """Tear down a half-reserved instance + its disk after a failed reserve/start.

        Best-effort: a reserve that fails partway (e.g. after the data disk or the
        instance exists) must not leak the box slot, but cleanup must not mask the
        original error -- so this logs and never raises.
        """
        try:
            self.destroy_instance(VpsInstanceId(instance_name))
        except (LimaCommandError, OSError) as exc:
            logger.warning("Could not clean up half-reserved slice {} on {}: {}", instance_name, self.box_address, exc)

    def provision_slice_vm(
        self,
        *,
        host_id: HostId,
        env_name: str | None,
        vcpus: int,
        memory_mib: int,
        disk_gib: int,
        host_dir: str,
        root_authorized_public_key: str,
        host_private_key_pem: str,
        host_public_key_openssh: str,
        boot_disk_gib: int,
        slot_count: int,
        port_range_start: int,
        port_range_end: int,
        extra_root_authorized_keys: tuple[str, ...] = (),
    ) -> SliceProvisionResult:
        """Reserve a box slot + ports, create the (env-stamped) VM, and boot it; return its handle.

        Two phases. First, under a box-wide ``flock`` (one SSH command, released the
        instant it returns), the reserve script enforces capacity against the box's
        real occupancy, picks two free host ports against every existing instance's
        forwards, and creates the data disk + the instance WITHOUT booting -- so the
        slot + ports are durably claimed and visible to a sibling bake. Second, with
        the lock released, the long ``limactl start`` boots the reserved VM.

        Raises ``SliceCapacityError`` if the box is already full or has no free port
        pair; cleans up a half-reserved instance on any other failure.
        """
        instance_name = slice_lima_instance_name(host_id, env_name)
        disk_name = slice_lima_disk_name(host_id, env_name)
        config = build_slice_lima_yaml(
            host_dir=host_dir,
            vcpus=vcpus,
            memory_mib=memory_mib,
            disk_gib=disk_gib,
            boot_disk_gib=boot_disk_gib,
            disk_name=disk_name,
            root_authorized_public_key=root_authorized_public_key,
            host_private_key_pem=host_private_key_pem,
            host_public_key_openssh=host_public_key_openssh,
            vm_ssh_host_port=VM_SSH_PORT_PLACEHOLDER,
            container_ssh_host_port=CONTAINER_SSH_PORT_PLACEHOLDER,
            extra_root_authorized_keys=extra_root_authorized_keys,
        )
        if self.vm_image_url is not None:
            config["images"] = [{"location": self.vm_image_url}]
        yaml_template_text = write_lima_yaml(config).read_text()

        # Phase 1: reserve the slot + ports and create the instance (no boot) under
        # the box lock, all in one SSH command so the lock is held only this long.
        reserve_script = build_slice_reserve_script(
            instance_name=instance_name,
            disk_name=disk_name,
            disk_gib=disk_gib,
            slot_count=slot_count,
            port_range_start=port_range_start,
            port_range_end=port_range_end,
            yaml_template_text=yaml_template_text,
            lima_service_user=self.box_ssh_user,
        )
        encoded_script = base64.b64encode(reserve_script.encode()).decode()
        reserve_command = f"echo {shlex.quote(encoded_script)} | base64 -d | bash"
        reserve_rc, reserve_out, reserve_err = self._run_on_box(
            reserve_command, timeout=_LIMA_RESERVE_TIMEOUT_SECONDS, label=f"reserve:{instance_name}", is_streaming=True
        )
        if reserve_rc != 0:
            if SLICE_BOX_FULL_MARKER in reserve_err:
                raise SliceCapacityError(
                    f"bare-metal box {self.box_address} is at capacity; cannot reserve a slice: {reserve_err.strip()}"
                )
            if SLICE_NO_PORTS_MARKER in reserve_err:
                raise SliceCapacityError(
                    f"no free host ports on box {self.box_address} to reserve a slice: {reserve_err.strip()}"
                )
            # A reserve can fail after the disk or instance was created; scrub it so
            # the box slot is not leaked.
            self._best_effort_destroy(instance_name)
            raise LimaCommandError("reserve", reserve_rc or 1, reserve_err)
        vm_ssh_host_port, container_ssh_host_port = parse_reserved_ports(reserve_out)

        # Phase 2: boot the reserved VM (the long step), with the lock already released.
        try:
            start_rc, _start_out, start_err = self._run_on_box(
                f"limactl --log-level=info start {shlex.quote(instance_name)}",
                timeout=_LIMA_START_TIMEOUT_SECONDS,
                label=f"start:{instance_name}",
                is_streaming=True,
            )
            if start_rc != 0:
                raise LimaCommandError("start", start_rc or 1, start_err)
        except (LimaCommandError, OSError):
            self._best_effort_destroy(instance_name)
            raise

        logger.info(
            "Provisioned slice VM {} (disk {}, ports vm={}/container={}) on {}",
            instance_name,
            disk_name,
            vm_ssh_host_port,
            container_ssh_host_port,
            self.box_address,
        )
        return SliceProvisionResult(
            instance_name=instance_name,
            disk_name=disk_name,
            vm_ssh_host_port=vm_ssh_host_port,
            container_ssh_host_port=container_ssh_host_port,
        )

    def destroy_instance(self, instance_id: VpsInstanceId) -> None:
        """Delete the slice's lima VM and its btrfs data disk on the box (frees the box slot)."""
        instance_name = str(instance_id)
        disk_name = f"{instance_name}{_DISK_NAME_SUFFIX}"
        delete_command = f"limactl delete --force {shlex.quote(instance_name)}"
        delete_rc, _out, delete_err = self._run_on_box(
            delete_command, timeout=_LIMA_SHORT_TIMEOUT_SECONDS, label="delete"
        )
        if delete_rc != 0:
            # Tolerate the instance already being absent: a carve can fail after the
            # data disk was created but before `limactl start` registered the
            # instance, so we must still fall through to the disk delete below --
            # otherwise the orphaned disk leaks and permanently holds the box slot.
            if not _is_already_absent_error(delete_err):
                raise LimaCommandError("delete", delete_rc or 1, delete_err)
            logger.debug("Lima instance {} already absent, skipping", instance_name)
        disk_delete_command = f"limactl disk delete --force {shlex.quote(disk_name)}"
        disk_rc, _disk_out, disk_err = self._run_on_box(
            disk_delete_command, timeout=_LIMA_SHORT_TIMEOUT_SECONDS, label="disk-delete"
        )
        if disk_rc != 0:
            # Tolerate the disk already being absent (e.g. a partial prior teardown).
            if not _is_already_absent_error(disk_err):
                raise LimaCommandError("disk delete", disk_rc or 1, disk_err)
            logger.debug("Lima disk {} already absent, skipping", disk_name)
        logger.info("Destroyed slice VM {} (disk {}) on {}", instance_name, disk_name, self.box_address)

    def list_instance_names(self) -> set[str]:
        """Return the names of all lima instances currently on the box."""
        list_rc, list_out, list_err = self._run_on_box(
            "limactl list --json", timeout=_LIMA_SHORT_TIMEOUT_SECONDS, label="list"
        )
        if list_rc != 0:
            raise LimaCommandError("list", list_rc or 1, list_err)
        names: set[str] = set()
        for line in list_out.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                instance = json.loads(stripped)
            except json.JSONDecodeError as exc:
                logger.warning("Failed to parse Lima instance JSON: {}", exc)
                continue
            name = instance.get("name")
            if name:
                names.add(name)
        return names

    def list_disk_names(self) -> set[str]:
        """Return the names of all lima disks currently on the box."""
        list_rc, list_out, list_err = self._run_on_box(
            "limactl disk list --json", timeout=_LIMA_SHORT_TIMEOUT_SECONDS, label="disk-list"
        )
        if list_rc != 0:
            raise LimaCommandError("disk list", list_rc or 1, list_err)
        names: set[str] = set()
        for line in list_out.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                disk = json.loads(stripped)
            except json.JSONDecodeError as exc:
                logger.warning("Failed to parse Lima disk JSON: {}", exc)
                continue
            name = disk.get("name")
            if name:
                names.add(name)
        return names

    def destroy_disk(self, disk_name: str) -> None:
        """Delete a lima data disk on the box, unlocking it first so a leaked locked disk still goes.

        Used to reap an orphan disk (one with no pool DB row) left behind when a failed
        carve's ``limactl delete`` could not unlock the disk. ``limactl disk delete``
        refuses a locked disk, so we ``disk unlock`` first (best-effort -- it errors when
        the disk is not actually locked, which is fine), then force-delete, tolerating the
        disk already being absent.
        """
        self._run_on_box(
            f"limactl disk unlock {shlex.quote(disk_name)}", timeout=_LIMA_SHORT_TIMEOUT_SECONDS, label="disk-unlock"
        )
        delete_rc, _out, delete_err = self._run_on_box(
            f"limactl disk delete --force {shlex.quote(disk_name)}",
            timeout=_LIMA_SHORT_TIMEOUT_SECONDS,
            label="disk-delete",
        )
        if delete_rc != 0 and not _is_already_absent_error(delete_err):
            raise LimaCommandError("disk delete", delete_rc or 1, delete_err)
        logger.info("Destroyed orphan slice disk {} on {}", disk_name, self.box_address)

    def get_instance_status(self, instance_id: VpsInstanceId) -> VpsInstanceStatus:
        list_rc, list_out, list_err = self._run_on_box(
            "limactl list --json", timeout=_LIMA_SHORT_TIMEOUT_SECONDS, label="list"
        )
        if list_rc != 0:
            raise LimaCommandError("list", list_rc or 1, list_err)
        for line in list_out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                instance = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning("Failed to parse Lima instance JSON: {}", exc)
                continue
            if instance.get("name") == str(instance_id):
                return _LIMA_STATUS_MAP.get(str(instance.get("status", "")), VpsInstanceStatus.UNKNOWN)
        return VpsInstanceStatus.UNKNOWN

    def get_instance_ip(self, instance_id: VpsInstanceId) -> str:
        # The slice's sshd is forwarded on the box's own interface; external
        # consumers (and the laptop-side bake) reach it at the box's address.
        return self.box_address

    def create_instance(
        self,
        label: str,
        region: str,
        plan: str,
        user_data: str,
        ssh_key_ids: Sequence[str],
        tags: Mapping[str, str],
    ) -> VpsInstanceId:
        raise self._unavailable("create_instance")

    def upload_ssh_key(self, name: str, public_key: str) -> str:
        raise self._unavailable("upload_ssh_key")

    def delete_ssh_key(self, key_id: str) -> None:
        raise self._unavailable("delete_ssh_key")
