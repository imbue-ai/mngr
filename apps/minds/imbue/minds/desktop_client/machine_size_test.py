import pytest

from imbue.minds.desktop_client.data_types import MachineSizeSelection
from imbue.minds.desktop_client.machine_size import DEFAULT_MACHINE_SIZE_BY_LAUNCH_MODE
from imbue.minds.desktop_client.machine_size import ImbueCloudMachineQuotaUsage
from imbue.minds.desktop_client.machine_size import MachineNotResizableError
from imbue.minds.desktop_client.machine_size import clamp_machine_size_to_cpu_count
from imbue.minds.desktop_client.machine_size import compute_imbue_cloud_resize_bounds
from imbue.minds.desktop_client.machine_size import is_imbue_cloud_machine_running
from imbue.minds.desktop_client.machine_size import is_imbue_cloud_machine_transitioning
from imbue.minds.desktop_client.machine_size import is_imbue_cloud_resize_offered_to_plan
from imbue.minds.desktop_client.machine_size import is_machine_disk_resizable_provider
from imbue.minds.desktop_client.machine_size import is_machine_resizable_provider
from imbue.minds.desktop_client.machine_size import machine_size_create_args
from imbue.minds.desktop_client.machine_size import machine_size_resize_args
from imbue.minds.desktop_client.machine_size import narrow_machine_size_to_launch_mode
from imbue.minds.primitives import LaunchMode
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import MACHINE_UNITS_STEP
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import MAX_MACHINE_UNITS
from imbue.mngr_imbue_cloud.wire_types import WorkspaceStatus

_FULL = MachineSizeSelection(cpu_count=4, memory_gb=16, disk_gb=200)


def test_every_launch_mode_sized_by_numbers_has_a_default_the_form_can_offer() -> None:
    assert set(DEFAULT_MACHINE_SIZE_BY_LAUNCH_MODE) == {LaunchMode.DOCKER, LaunchMode.LIMA, LaunchMode.MODAL}
    for launch_mode, default_size in DEFAULT_MACHINE_SIZE_BY_LAUNCH_MODE.items():
        assert narrow_machine_size_to_launch_mode(default_size, launch_mode) == default_size


def test_clamping_to_a_cpu_count_lowers_only_a_cpu_count_above_it() -> None:
    assert clamp_machine_size_to_cpu_count(_FULL, 2) == MachineSizeSelection(cpu_count=2, memory_gb=16, disk_gb=200)
    assert clamp_machine_size_to_cpu_count(_FULL, 4) == _FULL
    assert clamp_machine_size_to_cpu_count(_FULL, 8) == _FULL
    unsized = MachineSizeSelection(memory_gb=16)
    assert clamp_machine_size_to_cpu_count(unsized, 1) == unsized


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
    assert not is_machine_resizable_provider("aws_" + "0" * 32)
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


def test_imbue_cloud_resize_is_offered_to_the_ally_plan_only() -> None:
    assert is_imbue_cloud_resize_offered_to_plan("ally")
    assert not is_imbue_cloud_resize_offered_to_plan("explorer")
    assert not is_imbue_cloud_resize_offered_to_plan("free")
    assert not is_imbue_cloud_resize_offered_to_plan("guest")


# An ally account (80 units, 1400 GB of disk) with one other running 8-unit
# machine and 28 GB of other disk counted.
_ALLY_QUOTAS = ImbueCloudMachineQuotaUsage(
    max_active_machine_units=80,
    active_machine_units=16,
    max_total_machine_disk_gb=1400,
    total_machine_disk_gb=56,
)


def test_a_running_machines_own_units_are_handed_back_before_its_headroom_is_measured() -> None:
    bounds = compute_imbue_cloud_resize_bounds(
        status=WorkspaceStatus.RUNNING,
        memory_units=8,
        target_memory_units=None,
        disk_gb=28,
        target_disk_gb=None,
        quotas=_ALLY_QUOTAS,
    )

    # 80 - 8 (the other machine) = 72 units of headroom, in steps of 8.
    assert bounds.memory_choices_gb == (8, 16, 24, 32, 40, 48, 56, 64, 72)
    assert bounds.is_memory_capped_by_plan is True
    assert bounds.plan_memory_limit_gb == 80
    assert bounds.other_machines_memory_gb == 8
    # 1400 - 28 (the other machine's disk) = 1372 GB.
    assert bounds.max_disk_gb == 1372
    assert bounds.plan_disk_limit_gb == 1400
    assert bounds.other_machines_disk_gb == 28


def test_a_pending_target_is_what_a_running_machine_counts_at() -> None:
    quotas = ImbueCloudMachineQuotaUsage(
        max_active_machine_units=80,
        active_machine_units=24,
        max_total_machine_disk_gb=1400,
        total_machine_disk_gb=84,
    )

    bounds = compute_imbue_cloud_resize_bounds(
        status=WorkspaceStatus.RUNNING,
        memory_units=8,
        target_memory_units=16,
        disk_gb=28,
        target_disk_gb=56,
        quotas=quotas,
    )

    # The other machine uses 24 - max(8, 16) = 8 units and 84 - max(28, 56) = 28 GB.
    assert bounds.other_machines_memory_gb == 8
    assert bounds.memory_choices_gb[-1] == 72
    assert bounds.other_machines_disk_gb == 28
    assert bounds.max_disk_gb == 1372


