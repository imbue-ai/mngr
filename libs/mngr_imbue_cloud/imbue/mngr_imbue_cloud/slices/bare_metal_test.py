from datetime import datetime
from datetime import timezone

import pytest

from imbue.mngr.primitives import HostId
from imbue.mngr_imbue_cloud.data_types import BareMetalServer
from imbue.mngr_imbue_cloud.errors import BareMetalConfigError
from imbue.mngr_imbue_cloud.errors import SliceCapacityError
from imbue.mngr_imbue_cloud.primitives import BareMetalServerDbId
from imbue.mngr_imbue_cloud.primitives import BareMetalServerStatus
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_DELIVERED
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_FAILED
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_INSTALLING
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_ORDERED
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_READY
from imbue.mngr_imbue_cloud.slices.bare_metal import DISK_RESERVE_GB
from imbue.mngr_imbue_cloud.slices.bare_metal import SLICE_BOOT_DISK_GIB
from imbue.mngr_imbue_cloud.slices.bare_metal import choose_raid_level
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_capacity
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_orphan_slice_disk_names
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_orphan_slice_instance_names
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_slice_disk_budget_gib
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_slice_disk_gib
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_slice_memory_mib
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_slice_vcpus
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_slot_count
from imbue.mngr_imbue_cloud.slices.bare_metal import count_slice_resource_names
from imbue.mngr_imbue_cloud.slices.bare_metal import find_server_capacity_by_id
from imbue.mngr_imbue_cloud.slices.bare_metal import is_slice_owned_by_env
from imbue.mngr_imbue_cloud.slices.bare_metal import is_valid_status_transition
from imbue.mngr_imbue_cloud.slices.bare_metal import next_server_status
from imbue.mngr_imbue_cloud.slices.bare_metal import slice_lima_disk_name
from imbue.mngr_imbue_cloud.slices.bare_metal import slice_lima_instance_name
from imbue.mngr_imbue_cloud.slices.bare_metal import slice_name_env_owner


def _server(
    status: str,
    slot_count: int = 8,
    server_id: str = "11111111-1111-1111-1111-111111111111",
) -> BareMetalServer:
    now = datetime.now(timezone.utc)
    return BareMetalServer(
        id=BareMetalServerDbId(server_id),
        plan_code="24rise02-v1-us",
        region="vin",
        slot_count=slot_count,
        status=BareMetalServerStatus(status),
        created_at=now,
        updated_at=now,
    )


def test_compute_slot_count_reserves_host_and_per_vm_overhead() -> None:
    # slots = floor((ram - 8 host reserve) GiB / (slice + 0.5 per-VM overhead) GiB).
    # e.g. 256GB box, 8GB slices: (256-8)*1024 // (8*1024 + 512) = 253952 // 8704 = 29.
    assert compute_slot_count(256, 8) == 29
    assert compute_slot_count(64, 8) == 6
    assert compute_slot_count(128, 8) == 14
    # Too small to fit even one slice after the host reserve -> 0.
    assert compute_slot_count(4, 8) == 0
    # A larger per-slice RAM yields fewer slots.
    assert compute_slot_count(64, 16) == 3


def test_compute_slot_count_rejects_negative_ram_and_nonpositive_per_slice() -> None:
    with pytest.raises(BareMetalConfigError):
        compute_slot_count(-1, 8)
    with pytest.raises(BareMetalConfigError):
        compute_slot_count(64, 0)


def test_compute_slice_memory_mib_is_full_advertised() -> None:
    # The guest gets the full advertised RAM; per-VM overhead is accounted in slot_count.
    assert compute_slice_memory_mib(8) == 8 * 1024
    assert compute_slice_memory_mib(16) == 16 * 1024


def test_compute_slice_memory_mib_rejects_too_small() -> None:
    with pytest.raises(BareMetalConfigError):
        compute_slice_memory_mib(0)


def test_compute_slice_disk_budget_splits_usable_disk() -> None:
    # reserve = max(20, ceil(500 * 0.10)) = 50; (500 - 50) // 8 = 56 GiB budget each.
    assert compute_slice_disk_budget_gib(500, 8) == 56
    # Small disk: the fixed 20GiB floor wins over the fraction.
    assert compute_slice_disk_budget_gib(150, 8) == (150 - 20) // 8


