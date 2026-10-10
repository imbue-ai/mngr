from collections.abc import Mapping

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from imbue.mngr.interfaces.data_types import CpuResources
from imbue.mngr.interfaces.data_types import HostResources
from imbue.mngr_vps.primitives import VpsDiskGb
from imbue.mngr_vps.primitives import VpsMemoryMib
from imbue.mngr_vps.primitives import VpsVcpuCount

_MIB_PER_GIB: int = 1024


class VpsInstanceShape(FrozenModel):
    """The vCPU count, RAM, and root disk of a cloud instance shape, as the cloud reports them.

    Recorded on the host record at create, so the host's size is answered from
    the record alone afterwards (for a stopped host as much as a running one).
    """

    vcpu_count: VpsVcpuCount = Field(description="vCPUs of the instance shape")
    memory_mib: VpsMemoryMib = Field(description="RAM of the instance shape, in MiB")
    root_disk_gb: VpsDiskGb | None = Field(
        default=None, description="Root disk size in GB, or None when the cloud does not report one"
    )


@pure
def host_resources_for_shape(shape: VpsInstanceShape) -> HostResources:
    """What the VM has: its shape's vCPUs, its RAM in GiB, and its root disk."""
    return HostResources(
        cpu=CpuResources(count=int(shape.vcpu_count), frequency_ghz=None),
        memory_gb=shape.memory_mib / _MIB_PER_GIB,
        disk_gb=float(shape.root_disk_gb) if shape.root_disk_gb is not None else None,
        gpu=None,
    )


@pure
def legacy_shape_with_root_disk(
    plan_shapes: Mapping[str, VpsInstanceShape], plan: str, root_disk_gb: VpsDiskGb
) -> VpsInstanceShape | None:
    """The shape ``plan_shapes`` holds for ``plan`` with ``root_disk_gb`` filled in, or None for a plan outside the table.

    For a host record that predates shape recording on a cloud whose root disk
    is a config knob rather than part of the plan: the table holds no disk size,
    and the configured root disk is the best estimate of what the host was
    created with.
    """
    shape = plan_shapes.get(plan)
    if shape is None:
        return None
    return shape.model_copy_update(to_update(shape.field_ref().root_disk_gb, root_disk_gb))
