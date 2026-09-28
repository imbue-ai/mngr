from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.interfaces.data_types import HostResources
from imbue.mngr_lima.primitives import LimaCpuCount
from imbue.mngr_lima.primitives import LimaDiskSize
from imbue.mngr_lima.primitives import LimaMemoryGib


class LimaVmSize(FrozenModel):
    """The CPU count, memory, and boot-disk size a VM's ``limactl start`` arguments set, each None when unset."""

    cpus: int | None = Field(default=None, description="The `--cpus` value")
    memory_gib: float | None = Field(default=None, description="The `--memory` value, in GiB")
    boot_disk_gib: float | None = Field(default=None, description="The `--disk` value (the boot disk), in GiB")


class ResolvedLimaVmSize(FrozenModel):
    """A VM's effective CPU count, memory, and boot-disk size once lima's own defaults have filled the gaps."""

    cpus: int = Field(description="CPUs the VM boots with")
    memory_gib: float = Field(description="RAM the VM boots with, in GiB")
    boot_disk_gib: float = Field(description="Size of the VM's boot disk, in GiB")


class LimaSizeRequest(FrozenModel):
    """The dimensions a caller wants to set on a lima host; a None dimension is left as it is."""

    cpus: LimaCpuCount | None = Field(default=None, description="CPUs to give the VM")
    memory_gib: LimaMemoryGib | None = Field(default=None, description="RAM to give the VM, in GiB")
    data_disk_size: LimaDiskSize | None = Field(
        default=None, description="Size to grow the btrfs data disk to (it never shrinks)"
    )


class LimaResizeOutcome(FrozenModel):
    """A resize's result: the size now recorded for the host, and whether its VM was already reconfigured to it."""

    resources: HostResources = Field(description="The host's recorded size after the resize")
    is_applied_to_instance: bool = Field(
        description=(
            "True when the stopped VM was reconfigured at once; False when the VM was running, so the "
            "recorded size applies on its next start"
        )
    )
