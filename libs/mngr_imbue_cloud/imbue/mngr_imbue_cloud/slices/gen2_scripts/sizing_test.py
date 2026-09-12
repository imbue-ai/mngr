import pytest

from imbue.mngr_imbue_cloud.slices.gen2_scripts.errors import InvalidMachineSizeError
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import DATA_DISK_BASE_GIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import DEFAULT_MACHINE_UNITS
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import GEN2_BASE_IMAGE_GIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import GEN2_BOOT_DISK_GIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import GEN2_BOOT_PARTITION_GIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import GEN2_IMAGE_TAR_CACHE_GIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import GEN2_ROOT_PARTITION_GIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import GEN2_STAGING_MARGIN_GIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import GEN2_STORAGE_RESERVE_GIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import GEN2_SWAPFILE_GIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import GEN2_UPLINK_SHAPING_PERCENT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import GUEST_RAM_HOLDBACK_MIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import PER_VM_RAM_OVERHEAD_MIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import SLICE_BOOT_DISK_GIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_box_total_units
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_box_unit_budget_mib
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_default_machine_capacity
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_gen1_migrated_data_disk_gib
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_gen2_disk_budget_gib
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_gen2_storage_partition_estimate_gib
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_machine_data_disk_gib
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_machine_guest_memory_mib
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_machine_memory_footprint_mib
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_machine_vcpus
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import is_allowed_machine_units


def test_allowed_machine_units_are_the_multiples_of_the_step_up_to_the_max() -> None:
    assert is_allowed_machine_units(8)
    assert is_allowed_machine_units(16)
    assert is_allowed_machine_units(128)
    assert not is_allowed_machine_units(0)
    assert not is_allowed_machine_units(4)
    assert not is_allowed_machine_units(12)
    assert not is_allowed_machine_units(136)
    assert not is_allowed_machine_units(-8)


def test_box_unit_budget_holds_the_historical_default_machine_count() -> None:
    # The uniform model is the special case: a 128GB box's budget fits exactly
    # its historical 14 default-size machines (and not a 15th).
    budget_mib = compute_box_unit_budget_mib(128)
    default_footprint_mib = compute_machine_memory_footprint_mib(8)
    assert budget_mib // default_footprint_mib == 14
    assert compute_default_machine_capacity(128) == 14


def test_box_total_units_subtracts_the_host_reserve_and_refuses_tiny_boxes() -> None:
    assert compute_box_total_units(128) == 120
    with pytest.raises(InvalidMachineSizeError):
        compute_box_total_units(8)


def test_machine_footprint_refuses_a_non_positive_size() -> None:
    assert compute_machine_memory_footprint_mib(8) == 8 * 1024 + PER_VM_RAM_OVERHEAD_MIB
    with pytest.raises(InvalidMachineSizeError):
        compute_machine_memory_footprint_mib(0)


def test_machine_vcpus_are_proportional_floored_at_one_and_capped_at_the_threads() -> None:
    # A default machine on the standard 16-thread/128GB box at 2.0 overcommit.
    assert compute_machine_vcpus(16, 2.0, 8, 120) == 2
    # A 64-unit machine gets a proportionally larger share.
    assert compute_machine_vcpus(16, 2.0, 64, 120) == 16
    # The share never exceeds the box's real thread count.
    assert compute_machine_vcpus(16, 2.0, 120, 120) == 16
    # A tiny share still yields at least one vCPU.
    assert compute_machine_vcpus(4, 1.0, 8, 1000) == 1
    with pytest.raises(InvalidMachineSizeError):
        compute_machine_vcpus(0, 2.0, 8, 120)
    with pytest.raises(InvalidMachineSizeError):
        compute_machine_vcpus(16, 2.0, 8, 0)