def test_compute_slice_disk_budget_does_not_overcommit_nominal_disk() -> None:
    # A disk_gb registered from the nominal spec (e.g. "8 TB" -> 8000) must leave the
    # per-slice allocations within the real usable GiB (~0.93 * 8000 = 7440) thanks to
    # the fraction reserve, so slots * budget never exceeds usable.
    disk_gb = 8000
    slot_count = 29
    budget = compute_slice_disk_budget_gib(disk_gb, slot_count)
    usable_gib = int(disk_gb * 0.93)
    assert slot_count * budget <= usable_gib


def test_compute_slice_disk_gib_is_budget_minus_boot_disk() -> None:
    # Data disk = total budget minus the fixed boot disk, so boot + data == budget.
    assert compute_slice_disk_gib(500, 8) == compute_slice_disk_budget_gib(500, 8) - SLICE_BOOT_DISK_GIB
    assert compute_slice_disk_gib(500, 8) + SLICE_BOOT_DISK_GIB == compute_slice_disk_budget_gib(500, 8)


def test_compute_slice_disk_gib_rejects_when_budget_too_small_for_boot() -> None:
    # Budget below the boot-disk size leaves no room for a data disk.
    with pytest.raises(BareMetalConfigError):
        compute_slice_disk_gib(20, 8)
    with pytest.raises(BareMetalConfigError):
        compute_slice_disk_gib(500, 0)
    # Budget that fits but is smaller than the boot disk also fails.
    with pytest.raises(BareMetalConfigError):
        compute_slice_disk_gib(disk_gb=DISK_RESERVE_GB + SLICE_BOOT_DISK_GIB, slot_count=1)


def test_compute_slice_vcpus_applies_mild_overcommit() -> None:
    # RISE-2: 16 threads over 8 slots at 1.5x -> 3 vCPU/slice.
    assert compute_slice_vcpus(cpu_threads=16, slot_count=8, overcommit_ratio=1.5) == 3
    # No overcommit: 16 threads / 8 slots -> 2.
    assert compute_slice_vcpus(cpu_threads=16, slot_count=8, overcommit_ratio=1.0) == 2
    # Always at least one vCPU even when heavily oversubscribed.
    assert compute_slice_vcpus(cpu_threads=4, slot_count=16, overcommit_ratio=1.0) == 1


def test_compute_slice_vcpus_rejects_bad_inputs() -> None:
    with pytest.raises(BareMetalConfigError):
        compute_slice_vcpus(cpu_threads=0, slot_count=8, overcommit_ratio=1.5)
    with pytest.raises(BareMetalConfigError):
        compute_slice_vcpus(cpu_threads=16, slot_count=0, overcommit_ratio=1.5)
    with pytest.raises(BareMetalConfigError):
        compute_slice_vcpus(cpu_threads=16, slot_count=8, overcommit_ratio=0.0)


def test_choose_raid_level_prefers_mirroring() -> None:
    assert choose_raid_level(2) == "RAID1"
    assert choose_raid_level(4) == "RAID10"
    assert choose_raid_level(6) == "RAID10"


def test_choose_raid_level_rejects_unmirrorable_disk_counts() -> None:
    with pytest.raises(BareMetalConfigError):
        choose_raid_level(1)
    with pytest.raises(BareMetalConfigError):
        choose_raid_level(3)


def test_slice_lima_names_are_deterministic_and_distinct() -> None:
    host_id = HostId.generate()
    other_id = HostId.generate()
    assert slice_lima_instance_name(host_id) == slice_lima_instance_name(host_id)
    assert slice_lima_instance_name(host_id) != slice_lima_instance_name(other_id)
    assert slice_lima_disk_name(host_id) != slice_lima_instance_name(host_id)
    assert host_id.get_uuid().hex in slice_lima_instance_name(host_id)


