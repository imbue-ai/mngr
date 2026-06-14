import base64
import json
import shlex
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.primitives import HostId
from imbue.mngr_imbue_cloud.slices.bare_metal import slice_lima_disk_name
from imbue.mngr_imbue_cloud.slices.bare_metal import slice_lima_instance_name
from imbue.mngr_imbue_cloud.slices.lima_slice import build_slice_lima_yaml
from imbue.mngr_lima.errors import LimaCommandError
from imbue.mngr_lima.lima_yaml import write_lima_yaml
from imbue.mngr_vps_docker.primitives import VpsInstanceId
from imbue.mngr_vps_docker.primitives import VpsInstanceStatus
from imbue.mngr_vps_docker.primitives import VpsSnapshotId
from imbue.mngr_vps_docker.vps_client import VpsClientInterface
from imbue.mngr_vps_docker.vps_client import VpsSnapshotInfo
from imbue.mngr_vps_docker.vps_client import VpsSshKeyInfo

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
_BOX_CONNECT_TIMEOUT_SECONDS: Final[int] = 30


def parse_listening_ports(ss_output: str) -> set[int]:
    """Parse TCP listening ports from ``ss -Htln`` output (numeric, no header).

    The local-address field is the 4th whitespace-separated column; the port is
    whatever follows its last ``:`` (handles IPv6 ``[::]:<port>`` and ``*:<port>``).
    Lines that don't parse are skipped. Pure so it can be unit-tested without a box.
    """
    ports: set[int] = set()
    for line in ss_output.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        port_text = fields[3].rsplit(":", 1)[-1]
        if port_text.isdigit():
            ports.add(int(port_text))
    return ports


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
    equivalent), then the shared ``VpsDockerProvider`` reaches the VM's
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
            "StrictHostKeyChecking=accept-new",
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

    def _remove_remote_file(self, remote_path: str) -> None:
        """Best-effort ``rm -f`` of a file on the box (used to scrub the shipped YAML).

        Logged at debug and never raised: cleanup runs on both the success and the
        failure path, so it must not mask the carve's own error.
        """
        rc, _out, err = self._run_on_box(
            f"rm -f {shlex.quote(remote_path)}", timeout=_LIMA_SHORT_TIMEOUT_SECONDS, label="rm-yaml"
        )
        if rc != 0:
            logger.debug("Could not remove remote file {} on {}: {}", remote_path, self.box_address, err.strip())

    def provision_slice_vm(
        self,
        *,
        host_id: HostId,
        vcpus: int,
        memory_mib: int,
        disk_gib: int,
        host_dir: str,
        root_authorized_public_key: str,
        host_private_key_pem: str,
        host_public_key_openssh: str,
        vm_ssh_host_port: int,
        container_ssh_host_port: int,
        boot_disk_gib: int,
        extra_root_authorized_keys: tuple[str, ...] = (),
    ) -> SliceProvisionResult:
        """Create and start a VPS-parity lima VM for ``host_id`` on the box and return its handle.

        Renders the slice YAML locally, ships it to the box, pre-creates the
        lima-managed btrfs data disk (Lima only auto-formats an
        ``additionalDisks`` entry whose disk record already exists), then starts
        the VM -- all via ``limactl`` over SSH on the box.
        """
        instance_name = slice_lima_instance_name(host_id)
        disk_name = slice_lima_disk_name(host_id)
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
            vm_ssh_host_port=vm_ssh_host_port,
            container_ssh_host_port=container_ssh_host_port,
            extra_root_authorized_keys=extra_root_authorized_keys,
        )
        if self.vm_image_url is not None:
            config["images"] = [{"location": self.vm_image_url}]
        yaml_text = write_lima_yaml(config).read_text()

        # Ship the YAML to the box (base64 to dodge quoting), pre-create the disk,
        # then start the VM. The YAML embeds the VM's SSH host private key, so write
        # it 0600 (``umask 077`` before the redirect) and always remove it once the
        # VM has started -- it must not linger world-readable in /tmp on the box.
        remote_yaml_path = f"/tmp/{instance_name}.yaml"
        encoded = base64.b64encode(yaml_text.encode()).decode()
        ship_command = f"umask 077 && echo {shlex.quote(encoded)} | base64 -d > {shlex.quote(remote_yaml_path)}"
        ship_rc, _ship_out, ship_err = self._run_on_box(
            ship_command, timeout=_LIMA_SHORT_TIMEOUT_SECONDS, label="ship-yaml"
        )
        if ship_rc != 0:
            self._remove_remote_file(remote_yaml_path)
            raise LimaCommandError("ship yaml", ship_rc or 1, ship_err)

        try:
            disk_command = f"limactl disk create {shlex.quote(disk_name)} --size {shlex.quote(f'{disk_gib}GiB')}"
            disk_rc, _disk_out, disk_err = self._run_on_box(
                disk_command, timeout=_LIMA_SHORT_TIMEOUT_SECONDS, label="disk-create"
            )
            if disk_rc != 0:
                raise LimaCommandError("disk create", disk_rc or 1, disk_err)

            start_command = (
                f"limactl --log-level=info start --name={shlex.quote(instance_name)} {shlex.quote(remote_yaml_path)}"
            )
            start_rc, _start_out, start_err = self._run_on_box(
                start_command, timeout=_LIMA_START_TIMEOUT_SECONDS, label=f"start:{instance_name}", is_streaming=True
            )
            if start_rc != 0:
                raise LimaCommandError("start", start_rc or 1, start_err)
        finally:
            # The host-key-bearing YAML is no longer needed once the VM is started
            # (or the carve has failed): never leave it on the box.
            self._remove_remote_file(remote_yaml_path)

        logger.info("Provisioned slice VM {} (disk {}) on {}", instance_name, disk_name, self.box_address)
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

    def get_listening_ports(self) -> set[int]:
        """Return the set of TCP ports currently in LISTEN state on the box.

        Used by the slice provider to pick free box-forwarded ports for a new VM.
        Parses ``ss -Htln`` (numeric, no header): the local-address field is the
        4th column; the port is whatever follows its last ``:`` (handles IPv6
        ``[::]:<port>`` too).
        """
        rc, out, err = self._run_on_box("ss -Htln", timeout=_LIMA_SHORT_TIMEOUT_SECONDS, label="ss")
        if rc != 0:
            raise LimaCommandError("ss -Htln", rc or 1, err)
        return parse_listening_ports(out)

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

    def create_snapshot(self, instance_id: VpsInstanceId, description: str) -> VpsSnapshotId:
        raise self._unavailable("create_snapshot")

    def delete_snapshot(self, snapshot_id: VpsSnapshotId) -> None:
        raise self._unavailable("delete_snapshot")

    def list_snapshots(self) -> list[VpsSnapshotInfo]:
        raise self._unavailable("list_snapshots")

    def upload_ssh_key(self, name: str, public_key: str) -> str:
        raise self._unavailable("upload_ssh_key")

    def delete_ssh_key(self, key_id: str) -> None:
        raise self._unavailable("delete_ssh_key")

    def list_ssh_keys(self) -> list[VpsSshKeyInfo]:
        raise self._unavailable("list_ssh_keys")
