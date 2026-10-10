from imbue.mngr_vps.primitives import VpsDiskGb
from imbue.mngr_vps.primitives import VpsMemoryMib
from imbue.mngr_vps.primitives import VpsVcpuCount
from imbue.mngr_vps.sizing import VpsInstanceShape
from imbue.mngr_vps.sizing import host_resources_for_shape
from imbue.mngr_vps.sizing import legacy_shape_with_root_disk


def test_host_resources_for_shape_reports_vcpus_memory_in_gib_and_root_disk() -> None:
    shape = VpsInstanceShape(vcpu_count=VpsVcpuCount(2), memory_mib=VpsMemoryMib(8192), root_disk_gb=VpsDiskGb(30))

    resources = host_resources_for_shape(shape)

    assert resources.cpu.count == 2
    assert resources.memory_gb == 8.0
    assert resources.disk_gb == 30.0
    assert resources.gpu is None


def test_host_resources_for_shape_reports_fractional_memory_and_no_disk() -> None:
    shape = VpsInstanceShape(vcpu_count=VpsVcpuCount(1), memory_mib=VpsMemoryMib(1536), root_disk_gb=None)

    resources = host_resources_for_shape(shape)

    assert resources.cpu.count == 1
    assert resources.memory_gb == 1.5
    assert resources.disk_gb is None


_PLAN_SHAPES = {
    "small": VpsInstanceShape(vcpu_count=VpsVcpuCount(2), memory_mib=VpsMemoryMib(2048), root_disk_gb=None)
}


def test_legacy_shape_with_root_disk_fills_the_configured_disk_into_a_tabled_plan() -> None:
    shape = legacy_shape_with_root_disk(_PLAN_SHAPES, "small", VpsDiskGb(40))

    assert shape == VpsInstanceShape(
        vcpu_count=VpsVcpuCount(2), memory_mib=VpsMemoryMib(2048), root_disk_gb=VpsDiskGb(40)
    )


def test_legacy_shape_with_root_disk_is_none_for_a_plan_outside_the_table() -> None:
    assert legacy_shape_with_root_disk(_PLAN_SHAPES, "never-seen", VpsDiskGb(40)) is None