def test_machine_data_disk_is_the_base_plus_the_per_unit_factor_rounded_up() -> None:
    # 16 GiB base (the host's ~13 GiB docker image in containerd's root plus the
    # 4 GiB system reserve outside the host quota) + 3.5 GiB per unit.
    assert compute_machine_data_disk_gib(8) == 16 + 28
    assert compute_machine_data_disk_gib(16) == 16 + 56
    # A non-multiple unit count (the architectural floor is below the step)
    # still rounds up to whole GiB.
    assert compute_machine_data_disk_gib(2) == 16 + 7
    with pytest.raises(InvalidMachineSizeError):
        compute_machine_data_disk_gib(0)


def test_gen1_migrated_data_disk_is_the_gen1_disk_plus_the_gen2_base() -> None:
    # The cutover grows a transplanted gen-1 data disk by the gen-2 base (the
    # engines' roots + the system reserve), so the machine keeps its home
    # capacity; a 28 GiB gen-1 disk lands exactly on the gen-2 default size.
    assert compute_gen1_migrated_data_disk_gib(28) == 28 + DATA_DISK_BASE_GIB
    assert compute_gen1_migrated_data_disk_gib(28) == compute_machine_data_disk_gib(DEFAULT_MACHINE_UNITS)
    with pytest.raises(InvalidMachineSizeError):
        compute_gen1_migrated_data_disk_gib(0)


def test_gen2_storage_reserve_is_the_sum_of_its_named_parts() -> None:
    # 32 GiB swapfile + 16 GiB image tar cache + 4 GiB base image + 12 GiB margin.
    assert GEN2_STORAGE_RESERVE_GIB == 64
    assert (
        GEN2_SWAPFILE_GIB + GEN2_IMAGE_TAR_CACHE_GIB + GEN2_BASE_IMAGE_GIB + GEN2_STAGING_MARGIN_GIB
        == GEN2_STORAGE_RESERVE_GIB
    )


def test_gen2_disk_budget_subtracts_the_named_reserve_from_the_measured_partition() -> None:
    # A measured 879 GiB XFS partition (a 1 TB box after root + boot) yields 815 GiB.
    assert compute_gen2_disk_budget_gib(879) == 879 - 64
    # Exactly the reserve leaves nothing to sell.
    with pytest.raises(InvalidMachineSizeError):
        compute_gen2_disk_budget_gib(64)


def test_gen2_storage_partition_estimate_subtracts_the_fixed_partitions() -> None:
    assert GEN2_ROOT_PARTITION_GIB == 20
    assert GEN2_BOOT_PARTITION_GIB == 1
    # The pre-delivery guard's view of a 1000 GB catalog figure: 979 GiB of XFS,
    # a 915 GiB budget once the reserve comes off.
    assert compute_gen2_storage_partition_estimate_gib(1000) == 979
    assert compute_gen2_disk_budget_gib(compute_gen2_storage_partition_estimate_gib(1000)) == 915
    with pytest.raises(InvalidMachineSizeError):
        compute_gen2_storage_partition_estimate_gib(21)


def test_compute_machine_guest_memory_mib_holds_back_the_per_machine_reserve() -> None:
    # An 8-unit machine's cap is 8192 + 512 MiB; the guest boots with 8192 - 512 so
    # qemu's own memory fits under the cap without overcommitting the box.
    assert compute_machine_guest_memory_mib(8) == 8192 - GUEST_RAM_HOLDBACK_MIB
    assert compute_machine_guest_memory_mib(8) + GUEST_RAM_HOLDBACK_MIB + PER_VM_RAM_OVERHEAD_MIB == 8 * 1024 + 512
    with pytest.raises(InvalidMachineSizeError):
        compute_machine_guest_memory_mib(0)


def test_boot_disk_constants_match_the_deployed_fleet() -> None:
    # Every deployed gen-1 slice was carved with a 32 GiB boot disk and migration
    # 039 derives gen-1 data-disk sizes from that figure; gen-2 keeps the OS-only
    # 10 GiB disk because docker lives on its data disk.
    assert SLICE_BOOT_DISK_GIB == 32
    assert GEN2_BOOT_DISK_GIB == 10


def test_shaped_uplink_runs_the_root_class_below_the_declared_rate() -> None:
    assert GEN2_UPLINK_SHAPING_PERCENT == 95