def test_slice_lima_names_stamp_the_env_and_keep_the_host_hex() -> None:
    host_id = HostId.generate()
    stamped = slice_lima_instance_name(host_id, "dev-josh-foo")
    legacy = slice_lima_instance_name(host_id)
    assert stamped == f"mngr-slice-dev-josh-foo-{host_id.get_uuid().hex}"
    assert legacy == f"mngr-slice-{host_id.get_uuid().hex}"
    # The disk name is the instance name plus the data suffix, for both forms.
    assert slice_lima_disk_name(host_id, "dev-josh-foo") == f"{stamped}-data"
    assert slice_lima_disk_name(host_id) == f"{legacy}-data"


def test_slice_name_env_owner_distinguishes_stamped_legacy_and_non_slice() -> None:
    host_id = HostId.generate()
    assert slice_name_env_owner(slice_lima_instance_name(host_id, "dev-josh-foo")) == "dev-josh-foo"
    # The env owner is recoverable from the disk name too (the data suffix is stripped).
    assert slice_name_env_owner(slice_lima_disk_name(host_id, "dev-josh-foo")) == "dev-josh-foo"
    # Legacy (un-stamped) and non-slice names have no env owner.
    assert slice_name_env_owner(slice_lima_instance_name(host_id)) is None
    assert slice_name_env_owner("default") is None
    assert slice_name_env_owner("some-other-vm") is None


def test_is_slice_owned_by_env_only_matches_exact_env_stamp() -> None:
    host_id = HostId.generate()
    mine = slice_lima_instance_name(host_id, "dev-josh-foo")
    theirs = slice_lima_instance_name(host_id, "dev-alice-bar")
    legacy = slice_lima_instance_name(host_id)
    assert is_slice_owned_by_env(mine, "dev-josh-foo") is True
    assert is_slice_owned_by_env(theirs, "dev-josh-foo") is False
    assert is_slice_owned_by_env(legacy, "dev-josh-foo") is False


def test_count_slice_resource_names_counts_all_slices_regardless_of_stamp() -> None:
    host_a = HostId.generate()
    host_b = HostId.generate()
    # A mix of this env's slice, another env's slice, a legacy un-stamped slice, and
    # two non-slice disks.
    names = {
        slice_lima_disk_name(host_a, "dev-josh-foo"),
        slice_lima_disk_name(host_b, "dev-alice-bar"),
        slice_lima_disk_name(HostId.generate()),
        "default",
        "some-other-disk",
    }
    # True box occupancy is every slice (every env + legacy), excluding non-slice disks.
    assert count_slice_resource_names(names) == 3


def test_next_server_status_walks_the_forward_chain() -> None:
    assert next_server_status(BareMetalServerStatus(SERVER_STATUS_ORDERED)) == BareMetalServerStatus(
        SERVER_STATUS_DELIVERED
    )
    assert next_server_status(BareMetalServerStatus(SERVER_STATUS_DELIVERED)) == BareMetalServerStatus(
        SERVER_STATUS_INSTALLING
    )
    assert next_server_status(BareMetalServerStatus(SERVER_STATUS_INSTALLING)) == BareMetalServerStatus(
        SERVER_STATUS_READY
    )
    assert next_server_status(BareMetalServerStatus(SERVER_STATUS_READY)) is None
    assert next_server_status(BareMetalServerStatus(SERVER_STATUS_FAILED)) is None


def test_is_valid_status_transition_allows_forward_and_failure_only() -> None:
    ordered = BareMetalServerStatus(SERVER_STATUS_ORDERED)
    delivered = BareMetalServerStatus(SERVER_STATUS_DELIVERED)
    installing = BareMetalServerStatus(SERVER_STATUS_INSTALLING)
    ready = BareMetalServerStatus(SERVER_STATUS_READY)
    failed = BareMetalServerStatus(SERVER_STATUS_FAILED)
    assert is_valid_status_transition(ordered, delivered) is True
    assert is_valid_status_transition(ordered, failed) is True
    # Cannot skip a step.
    assert is_valid_status_transition(ordered, installing) is False
    # Terminal states admit nothing further.
    assert is_valid_status_transition(ready, failed) is False
    assert is_valid_status_transition(failed, ordered) is False


