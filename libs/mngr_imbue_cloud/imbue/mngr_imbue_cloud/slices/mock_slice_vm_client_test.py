from collections.abc import Mapping
from collections.abc import Sequence

from pydantic import Field

from imbue.mngr.primitives import HostId
from imbue.mngr_imbue_cloud.data_types import BoxManagementTrust
from imbue.mngr_imbue_cloud.data_types import SliceProvisionResult
from imbue.mngr_imbue_cloud.data_types import StorageVolumeState
from imbue.mngr_imbue_cloud.errors import SliceCommandError
from imbue.mngr_imbue_cloud.interfaces import SliceVmClientInterface
from imbue.mngr_imbue_cloud.slices.bare_metal import SLICE_DISK_SUFFIX
from imbue.mngr_imbue_cloud.slices.gen2_scripts.box_commands import SliceInstanceObservation
from imbue.mngr_vps.primitives import VpsInstanceId
from imbue.mngr_vps.primitives import VpsInstanceStatus


class MockSliceVmClient(SliceVmClientInterface):
    """An in-memory box: slice instances with their observations and data disks, no SSH.

    ``destroy_instance`` removes the instance and its disk the way both real
    clients do (a gen-1 ``limactl delete`` drops the lima disk with it, a gen-2
    destroy removes the whole instance dir); ``destroy_disk`` removes a disk on
    its own. Names listed in ``failing_resource_names`` raise on destroy so
    tests can exercise partial-failure handling.
    """

    observations: list[SliceInstanceObservation] = Field(
        default_factory=list, description="The slice VM instances currently on the box, with their state and age"
    )
    disk_names: set[str] = Field(default_factory=set, description="The slice data disks currently on the box")
    failing_resource_names: set[str] = Field(
        default_factory=set, description="Instance and disk names whose destroy raises SliceCommandError"
    )
    destroyed_instance_names: list[str] = Field(
        default_factory=list, description="Instances destroyed successfully, in order"
    )
    destroyed_disk_names: list[str] = Field(
        default_factory=list, description="Disks destroyed successfully on their own, in order"
    )
    management_trust: BoxManagementTrust | None = Field(
        default=None, description="What read_management_trust returns; None makes the read raise"
    )
    storage_volume_state: StorageVolumeState | None = Field(
        default=None, description="What read_storage_volume_state returns; None makes the read raise"
    )

    def provision_slice_vm(
        self,
        *,
        host_id: HostId,
        env_name: str | None,
        vcpus: int,
        memory_mib: int,
        disk_gib: int,
        host_dir: str,
        root_authorized_public_key: str | None,
        host_private_key_pem: str,
        host_public_key_openssh: str,
        boot_disk_gib: int,
        slot_count: int,
        port_range_start: int,
        port_range_end: int,
        extra_root_authorized_keys: tuple[str, ...] = (),
        trusted_user_ca_public_key: str | None = None,
        uplink_mbps: int | None = None,
        units: int | None = None,
        box_total_units: int | None = None,
        box_disk_budget_gib: int | None = None,
    ) -> SliceProvisionResult:
        raise NotImplementedError

    def run_on_box(
        self, remote_command: str, *, timeout: float, label: str, is_streaming: bool = False
    ) -> tuple[int | None, str, str]:
        raise NotImplementedError

    def list_instance_names(self) -> set[str]:
        return {observation.instance_name for observation in self.observations}

    def list_instance_observations(self) -> tuple[SliceInstanceObservation, ...]:
        return tuple(self.observations)

    def list_disk_names(self) -> set[str]:
        return set(self.disk_names)

    def destroy_instance(self, instance_id: VpsInstanceId) -> None:
        instance_name = str(instance_id)
        if instance_name in self.failing_resource_names:
            raise SliceCommandError("destroy", 1, f"mock destroy of {instance_name} failed")
        self.observations = [
            observation for observation in self.observations if observation.instance_name != instance_name
        ]
        self.disk_names.discard(f"{instance_name}{SLICE_DISK_SUFFIX}")
        self.destroyed_instance_names.append(instance_name)

    def destroy_disk(self, disk_name: str) -> None:
        if disk_name in self.failing_resource_names:
            raise SliceCommandError("disk delete", 1, f"mock disk delete of {disk_name} failed")
        self.disk_names.discard(disk_name)
        self.destroyed_disk_names.append(disk_name)

    def read_management_trust(self) -> BoxManagementTrust:
        if self.management_trust is None:
            raise NotImplementedError
        return self.management_trust

    def read_box_health_texts(self) -> tuple[str, str]:
        raise NotImplementedError

    def read_storage_volume_state(self) -> StorageVolumeState:
        if self.storage_volume_state is None:
            raise NotImplementedError
        return self.storage_volume_state

    def create_instance(
        self,
        label: str,
        region: str,
        plan: str,
        user_data: str,
        ssh_key_ids: Sequence[str],
        tags: Mapping[str, str],
    ) -> VpsInstanceId:
        raise NotImplementedError

    def get_instance_status(self, instance_id: VpsInstanceId) -> VpsInstanceStatus:
        raise NotImplementedError

    def get_instance_ip(self, instance_id: VpsInstanceId) -> str:
        raise NotImplementedError

    def upload_ssh_key(self, name: str, public_key: str) -> str:
        raise NotImplementedError

    def delete_ssh_key(self, key_id: str) -> None:
        raise NotImplementedError
