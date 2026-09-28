import pytest

from imbue.mngr.primitives import DockerCpuCount
from imbue.mngr.primitives import DockerMemorySize
from imbue.mngr.providers.docker.data_types import ContainerSize
from imbue.mngr.providers.docker.data_types import ContainerSizeRequest
from imbue.mngr.providers.docker.data_types import DockerDaemonTotals
from imbue.mngr.providers.docker.sizing import apply_size_request
from imbue.mngr.providers.docker.sizing import clamp_cpus_to_daemon
from imbue.mngr.providers.docker.sizing import docker_update_args
from imbue.mngr.providers.docker.sizing import host_resources_for_container
from imbue.mngr.providers.docker.sizing import parse_container_size
from imbue.mngr.providers.docker.sizing import render_size_start_args
from imbue.mngr.providers.docker.sizing import strip_size_start_args
from imbue.mngr.utils.testing import allow_warnings

_DAEMON_TOTALS = DockerDaemonTotals(cpu_count=8, memory_bytes=32 * 1024**3)


@pytest.mark.parametrize(
    ("start_args", "expected_cpus", "expected_memory"),
    [
        ((), None, None),
        (("--tmpfs", "/run"), None, None),
        (("--cpus=2", "--memory=4g"), 2.0, "4g"),
        (("--cpus", "1.5", "--memory", "512m"), 1.5, "512m"),
        (("-m", "1g"), None, "1g"),
        (("-m=1g",), None, "1g"),
        (("-m1g",), None, "1g"),
        (("--memory-swap=8g", "--memory-reservation=1g"), None, None),
        (("--cpus=1", "--memory=1g", "--cpus=4", "--memory=8g"), 4.0, "8g"),
        (("--cpus",), None, None),
    ],
)
def test_parse_container_size_reads_every_docker_spelling_with_last_flag_winning(
    start_args: tuple[str, ...], expected_cpus: float | None, expected_memory: str | None
) -> None:
    size = parse_container_size(start_args)
    assert size.cpus == expected_cpus
    assert size.memory == expected_memory


def test_parse_container_size_keeps_a_recorded_memory_swap() -> None:
    size = parse_container_size(("--memory=4g", "--memory-swap=6g"))
    assert size.memory == "4g"
    assert size.memory_swap == "6g"


def test_parse_container_size_ignores_values_docker_could_not_have_accepted() -> None:
    with allow_warnings():
        size = parse_container_size(("--cpus=lots", "--memory=plenty", "--memory-swap=some"))
    assert size == ContainerSize()


def test_strip_size_start_args_removes_only_the_requested_dimensions() -> None:
    start_args = ("--tmpfs", "/run", "--cpus", "2", "-m4g", "--memory-swap=4g", "--workdir=/")
    assert strip_size_start_args(start_args, is_cpus_stripped=True, is_memory_stripped=False) == (
        "--tmpfs",
        "/run",
        "-m4g",
        "--memory-swap=4g",
        "--workdir=/",
    )
    assert strip_size_start_args(start_args, is_cpus_stripped=False, is_memory_stripped=True) == (
        "--tmpfs",
        "/run",
        "--cpus",
        "2",
        "--workdir=/",
    )
    assert strip_size_start_args(start_args, is_cpus_stripped=False, is_memory_stripped=False) == start_args


def test_render_size_start_args_caps_swap_at_the_memory_cap() -> None:
    request = ContainerSizeRequest(cpus=DockerCpuCount(4), memory=DockerMemorySize("8g"))
    assert render_size_start_args(request) == ("--cpus=4", "--memory=8g", "--memory-swap=8g")
    assert render_size_start_args(ContainerSizeRequest(cpus=DockerCpuCount(2))) == ("--cpus=2",)
    assert render_size_start_args(ContainerSizeRequest()) == ()


def test_apply_size_request_replaces_the_requested_dimension_and_keeps_the_rest() -> None:
    start_args = ("--cpus=2", "--memory=4g", "--memory-swap=4g", "--workdir=/")
    resized = apply_size_request(start_args, ContainerSizeRequest(memory=DockerMemorySize("16g")))
    assert resized == ("--cpus=2", "--workdir=/", "--memory=16g", "--memory-swap=16g")
    assert parse_container_size(resized) == ContainerSize(
        cpus=2.0, memory=DockerMemorySize("16g"), memory_swap=DockerMemorySize("16g")
    )


def test_apply_size_request_adds_caps_to_an_uncapped_container() -> None:
    resized = apply_size_request(("--workdir=/",), ContainerSizeRequest(cpus=DockerCpuCount(1)))
    assert resized == ("--workdir=/", "--cpus=1")


def test_docker_update_args_always_sends_memory_and_swap_together() -> None:
    assert docker_update_args(ContainerSize()) == ()
    assert docker_update_args(ContainerSize(cpus=1.5)) == ("--cpus", "1.5")
    assert docker_update_args(ContainerSize(cpus=2.0)) == ("--cpus", "2")
    assert docker_update_args(ContainerSize(memory=DockerMemorySize("4g"))) == (
        "--memory",
        "4g",
        "--memory-swap",
        "4g",
    )
    assert docker_update_args(ContainerSize(memory=DockerMemorySize("4g"), memory_swap=DockerMemorySize("6g"))) == (
        "--memory",
        "4g",
        "--memory-swap",
        "6g",
    )


@pytest.mark.parametrize(("requested", "expected"), [(4, 4), (8, 8), (9, 8), (100, 8)])
def test_clamp_cpus_to_daemon_never_exceeds_the_daemon_cpu_count(requested: int, expected: int) -> None:
    assert clamp_cpus_to_daemon(requested, _DAEMON_TOTALS) == expected


def test_host_resources_for_container_reports_caps_and_fills_uncapped_dimensions_from_the_daemon() -> None:
    capped = host_resources_for_container(ContainerSize(cpus=2.0, memory=DockerMemorySize("4g")), _DAEMON_TOTALS)
    assert capped.cpu.count == 2
    assert capped.memory_gb == 4.0
    assert capped.disk_gb is None

    uncapped = host_resources_for_container(ContainerSize(), _DAEMON_TOTALS)
    assert uncapped.cpu.count == 8
    assert uncapped.memory_gb == 32.0

    memory_only = host_resources_for_container(ContainerSize(memory=DockerMemorySize("512m")), _DAEMON_TOTALS)
    assert memory_only.cpu.count == 8
    assert memory_only.memory_gb == 0.5


def test_host_resources_for_container_rounds_a_fractional_cpu_cap_up() -> None:
    assert host_resources_for_container(ContainerSize(cpus=1.5), _DAEMON_TOTALS).cpu.count == 2
    assert host_resources_for_container(ContainerSize(cpus=0.25), _DAEMON_TOTALS).cpu.count == 1
