import pytest

from imbue.minds.desktop_client.data_types import MachineSizeSelection
from imbue.minds.desktop_client.machine_size import DEFAULT_MACHINE_SIZE_BY_LAUNCH_MODE
from imbue.minds.desktop_client.machine_size import MachineNotResizableError
from imbue.minds.desktop_client.machine_size import is_machine_disk_resizable_provider
from imbue.minds.desktop_client.machine_size import is_machine_resizable_provider
from imbue.minds.desktop_client.machine_size import machine_size_create_args
from imbue.minds.desktop_client.machine_size import machine_size_resize_args
from imbue.minds.desktop_client.machine_size import narrow_machine_size_to_launch_mode
from imbue.minds.primitives import LaunchMode

_FULL = MachineSizeSelection(cpu_count=4, memory_gb=16, disk_gb=200)


def test_every_launch_mode_sized_by_numbers_has_a_default_the_form_can_offer() -> None:
    assert set(DEFAULT_MACHINE_SIZE_BY_LAUNCH_MODE) == {LaunchMode.DOCKER, LaunchMode.LIMA, LaunchMode.MODAL}
    for launch_mode, default_size in DEFAULT_MACHINE_SIZE_BY_LAUNCH_MODE.items():
        assert narrow_machine_size_to_launch_mode(default_size, launch_mode) == default_size


@pytest.mark.parametrize(
    ("launch_mode", "expected"),
    [
        (LaunchMode.DOCKER, MachineSizeSelection(cpu_count=4, memory_gb=16)),
        (LaunchMode.MODAL, MachineSizeSelection(cpu_count=4, memory_gb=16)),
        (LaunchMode.LIMA, _FULL),
        (LaunchMode.VULTR, None),
        (LaunchMode.IMBUE_CLOUD, None),
        (LaunchMode.AWS, None),
        (LaunchMode.GCP, None),
        (LaunchMode.AZURE, None),
    ],
)
def test_narrow_machine_size_keeps_only_the_dimensions_the_mode_takes(
    launch_mode: LaunchMode, expected: MachineSizeSelection | None
) -> None:
    assert narrow_machine_size_to_launch_mode(_FULL, launch_mode) == expected


def test_narrow_machine_size_is_none_when_nothing_the_mode_takes_was_chosen() -> None:
    assert narrow_machine_size_to_launch_mode(MachineSizeSelection(disk_gb=200), LaunchMode.DOCKER) is None
    assert narrow_machine_size_to_launch_mode(MachineSizeSelection(), LaunchMode.LIMA) is None


def test_machine_size_create_args_speak_each_providers_own_flags() -> None:
    assert machine_size_create_args(LaunchMode.DOCKER, _FULL) == ["-s", "--cpus=4", "-s", "--memory=16g"]
    assert machine_size_create_args(LaunchMode.LIMA, _FULL) == [
        "-s",
        "--cpus=4",
        "-s",
        "--memory=16",
        "-S",
        "providers.lima.host_data_disk_size=200GiB",
    ]
    assert machine_size_create_args(LaunchMode.MODAL, _FULL) == ["-b", "cpu=4", "-b", "memory=16"]
    assert machine_size_create_args(LaunchMode.DOCKER, MachineSizeSelection(memory_gb=8)) == ["-s", "--memory=8g"]
    assert machine_size_create_args(LaunchMode.IMBUE_CLOUD, _FULL) == []
    assert machine_size_create_args(LaunchMode.AWS, _FULL) == []


def test_only_local_docker_and_lima_machines_are_resizable_and_only_lima_grows_its_disk() -> None:
    assert is_machine_resizable_provider("docker")
    assert is_machine_resizable_provider("lima")
    assert not is_machine_resizable_provider("modal")
    assert not is_machine_resizable_provider("imbue_cloud_alice")
    assert not is_machine_resizable_provider("byok-aws-team")
    assert is_machine_disk_resizable_provider("lima")
    assert not is_machine_disk_resizable_provider("docker")


def test_machine_size_resize_args_name_the_providers_own_command() -> None:
    host_id = "host-" + "a" * 32
    assert machine_size_resize_args("docker", host_id, _FULL) == [
        "docker",
        "resize",
        host_id,
        "--cpus",
        "4",
        "--memory",
        "16g",
    ]
    assert machine_size_resize_args("lima", host_id, MachineSizeSelection(disk_gb=200)) == [
        "lima",
        "resize",
        host_id,
        "--disk",
        "200",
    ]
    with pytest.raises(MachineNotResizableError, match="modal"):
        machine_size_resize_args("modal", host_id, _FULL)