def test_a_stopped_machine_counts_no_units_but_its_disk_until_it_starts_again() -> None:
    bounds = compute_imbue_cloud_resize_bounds(
        status=WorkspaceStatus.STOPPED,
        memory_units=8,
        target_memory_units=None,
        disk_gb=28,
        target_disk_gb=None,
        quotas=_ALLY_QUOTAS,
    )

    # Both running machines' 16 units stay counted: 80 - 16 = 64 units of headroom.
    assert bounds.memory_choices_gb == (8, 16, 24, 32, 40, 48, 56, 64)
    assert bounds.other_machines_memory_gb == 16
    # Its own 28 GB is handed back; the other machine's 28 GB stays counted.
    assert bounds.other_machines_disk_gb == 28
    assert bounds.max_disk_gb == 1372


def test_the_fleets_largest_size_caps_the_choices_when_the_plan_would_grant_more() -> None:
    quotas = ImbueCloudMachineQuotaUsage(
        max_active_machine_units=400,
        active_machine_units=8,
        max_total_machine_disk_gb=1400,
        total_machine_disk_gb=28,
    )

    bounds = compute_imbue_cloud_resize_bounds(
        status=WorkspaceStatus.RUNNING,
        memory_units=8,
        target_memory_units=None,
        disk_gb=28,
        target_disk_gb=None,
        quotas=quotas,
    )

    assert bounds.memory_choices_gb[-1] == MAX_MACHINE_UNITS
    assert len(bounds.memory_choices_gb) == MAX_MACHINE_UNITS // MACHINE_UNITS_STEP
    assert bounds.is_memory_capped_by_plan is False


def test_a_running_machine_the_quota_no_longer_holds_can_only_keep_its_size() -> None:
    # A plan lowered below usage: the connector counts even a shrink at the
    # machine's 16 units, so it refuses every change, and no disk growth is
    # left either.
    quotas = ImbueCloudMachineQuotaUsage(
        max_active_machine_units=8,
        active_machine_units=16,
        max_total_machine_disk_gb=40,
        total_machine_disk_gb=56,
    )

    bounds = compute_imbue_cloud_resize_bounds(
        status=WorkspaceStatus.RUNNING,
        memory_units=16,
        target_memory_units=None,
        disk_gb=56,
        target_disk_gb=None,
        quotas=quotas,
    )

    assert bounds.memory_choices_gb == (16,)
    assert bounds.max_disk_gb == 56


def test_a_stopped_machine_the_quota_no_longer_holds_may_shrink_to_what_its_start_would_be_granted() -> None:
    # The other running machine uses 8 of the plan's 16 units: a shrink to 8
    # lets this 16-unit machine start again, so it is offered beside its size.
    quotas = ImbueCloudMachineQuotaUsage(
        max_active_machine_units=16,
        active_machine_units=8,
        max_total_machine_disk_gb=1400,
        total_machine_disk_gb=56,
    )

    bounds = compute_imbue_cloud_resize_bounds(
        status=WorkspaceStatus.STOPPED,
        memory_units=16,
        target_memory_units=None,
        disk_gb=28,
        target_disk_gb=None,
        quotas=quotas,
    )

    assert bounds.memory_choices_gb == (8, 16)


def test_a_stopped_machine_with_no_headroom_can_only_keep_its_size() -> None:
    quotas = ImbueCloudMachineQuotaUsage(
        max_active_machine_units=8,
        active_machine_units=8,
        max_total_machine_disk_gb=140,
        total_machine_disk_gb=56,
    )

    bounds = compute_imbue_cloud_resize_bounds(
        status=WorkspaceStatus.STOPPED,
        memory_units=8,
        target_memory_units=None,
        disk_gb=28,
        target_disk_gb=None,
        quotas=quotas,
    )

    assert bounds.memory_choices_gb == (8,)
    assert bounds.other_machines_memory_gb == 8
    assert bounds.max_disk_gb == 140 - 28


@pytest.mark.parametrize(
    ("status", "is_transitioning", "is_running"),
    [
        (WorkspaceStatus.RUNNING, False, True),
        (WorkspaceStatus.STOPPED, False, False),
        (WorkspaceStatus.STOPPING, True, False),
        (WorkspaceStatus.STARTING, True, False),
        (WorkspaceStatus.CRASHED, False, False),
        (WorkspaceStatus.UNKNOWN, False, False),
    ],
)
def test_the_lifecycle_statuses_a_resize_waits_out_and_the_one_a_restart_applies_now(
    status: WorkspaceStatus, is_transitioning: bool, is_running: bool
) -> None:
    assert is_imbue_cloud_machine_transitioning(status) is is_transitioning
    assert is_imbue_cloud_machine_running(status) is is_running