def test_compute_capacity_reports_free_slots() -> None:
    capacity = compute_capacity(_server(SERVER_STATUS_READY, slot_count=8), used_slots=3)
    assert capacity.free_slots == 5
    assert capacity.used_slots == 3


def test_compute_capacity_clamps_overfull_to_zero() -> None:
    capacity = compute_capacity(_server(SERVER_STATUS_READY, slot_count=8), used_slots=10)
    assert capacity.free_slots == 0


def test_compute_capacity_rejects_negative_used() -> None:
    with pytest.raises(BareMetalConfigError):
        compute_capacity(_server(SERVER_STATUS_READY), used_slots=-1)


def test_find_server_capacity_by_id_returns_the_matching_server() -> None:
    target_id = BareMetalServerDbId("22222222-2222-2222-2222-222222222222")
    other = compute_capacity(_server(SERVER_STATUS_READY, slot_count=8), used_slots=1)
    target = compute_capacity(_server(SERVER_STATUS_READY, slot_count=16, server_id=str(target_id)), used_slots=2)
    chosen = find_server_capacity_by_id([other, target], target_id)
    assert chosen.server.id == target_id
    assert chosen.free_slots == 14


def test_find_server_capacity_by_id_raises_when_absent() -> None:
    only = compute_capacity(_server(SERVER_STATUS_READY, slot_count=8), used_slots=0)
    with pytest.raises(SliceCapacityError):
        find_server_capacity_by_id([only], BareMetalServerDbId("99999999-9999-9999-9999-999999999999"))


def test_compute_orphan_slice_instance_names_returns_this_envs_untracked_slice_vms() -> None:
    # This env's on-box VMs not present in the DB are orphans.
    aaa = slice_lima_instance_name(HostId.generate(), "dev-josh")
    bbb = slice_lima_instance_name(HostId.generate(), "dev-josh")
    ccc = slice_lima_instance_name(HostId.generate(), "dev-josh")
    box = {aaa, bbb, ccc}
    tracked = {aaa}
    assert compute_orphan_slice_instance_names(box, tracked, "dev-josh") == {bbb, ccc}


def test_compute_orphan_slice_instance_names_never_touches_other_envs_or_legacy() -> None:
    # Another env's slice, a legacy un-stamped slice, and a non-slice VM must never
    # be considered orphans of this env -- this is what makes box sharing safe.
    mine = slice_lima_instance_name(HostId.generate(), "dev-josh")
    theirs = slice_lima_instance_name(HostId.generate(), "dev-alice")
    legacy = slice_lima_instance_name(HostId.generate())
    box = {mine, theirs, legacy, "some-other-vm", "default"}
    tracked: set[str] = set()
    assert compute_orphan_slice_instance_names(box, tracked, "dev-josh") == {mine}


def test_compute_orphan_slice_instance_names_empty_when_all_tracked() -> None:
    aaa = slice_lima_instance_name(HostId.generate(), "dev-josh")
    bbb = slice_lima_instance_name(HostId.generate(), "dev-josh")
    box = {aaa, bbb}
    tracked = {aaa, bbb, slice_lima_instance_name(HostId.generate(), "dev-josh")}
    assert compute_orphan_slice_instance_names(box, tracked, "dev-josh") == set()


def test_compute_orphan_slice_disk_names_returns_this_envs_untracked_slice_disks() -> None:
    aaa = slice_lima_disk_name(HostId.generate(), "dev-josh")
    bbb = slice_lima_disk_name(HostId.generate(), "dev-josh")
    box = {aaa, bbb}
    tracked = {aaa}
    assert compute_orphan_slice_disk_names(box, tracked, "dev-josh") == {bbb}


def test_compute_orphan_slice_disk_names_never_touches_other_envs_or_legacy() -> None:
    mine = slice_lima_disk_name(HostId.generate(), "dev-josh")
    theirs = slice_lima_disk_name(HostId.generate(), "dev-alice")
    legacy = slice_lima_disk_name(HostId.generate())
    box = {mine, theirs, legacy, "some-other-disk"}
    tracked: set[str] = set()
    assert compute_orphan_slice_disk_names(box, tracked, "dev-josh") == {mine}
