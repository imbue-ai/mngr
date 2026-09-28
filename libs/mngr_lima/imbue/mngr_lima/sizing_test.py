import pytest

from imbue.mngr.interfaces.data_types import CpuResources
from imbue.mngr.interfaces.data_types import HostResources
from imbue.mngr.utils.testing import allow_warnings
from imbue.mngr_lima.data_types import LimaSizeRequest
from imbue.mngr_lima.data_types import ResolvedLimaVmSize
from imbue.mngr_lima.primitives import LimaCpuCount
from imbue.mngr_lima.primitives import LimaDiskSize
from imbue.mngr_lima.primitives import LimaMemoryGib
from imbue.mngr_lima.sizing import apply_size_request_to_start_args
from imbue.mngr_lima.sizing import format_gib
from imbue.mngr_lima.sizing import host_resources_for_lima_host
from imbue.mngr_lima.sizing import is_same_vm_size
from imbue.mngr_lima.sizing import parse_vm_size_start_args
from imbue.mngr_lima.sizing import resolve_vm_size
from imbue.mngr_lima.sizing import resolved_vm_size_from_resources
from imbue.mngr_lima.sizing import strip_size_start_args
from imbue.mngr_lima.sizing import vm_size_from_limactl_instance


@pytest.mark.parametrize(
    ("start_args", "expected_cpus", "expected_memory_gib", "expected_boot_disk_gib"),
    [
        ((), None, None, None),
        (("--vm-type=vz",), None, None, None),
        (("--cpus=2", "--memory=4", "--disk=20"), 2, 4.0, 20.0),
        (("--cpus", "8", "--memory", "1.5"), 8, 1.5, None),
        (("--cpus=1", "--cpus=4", "--memory=2", "--memory=16"), 4, 16.0, None),
        (("--cpus",), None, None, None),
    ],
)
def test_parse_vm_size_start_args_reads_both_spellings_with_last_flag_winning(
    start_args: tuple[str, ...],
    expected_cpus: int | None,
    expected_memory_gib: float | None,
    expected_boot_disk_gib: float | None,
) -> None:
    size = parse_vm_size_start_args(start_args)
    assert size.cpus == expected_cpus
    assert size.memory_gib == expected_memory_gib
    assert size.boot_disk_gib == expected_boot_disk_gib


@pytest.mark.parametrize("value", ["lots", "0", "-2", "inf"])
def test_parse_vm_size_start_args_ignores_values_lima_could_not_have_accepted(value: str) -> None:
    with allow_warnings():
        size = parse_vm_size_start_args((f"--cpus={value}", f"--memory={value}"))
    assert size.cpus is None
    assert size.memory_gib is None


def test_strip_size_start_args_removes_only_the_requested_dimensions() -> None:
    start_args = ("--cpus=2", "--memory", "4", "--disk=20", "--vm-type=vz")
    assert strip_size_start_args(start_args, is_cpus_stripped=True, is_memory_stripped=False) == (
        "--memory",
        "4",
        "--disk=20",
        "--vm-type=vz",
    )
    assert strip_size_start_args(start_args, is_cpus_stripped=False, is_memory_stripped=True) == (
        "--cpus=2",
        "--disk=20",
        "--vm-type=vz",
    )
    assert strip_size_start_args(start_args, is_cpus_stripped=False, is_memory_stripped=False) == start_args


def test_apply_size_request_replaces_the_requested_dimensions_and_keeps_the_boot_disk() -> None:
    start_args = ("--cpus=2", "--memory=4", "--disk=20")
    request = LimaSizeRequest(cpus=LimaCpuCount(4), memory_gib=LimaMemoryGib(8), data_disk_size=LimaDiskSize("200GiB"))
    assert apply_size_request_to_start_args(start_args, request) == ("--disk=20", "--cpus=4", "--memory=8")
    assert apply_size_request_to_start_args(start_args, LimaSizeRequest(memory_gib=LimaMemoryGib(1.5))) == (
        "--cpus=2",
        "--disk=20",
        "--memory=1.5",
    )
    assert apply_size_request_to_start_args((), LimaSizeRequest(cpus=LimaCpuCount(3))) == ("--cpus=3",)


def test_format_gib_keeps_whole_numbers_whole() -> None:
    assert format_gib(8.0) == "8"
    assert format_gib(1.5) == "1.5"


def test_resolve_vm_size_prefers_start_args_then_the_instance_config_then_lima_defaults() -> None:
    lima_config = {"cpus": 6, "memory": "12GiB", "disk": "50GiB"}
    assert resolve_vm_size(("--cpus=2",), lima_config) == ResolvedLimaVmSize(
        cpus=2, memory_gib=12.0, boot_disk_gib=50.0
    )
    assert resolve_vm_size((), {}) == ResolvedLimaVmSize(cpus=4, memory_gib=4.0, boot_disk_gib=100.0)
    assert resolve_vm_size((), {"memory": 2, "disk": 30.5}) == ResolvedLimaVmSize(
        cpus=4, memory_gib=2.0, boot_disk_gib=30.5
    )


def test_resolve_vm_size_ignores_unparseable_config_values() -> None:
    with allow_warnings():
        size = resolve_vm_size((), {"cpus": True, "memory": "plenty", "disk": ["big"]})
    assert size == ResolvedLimaVmSize(cpus=4, memory_gib=4.0, boot_disk_gib=100.0)


def test_vm_size_from_limactl_instance_converts_bytes_to_gib() -> None:
    instance = {"name": "x", "cpus": 2, "memory": 4 * 1024**3, "disk": 20 * 1024**3}
    assert vm_size_from_limactl_instance(instance) == ResolvedLimaVmSize(cpus=2, memory_gib=4.0, boot_disk_gib=20.0)
    assert vm_size_from_limactl_instance({"name": "x", "status": "Stopped"}) is None


def test_is_same_vm_size_compares_only_cpus_and_memory() -> None:
    first = ResolvedLimaVmSize(cpus=2, memory_gib=4.0, boot_disk_gib=20.0)
    assert is_same_vm_size(first, ResolvedLimaVmSize(cpus=2, memory_gib=4.0, boot_disk_gib=100.0))
    assert not is_same_vm_size(first, ResolvedLimaVmSize(cpus=3, memory_gib=4.0, boot_disk_gib=20.0))
    assert not is_same_vm_size(first, ResolvedLimaVmSize(cpus=2, memory_gib=8.0, boot_disk_gib=20.0))


def test_host_resources_report_the_data_disk_when_there_is_one_else_the_boot_disk() -> None:
    vm_size = ResolvedLimaVmSize(cpus=2, memory_gib=4.0, boot_disk_gib=20.0)
    with_data_disk = host_resources_for_lima_host(vm_size, LimaDiskSize("100GiB"))
    assert with_data_disk == HostResources(cpu=CpuResources(count=2), memory_gb=4.0, disk_gb=100.0, gpu=None)
    assert host_resources_for_lima_host(vm_size, None).disk_gb == 20.0
    assert resolved_vm_size_from_resources(with_data_disk) == ResolvedLimaVmSize(
        cpus=2, memory_gib=4.0, boot_disk_gib=100.0
    )
