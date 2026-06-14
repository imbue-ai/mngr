from collections.abc import Mapping
from collections.abc import Sequence
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.primitives import HostId
from imbue.mngr_imbue_cloud.bare_metal import slice_lima_disk_name
from imbue.mngr_imbue_cloud.bare_metal import slice_lima_instance_name
from imbue.mngr_imbue_cloud.lima_slice import build_slice_lima_yaml
from imbue.mngr_lima.lima_yaml import write_lima_yaml
from imbue.mngr_lima.limactl import limactl_delete
from imbue.mngr_lima.limactl import limactl_disk_create
from imbue.mngr_lima.limactl import limactl_disk_delete
from imbue.mngr_lima.limactl import limactl_list
from imbue.mngr_lima.limactl import limactl_start_new
from imbue.mngr_vps_docker.primitives import VpsInstanceId
from imbue.mngr_vps_docker.primitives import VpsInstanceStatus
from imbue.mngr_vps_docker.primitives import VpsSnapshotId
from imbue.mngr_vps_docker.vps_client import VpsClientInterface
from imbue.mngr_vps_docker.vps_client import VpsSnapshotInfo
from imbue.mngr_vps_docker.vps_client import VpsSshKeyInfo

# The client runs limactl on the machine it executes on (the bare-metal box,
# where mngr is installed). The slice VM's forwarded ports are reachable at the
# box's own interface; the provider connects to them via loopback while baking
# on the box, and the admin records the box's external address for outside use.
_LOOPBACK_ADDRESS: Final[str] = "127.0.0.1"

# Lima "Status" strings (from `limactl list --json`) mapped to VPS statuses.
_LIMA_STATUS_MAP: Final[dict[str, VpsInstanceStatus]] = {
    "Running": VpsInstanceStatus.ACTIVE,
    "Stopped": VpsInstanceStatus.HALTED,
}

_DISK_NAME_SUFFIX: Final[str] = "-data"


class SliceProvisionResult(FrozenModel):
    """What a slice provision produced: the lima instance/disk and the two host ports."""

    instance_name: str = Field(description="Lima instance name (also the VpsInstanceId)")
    disk_name: str = Field(description="Lima additional-disk name backing the slice's btrfs data")
    vm_ssh_host_port: int = Field(description="Box host port forwarded to the VM's root sshd")
    container_ssh_host_port: int = Field(description="Box host port forwarded to the inner container sshd")


class LimaSliceVpsClient(VpsClientInterface):
    """VpsClientInterface backed by a local lima VM -- a 'slice' on a bare-metal box.

    Drives ``limactl`` on the machine it runs on (the box, where mngr is
    installed and vendored). Provisioning is multi-step (build YAML -> create disk
    -> start VM), so ``create_instance`` raises like the OVH client and
    ``SliceVpsDockerProvider`` calls :meth:`provision_slice_vm` directly. Ordering
    / snapshot / ssh-key API operations are unavailable; the lifecycle methods the
    shared ``VpsDockerProvider`` needs (destroy / status / ip) are implemented.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

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
        """Create and start a VPS-parity lima VM for ``host_id`` and return its handle.

        Pre-creates the lima-managed btrfs data disk (Lima only auto-formats an
        ``additionalDisks`` entry whose disk record already exists), then starts
        the VM from the slice YAML.
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
        yaml_path = write_lima_yaml(config)
        cg = ConcurrencyGroup(name="slice-provision")
        with cg:
            limactl_disk_create(cg, disk_name, f"{disk_gib}GiB")
            limactl_start_new(cg, instance_name, yaml_path)
        logger.info("Provisioned slice VM {} (disk {})", instance_name, disk_name)
        return SliceProvisionResult(
            instance_name=instance_name,
            disk_name=disk_name,
            vm_ssh_host_port=vm_ssh_host_port,
            container_ssh_host_port=container_ssh_host_port,
        )

    def destroy_instance(self, instance_id: VpsInstanceId) -> None:
        """Delete the slice's lima VM and its btrfs data disk (frees the box slot)."""
        instance_name = str(instance_id)
        disk_name = f"{instance_name}{_DISK_NAME_SUFFIX}"
        cg = ConcurrencyGroup(name="slice-destroy")
        with cg:
            limactl_delete(cg, instance_name)
            limactl_disk_delete(cg, disk_name)
        logger.info("Destroyed slice VM {} (disk {})", instance_name, disk_name)

    def get_instance_status(self, instance_id: VpsInstanceId) -> VpsInstanceStatus:
        cg = ConcurrencyGroup(name="slice-status")
        with cg:
            instances = limactl_list(cg)
        for instance in instances:
            if instance.get("name") == str(instance_id):
                return _LIMA_STATUS_MAP.get(str(instance.get("status", "")), VpsInstanceStatus.UNKNOWN)
        return VpsInstanceStatus.UNKNOWN

    def get_instance_ip(self, instance_id: VpsInstanceId) -> str:
        # The provider bakes on the box itself, reaching the VM's forwarded ports
        # via loopback. External reachability uses the box's recorded public
        # address (held by the admin/connector layer), not this value.
        return _LOOPBACK_ADDRESS

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
