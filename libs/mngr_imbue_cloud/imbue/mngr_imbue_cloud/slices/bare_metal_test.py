import os
import subprocess
from datetime import datetime
from datetime import timezone
from pathlib import Path
from uuid import uuid4

import pytest

from imbue.mngr.primitives import HostId
from imbue.mngr_imbue_cloud.data_types import BareMetalServer
from imbue.mngr_imbue_cloud.data_types import BoxManagementTrust
from imbue.mngr_imbue_cloud.data_types import StorageVolumeState
from imbue.mngr_imbue_cloud.errors import BareMetalConfigError
from imbue.mngr_imbue_cloud.errors import SliceCapacityError
from imbue.mngr_imbue_cloud.primitives import BareMetalServerDbId
from imbue.mngr_imbue_cloud.primitives import BareMetalServerStatus
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_DELIVERED
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_FAILED
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_INSTALLING
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_ORDERED
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_READY
from imbue.mngr_imbue_cloud.slices.bare_metal import CI_SLICE_MAX_AGE_SECONDS
from imbue.mngr_imbue_cloud.slices.bare_metal import DEFAULT_SLICE_CPU_OVERCOMMIT_RATIO
from imbue.mngr_imbue_cloud.slices.bare_metal import GEN1_SLICE_SERVICE_USER
from imbue.mngr_imbue_cloud.slices.bare_metal import MAX_SLICE_ENV_NAME_LENGTH
from imbue.mngr_imbue_cloud.slices.bare_metal import MAX_SLICE_INSTANCE_NAME_LENGTH
from imbue.mngr_imbue_cloud.slices.bare_metal import ORPHAN_SLICE_MIN_AGE_SECONDS
from imbue.mngr_imbue_cloud.slices.bare_metal import SLICE_HOST_ID_HEX_LENGTH
from imbue.mngr_imbue_cloud.slices.bare_metal import assert_env_name_fits_slice_names
from imbue.mngr_imbue_cloud.slices.bare_metal import assert_region_label_matches_box_datacenter
from imbue.mngr_imbue_cloud.slices.bare_metal import box_service_user
from imbue.mngr_imbue_cloud.slices.bare_metal import build_read_management_trust_command
from imbue.mngr_imbue_cloud.slices.bare_metal import build_read_storage_volume_command
from imbue.mngr_imbue_cloud.slices.bare_metal import build_slice_container_memory_start_args
from imbue.mngr_imbue_cloud.slices.bare_metal import choose_raid_level
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_capacity
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_gen2_box_default_machine_fit
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_orphan_slice_disk_names
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_orphan_slice_instance_names
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_slice_container_memory_cap_mib
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_slice_disk_budget_gib
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_slice_disk_gib
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_slice_memory_mib
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_slice_vcpus
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_slot_count
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_tier_orphan_disk_names
from imbue.mngr_imbue_cloud.slices.bare_metal import count_authorized_key_lines
from imbue.mngr_imbue_cloud.slices.bare_metal import count_slice_resource_names
from imbue.mngr_imbue_cloud.slices.bare_metal import default_slice_service_user
from imbue.mngr_imbue_cloud.slices.bare_metal import describe_gen2_box_disk_shortfall
from imbue.mngr_imbue_cloud.slices.bare_metal import expected_static_authorized_key_count
from imbue.mngr_imbue_cloud.slices.bare_metal import find_first_ready_server_in_datacenter
from imbue.mngr_imbue_cloud.slices.bare_metal import find_server_capacity_by_id
from imbue.mngr_imbue_cloud.slices.bare_metal import foreign_tier_slice_names
from imbue.mngr_imbue_cloud.slices.bare_metal import is_slice_owned_by_env
from imbue.mngr_imbue_cloud.slices.bare_metal import is_trusted_ca_correct_for_tier
from imbue.mngr_imbue_cloud.slices.bare_metal import is_valid_status_transition
from imbue.mngr_imbue_cloud.slices.bare_metal import next_server_status
from imbue.mngr_imbue_cloud.slices.bare_metal import parse_degraded_md_arrays
from imbue.mngr_imbue_cloud.slices.bare_metal import parse_management_trust_output
from imbue.mngr_imbue_cloud.slices.bare_metal import parse_raw_swap_devices
from imbue.mngr_imbue_cloud.slices.bare_metal import parse_storage_volume_output
from imbue.mngr_imbue_cloud.slices.bare_metal import partition_slice_names_by_tier_and_age
from imbue.mngr_imbue_cloud.slices.bare_metal import slice_disk_name
from imbue.mngr_imbue_cloud.slices.bare_metal import slice_instance_name
from imbue.mngr_imbue_cloud.slices.bare_metal import slice_name_env_owner
from imbue.mngr_imbue_cloud.slices.gen2_scripts.box_commands import SliceInstanceObservation
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_SLICE_SERVICE_USER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import DEFAULT_MACHINE_UNITS
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import DISK_RESERVE_GB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import SLICE_BOOT_DISK_GIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import SLICE_CONTAINER_MEMORY_RESERVE_MIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_box_total_units
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_default_machine_capacity
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_machine_vcpus


def _server(
    status: str = SERVER_STATUS_READY,
    slot_count: int = 8,
    server_id: str = "11111111-1111-1111-1111-111111111111",
    region: str = "vin",
    slice_service_user: str | None = None,
    box_generation: int = 1,
) -> BareMetalServer:
    now = datetime.now(timezone.utc)
    return BareMetalServer(
        id=BareMetalServerDbId(server_id),
        plan_code="24rise02-v1-us",
        region=region,
        slot_count=slot_count,
        slice_service_user=slice_service_user,
        status=BareMetalServerStatus(status),
        created_at=now,
        updated_at=now,
        box_generation=box_generation,
        uplink_mbps=1000,
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


def test_slice_names_are_deterministic_and_distinct() -> None:
    host_id = HostId.generate()
    other_id = HostId.generate()
    assert slice_instance_name(host_id) == slice_instance_name(host_id)
    assert slice_instance_name(host_id) != slice_instance_name(other_id)
    assert slice_disk_name(host_id) != slice_instance_name(host_id)
    assert host_id.get_uuid().hex[:SLICE_HOST_ID_HEX_LENGTH] in slice_instance_name(host_id)


def test_slice_names_stamp_the_env_and_keep_the_truncated_host_hex() -> None:
    host_id = HostId.generate()
    host_hex = host_id.get_uuid().hex[:SLICE_HOST_ID_HEX_LENGTH]
    stamped = slice_instance_name(host_id, "dev-josh-foo")
    legacy = slice_instance_name(host_id)
    assert stamped == f"mngr-slice-dev-josh-foo-{host_hex}"
    assert legacy == f"mngr-slice-{host_hex}"
    # The disk name is the instance name plus the data suffix, for both forms.
    assert slice_disk_name(host_id, "dev-josh-foo") == f"{stamped}-data"
    assert slice_disk_name(host_id) == f"{legacy}-data"


def test_slice_name_env_owner_parses_pre_truncation_full_hex_names() -> None:
    # Slices baked before the hex truncation carry the full 32-char host hex;
    # every parser must keep attributing them correctly.
    full_hex = HostId.generate().get_uuid().hex
    assert slice_name_env_owner(f"mngr-slice-dev-josh-foo-{full_hex}") == "dev-josh-foo"
    assert slice_name_env_owner(f"mngr-slice-dev-josh-foo-{full_hex}-data") == "dev-josh-foo"
    assert slice_name_env_owner(f"mngr-slice-{full_hex}") is None


def test_slice_name_env_owner_distinguishes_stamped_legacy_and_non_slice() -> None:
    host_id = HostId.generate()
    assert slice_name_env_owner(slice_instance_name(host_id, "dev-josh-foo")) == "dev-josh-foo"
    # The env owner is recoverable from the disk name too (the data suffix is stripped).
    assert slice_name_env_owner(slice_disk_name(host_id, "dev-josh-foo")) == "dev-josh-foo"
    # Legacy (un-stamped) and non-slice names have no env owner.
    assert slice_name_env_owner(slice_instance_name(host_id)) is None
    assert slice_name_env_owner("default") is None
    assert slice_name_env_owner("some-other-vm") is None


def test_is_slice_owned_by_env_only_matches_exact_env_stamp() -> None:
    host_id = HostId.generate()
    mine = slice_instance_name(host_id, "dev-josh-foo")
    theirs = slice_instance_name(host_id, "dev-alice-bar")
    legacy = slice_instance_name(host_id)
    assert is_slice_owned_by_env(mine, "dev-josh-foo") is True
    assert is_slice_owned_by_env(theirs, "dev-josh-foo") is False
    assert is_slice_owned_by_env(legacy, "dev-josh-foo") is False


def test_count_slice_resource_names_counts_all_slices_regardless_of_stamp() -> None:
    host_a = HostId.generate()
    host_b = HostId.generate()
    # A mix of this env's slice, another env's slice, a legacy un-stamped slice, and
    # two non-slice disks.
    names = {
        slice_disk_name(host_a, "dev-josh-foo"),
        slice_disk_name(host_b, "dev-alice-bar"),
        slice_disk_name(HostId.generate()),
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


def test_find_first_ready_server_in_datacenter_returns_the_first_ready_row_in_that_datacenter() -> None:
    not_ready = _server(SERVER_STATUS_ORDERED, server_id="11111111-1111-1111-1111-111111111111")
    first_ready = _server(SERVER_STATUS_READY, server_id="22222222-2222-2222-2222-222222222222")
    later_ready = _server(SERVER_STATUS_READY, server_id="33333333-3333-3333-3333-333333333333")
    chosen = find_first_ready_server_in_datacenter([not_ready, first_ready, later_ready], "vin")
    assert chosen is not None
    assert chosen.id == first_ready.id


def test_find_first_ready_server_in_datacenter_ignores_other_datacenters() -> None:
    hil_ready = _server(SERVER_STATUS_READY, server_id="22222222-2222-2222-2222-222222222222", region="hil")
    vin_ready = _server(SERVER_STATUS_READY, server_id="33333333-3333-3333-3333-333333333333", region="vin")
    chosen = find_first_ready_server_in_datacenter([hil_ready, vin_ready], "hil")
    assert chosen is not None
    assert chosen.id == hil_ready.id


def test_find_first_ready_server_in_datacenter_returns_none_when_no_ready_row_matches() -> None:
    assert find_first_ready_server_in_datacenter([], "vin") is None
    assert find_first_ready_server_in_datacenter([_server(SERVER_STATUS_INSTALLING)], "vin") is None
    assert find_first_ready_server_in_datacenter([_server(SERVER_STATUS_READY, region="hil")], "vin") is None


def _observed(name: str, *, is_active: bool = False, age_seconds: float = 3 * 3600) -> SliceInstanceObservation:
    return SliceInstanceObservation(instance_name=name, is_active=is_active, age_seconds=age_seconds)


def test_compute_orphan_slice_instance_names_returns_this_envs_old_stopped_untracked_vms() -> None:
    # This env's on-box VMs with no DB row, not running, and older than the bake window are orphans.
    aaa = slice_instance_name(HostId.generate(), "dev-josh")
    bbb = slice_instance_name(HostId.generate(), "dev-josh")
    ccc = slice_instance_name(HostId.generate(), "dev-josh")
    observations = (_observed(aaa), _observed(bbb), _observed(ccc))
    assert compute_orphan_slice_instance_names(observations, {aaa}, "dev-josh") == {bbb, ccc}


def test_compute_orphan_slice_instance_names_never_touches_other_envs_or_legacy() -> None:
    # Another env's slice, a legacy un-stamped slice, and a non-slice VM must never
    # be considered orphans of this env -- this is what makes box sharing safe.
    mine = slice_instance_name(HostId.generate(), "dev-josh")
    theirs = slice_instance_name(HostId.generate(), "dev-alice")
    legacy = slice_instance_name(HostId.generate())
    observations = tuple(_observed(name) for name in (mine, theirs, legacy, "some-other-vm", "default"))
    assert compute_orphan_slice_instance_names(observations, set(), "dev-josh") == {mine}


def test_compute_orphan_slice_instance_names_empty_when_all_tracked() -> None:
    aaa = slice_instance_name(HostId.generate(), "dev-josh")
    bbb = slice_instance_name(HostId.generate(), "dev-josh")
    tracked = {aaa, bbb, slice_instance_name(HostId.generate(), "dev-josh")}
    assert compute_orphan_slice_instance_names((_observed(aaa), _observed(bbb)), tracked, "dev-josh") == set()


def test_compute_orphan_slice_instance_names_spares_running_and_young_rowless_vms() -> None:
    # A rowless VM that is running, or younger than a bake, is someone's in-flight
    # carve (the row lands after the carve); only an old, stopped one is reaped.
    running = slice_instance_name(HostId.generate(), "dev-josh")
    young = slice_instance_name(HostId.generate(), "dev-josh")
    old_stopped = slice_instance_name(HostId.generate(), "dev-josh")
    observations = (
        _observed(running, is_active=True, age_seconds=10 * 3600),
        _observed(young, age_seconds=ORPHAN_SLICE_MIN_AGE_SECONDS - 1),
        _observed(old_stopped, age_seconds=ORPHAN_SLICE_MIN_AGE_SECONDS),
    )
    assert compute_orphan_slice_instance_names(observations, set(), "dev-josh") == {old_stopped}


def test_compute_orphan_slice_disk_names_returns_this_envs_untracked_slice_disks() -> None:
    aaa = slice_disk_name(HostId.generate(), "dev-josh")
    bbb = slice_disk_name(HostId.generate(), "dev-josh")
    assert compute_orphan_slice_disk_names({aaa, bbb}, {aaa}, "dev-josh", set()) == {bbb}


def test_compute_orphan_slice_disk_names_never_touches_other_envs_or_legacy() -> None:
    mine = slice_disk_name(HostId.generate(), "dev-josh")
    theirs = slice_disk_name(HostId.generate(), "dev-alice")
    legacy = slice_disk_name(HostId.generate())
    box = {mine, theirs, legacy, "some-other-disk"}
    assert compute_orphan_slice_disk_names(box, set(), "dev-josh", set()) == {mine}


def test_compute_orphan_slice_disk_names_spares_the_disks_of_the_instances_the_reap_keeps() -> None:
    # The disk of a running rowless VM (an in-flight bake) is held open by qemu:
    # unlinking it would silently destroy the VM's data at its next restart. The
    # disk of a stopped-but-young rowless VM the instance reap spared must survive
    # too, or sparing the VM is pointless. Only a disk whose VM is gone is reaped.
    running_id = HostId.generate()
    spared_id = HostId.generate()
    held = {slice_instance_name(running_id, "dev-josh"), slice_instance_name(spared_id, "dev-josh")}
    running_disk = slice_disk_name(running_id, "dev-josh")
    spared_disk = slice_disk_name(spared_id, "dev-josh")
    stale_disk = slice_disk_name(HostId.generate(), "dev-josh")
    orphans = compute_orphan_slice_disk_names({running_disk, spared_disk, stale_disk}, set(), "dev-josh", held)
    assert orphans == {stale_disk}


def test_compute_slice_container_memory_cap_mib_subtracts_the_vm_reserve() -> None:
    # An 8GiB slice yields a 7GiB container cap: the fixed VM-side reserve comes
    # off the top so the VM's own daemons (sshd, dockerd, guestagent) keep room.
    assert compute_slice_container_memory_cap_mib(8192) == 8192 - SLICE_CONTAINER_MEMORY_RESERVE_MIB


def test_compute_slice_container_memory_cap_mib_rejects_slices_smaller_than_the_reserve() -> None:
    with pytest.raises(BareMetalConfigError):
        compute_slice_container_memory_cap_mib(SLICE_CONTAINER_MEMORY_RESERVE_MIB)


def test_build_slice_container_memory_start_args_pins_swap_to_the_memory_cap() -> None:
    # --memory-swap equals --memory so the container can never swap (it must be
    # shed under pressure, not thrash).
    assert build_slice_container_memory_start_args(8192) == ("--memory=7168m", "--memory-swap=7168m")


def test_foreign_tier_slice_names_flags_a_dev_slice_on_a_staging_box() -> None:
    # The exact shape of the incident this guard exists for: a dev env's slice
    # carved onto a box the staging tier owns.
    theirs = slice_disk_name(HostId.generate(), "dev-xiaq")
    mine = slice_disk_name(HostId.generate(), "staging")
    assert foreign_tier_slice_names({theirs, mine}, "staging") == {theirs}


def test_foreign_tier_slice_names_allows_box_sharing_within_the_dev_tier() -> None:
    # Several dev envs on one dev box is documented, routine, and must stay allowed.
    josh = slice_disk_name(HostId.generate(), "dev-josh")
    alice = slice_disk_name(HostId.generate(), "dev-alice")
    assert foreign_tier_slice_names({josh, alice}, "dev-josh") == set()


def test_foreign_tier_slice_names_flags_staging_slices_from_a_dev_env() -> None:
    # Symmetric: the guard fires from whichever side bakes second.
    staging_slice = slice_disk_name(HostId.generate(), "staging")
    assert foreign_tier_slice_names({staging_slice}, "dev-josh") == {staging_slice}


def test_foreign_tier_slice_names_separates_production_from_staging() -> None:
    production_slice = slice_disk_name(HostId.generate(), "production")
    assert foreign_tier_slice_names({production_slice}, "staging") == {production_slice}
    assert foreign_tier_slice_names({production_slice}, "production") == set()


def test_foreign_tier_slice_names_ignores_legacy_unstamped_and_non_slice_names() -> None:
    # A legacy name carries no env, so its tier is unknowable -- it counts toward
    # occupancy but must never be reported as a cross-tier violation. Non-slice
    # lima resources are not ours to judge at all.
    legacy = slice_disk_name(HostId.generate())
    assert foreign_tier_slice_names({legacy, "some-unrelated-disk"}, "staging") == set()


def test_foreign_tier_slice_names_is_empty_for_an_empty_box() -> None:
    assert foreign_tier_slice_names(set(), "staging") == set()


def test_count_authorized_key_lines_counts_only_key_bearing_lines() -> None:
    # A correctly prepped box holds exactly this: one pool key, nothing else.
    assert count_authorized_key_lines("ssh-ed25519 AAAApool pool-management\n") == 1
    # Blank lines, whitespace-only lines, and comments carry no key.
    assert count_authorized_key_lines("") == 0
    assert count_authorized_key_lines("\n\n   \n") == 0
    assert count_authorized_key_lines("# only a comment\nssh-ed25519 AAAApool\n") == 1
    assert count_authorized_key_lines("\t# indented comment\n  ssh-ed25519 AAAApool\n") == 1
    # The condition the tier guard refuses on: a second key added out of band.
    assert count_authorized_key_lines("ssh-ed25519 AAAApool\nssh-ed25519 AAAAother dev-tier\n") == 2
    # A file with no trailing newline must not lose its last key.
    assert count_authorized_key_lines("ssh-ed25519 AAAApool\nssh-ed25519 AAAAother") == 2


# Verbatim from the 2026-08-07 production incident box (51.81.185.229): nvme0
# dropped off the bus, both RAID1 arrays run degraded on nvme1, and the dead
# disk's raw swap partition lingers as a "(deleted)" entry.
_DEGRADED_MDSTAT = """\
Personalities : [raid1] [linear] [multipath] [raid0] [raid6] [raid5] [raid4] [raid10]
md2 : active raid1 nvme1n1p2[1]
      1046528 blocks super 1.2 [2/1] [_U]
      bitmap: 1/1 pages [4KB], 65536KB chunk

md3 : active raid1 nvme1n1p3[1]
      936244224 blocks super 1.2 [2/1] [_U]
      bitmap: 7/7 pages [28KB], 65536KB chunk

unused devices: <none>
"""

_HEALTHY_MDSTAT = """\
Personalities : [raid1]
md3 : active raid1 nvme0n1p3[0] nvme1n1p3[1]
      935460864 blocks super 1.2 [2/2] [UU]
md2 : active raid1 nvme0n1p2[0] nvme1n1p2[1]
      1046528 blocks super 1.2 [2/2] [UU]
unused devices: <none>
"""

_INCIDENT_PROC_SWAPS = """\
Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority
/dev/nvme1n1p4                          partition\t524284\t\t220932\t\t-2
/dev/nvme0n1p4\\040(deleted)             partition\t524284\t\t346108\t\t-3
/swapfile                               file\t\t33554428\t1426020\t\t-4
"""


def test_parse_degraded_md_arrays_reports_arrays_missing_a_member() -> None:
    assert parse_degraded_md_arrays(_DEGRADED_MDSTAT) == ["md2", "md3"]


def test_parse_degraded_md_arrays_reports_nothing_for_healthy_arrays() -> None:
    assert parse_degraded_md_arrays(_HEALTHY_MDSTAT) == []
    assert parse_degraded_md_arrays("") == []


def test_parse_raw_swap_devices_flags_partitions_but_not_the_swapfile() -> None:
    # Both raw partitions are flagged -- including the dead disk's lingering
    # "(deleted)" entry -- while the mirrored swapfile is not.
    assert parse_raw_swap_devices(_INCIDENT_PROC_SWAPS) == [
        "/dev/nvme1n1p4",
        "/dev/nvme0n1p4\\040(deleted)",
    ]


def test_parse_raw_swap_devices_ignores_md_backed_swap_and_empty_input() -> None:
    md_swap = "Filename\tType\tSize\tUsed\tPriority\n/dev/md1 partition 524284 0 -2\n"
    assert parse_raw_swap_devices(md_swap) == []
    assert parse_raw_swap_devices("") == []


def test_env_names_at_the_slice_name_cap_pass_and_longer_ones_fail() -> None:
    # The binding constraint is limactl's instance-name budget (its ssh socket
    # path must fit UNIX_PATH_MAX); an env at the cap must land exactly on it.
    host_id = HostId.generate()
    at_cap = "c" * MAX_SLICE_ENV_NAME_LENGTH
    assert_env_name_fits_slice_names(at_cap)
    assert len(slice_instance_name(host_id, at_cap)) == MAX_SLICE_INSTANCE_NAME_LENGTH
    # The secondary 76-char identifier cap must also hold for the disk name.
    assert len(slice_disk_name(host_id, at_cap)) <= 76

    with pytest.raises(SliceCapacityError, match="instance name"):
        assert_env_name_fits_slice_names("c" * (MAX_SLICE_ENV_NAME_LENGTH + 1))


def test_orchestrator_style_ci_env_names_fit_the_slice_name_cap() -> None:
    # The CI orchestrator mints ``ci-<16-char-timestamp>-<8-hex>`` names (28
    # chars, see _mint_shared_env_name); both real incidents -- the 77-char disk
    # identifier and the UNIX_PATH_MAX ssh.sock overflow -- must stay fixed for
    # that shape.
    assert_env_name_fits_slice_names("ci-20260820t171706z-b0c869d1")


def test_gen1_slot_count_matches_the_gen2_default_machine_capacity() -> None:
    # The uniform model is the special case: on a 128GB box the gen-1 slot math
    # lands on exactly the 14 default-size machines the gen-2 unit budget holds.
    assert compute_slot_count(128, 8) == compute_default_machine_capacity(128) == 14


def test_default_overcommit_gives_a_default_machine_four_vcpus_on_the_standard_box() -> None:
    # The gen-2 sizing decision: room for gVisor's systrap sentry threads next to the application.
    total_units = compute_box_total_units(128)
    assert compute_machine_vcpus(16, DEFAULT_SLICE_CPU_OVERCOMMIT_RATIO, DEFAULT_MACHINE_UNITS, total_units) == 4


def _ci_slice_name(env: str) -> str:
    return f"mngr-slice-{env}-{uuid4().hex}"


def test_ci_sweep_threshold_outlasts_the_orphan_reapers_guard() -> None:
    # The reaper's guard protects an in-flight bake; the CI sweep's protects an
    # in-flight release run, which is the longer of the two.
    assert CI_SLICE_MAX_AGE_SECONDS >= ORPHAN_SLICE_MIN_AGE_SECONDS
    assert CI_SLICE_MAX_AGE_SECONDS == 4 * 3600


def test_partition_slice_names_by_tier_and_age_splits_by_owner_tier_and_age() -> None:
    stale_name = _ci_slice_name("ci-20260820t000000z-dead")
    young_name = _ci_slice_name("ci-20260820t120000z-live")
    warm_name = _ci_slice_name("ci-warm")
    dev_name = _ci_slice_name("dev-josh")
    legacy_name = f"mngr-slice-{uuid4().hex}"
    ages = {
        stale_name: 5 * 3600.0,
        young_name: 60.0,
        warm_name: 5 * 3600.0,
        dev_name: 10 * 3600.0,
        legacy_name: 10 * 3600.0,
        "some-unrelated-vm": 10 * 3600.0,
    }

    stale, young, foreign = partition_slice_names_by_tier_and_age(ages, "ci", max_age_seconds=4 * 3600.0)

    assert stale == {stale_name, warm_name}
    assert young == {young_name}
    assert foreign == {dev_name}


def test_partition_slice_names_by_tier_and_age_attributes_disk_names_via_their_suffix() -> None:
    disk_name = _ci_slice_name("ci-20260820t000000z-x") + "-data"

    stale, _young, _foreign = partition_slice_names_by_tier_and_age(
        {disk_name: 9 * 3600.0}, "ci", max_age_seconds=4 * 3600.0
    )

    assert stale == {disk_name}


def test_compute_tier_orphan_disk_names_frees_only_tier_disks_whose_instance_is_gone() -> None:
    held_instance = _ci_slice_name("ci-20260820t120000z-live")
    gone_instance = _ci_slice_name("ci-20260819t000000z-gone")
    dev_instance = _ci_slice_name("dev-josh")
    legacy_disk = f"mngr-slice-{uuid4().hex}-data"
    disks = {f"{held_instance}-data", f"{gone_instance}-data", f"{dev_instance}-data", legacy_disk}

    orphans, foreign = compute_tier_orphan_disk_names(disks, "ci", held_instance_names={held_instance})

    assert orphans == {f"{gone_instance}-data"}
    assert foreign == {f"{dev_instance}-data"}


def test_region_label_check_passes_when_the_label_names_the_box_datacenter() -> None:
    assert_region_label_matches_box_datacenter(region_label="US-EAST-VA", box_datacenter="vin")
    assert_region_label_matches_box_datacenter(region_label="US-WEST-OR", box_datacenter="hil")


def test_region_label_check_refuses_a_label_for_another_datacenter() -> None:
    with pytest.raises(BareMetalConfigError, match="bake it with --region US-WEST-OR"):
        assert_region_label_matches_box_datacenter(region_label="US-EAST-VA", box_datacenter="hil")


def test_region_label_check_refuses_unknown_labels_and_datacenters() -> None:
    with pytest.raises(BareMetalConfigError, match="not a known lease region"):
        assert_region_label_matches_box_datacenter(region_label="EU-WEST-FR", box_datacenter="vin")
    with pytest.raises(BareMetalConfigError, match="not in the region map"):
        assert_region_label_matches_box_datacenter(region_label="US-EAST-VA", box_datacenter="gra")


def test_default_slice_service_user_follows_the_box_generation() -> None:
    # Gen-1 boxes keep their lima user; every gen-2 box runs the generic one
    # the prep artifacts and sudoers grants are rendered for.
    assert default_slice_service_user(1) == GEN1_SLICE_SERVICE_USER
    assert default_slice_service_user(2) == GEN2_SLICE_SERVICE_USER
    assert default_slice_service_user(3) == GEN2_SLICE_SERVICE_USER
    assert GEN1_SLICE_SERVICE_USER != GEN2_SLICE_SERVICE_USER


def test_box_service_user_prefers_the_recorded_user_and_falls_back_per_generation() -> None:
    recorded = _server(slice_service_user="custom-user", box_generation=2)
    assert box_service_user(recorded) == "custom-user"
    unrecorded_gen1 = _server(slice_service_user=None, box_generation=1)
    assert box_service_user(unrecorded_gen1) == GEN1_SLICE_SERVICE_USER
    unrecorded_gen2 = _server(slice_service_user=None, box_generation=2)
    assert box_service_user(unrecorded_gen2) == GEN2_SLICE_SERVICE_USER


def test_expected_static_authorized_key_count_is_one_pool_key_on_gen1_and_none_on_gen2() -> None:
    assert expected_static_authorized_key_count(1) == 1
    assert expected_static_authorized_key_count(2) == 0


def test_parse_management_trust_output_splits_keys_from_the_trusted_ca() -> None:
    trust = parse_management_trust_output(
        "ssh-ed25519 AAAApool pool\n# comment\nMNGR_MANAGEMENT_TRUST_SPLIT\nssh-ed25519 AAAAca tier-ca\n"
    )
    assert trust.authorized_key_count == 1
    assert trust.trusted_ca_public_key == "ssh-ed25519 AAAAca tier-ca"
    bare = parse_management_trust_output("MNGR_MANAGEMENT_TRUST_SPLIT\n")
    assert (bare.authorized_key_count, bare.trusted_ca_public_key) == (0, None)
    with pytest.raises(BareMetalConfigError):
        parse_management_trust_output("no marker at all\n")


def test_parse_storage_volume_output_reports_the_luks_mapper_as_encrypted_and_everything_else_as_not() -> None:
    encrypted = parse_storage_volume_output("/dev/mapper/mngr-storage\nMNGR_STORAGE_VOLUME_SPLIT\ncrypt\n")
    assert encrypted == StorageVolumeState(mounted_source="/dev/mapper/mngr-storage", is_encrypted=True)
    # A plain partition mounted at the root: unencrypted, whatever its type says.
    plain = parse_storage_volume_output("/dev/md4\nMNGR_STORAGE_VOLUME_SPLIT\nraid1\n")
    assert plain == StorageVolumeState(mounted_source="/dev/md4", is_encrypted=False)
    # Nothing mounted (a locked volume, or no storage partition at all).
    locked = parse_storage_volume_output("\nMNGR_STORAGE_VOLUME_SPLIT\n")
    assert locked == StorageVolumeState(mounted_source=None, is_encrypted=False)
    # The mapper name alone is not enough: it must be a crypt device.
    assert (
        parse_storage_volume_output("/dev/mapper/mngr-storage\nMNGR_STORAGE_VOLUME_SPLIT\nlvm\n").is_encrypted is False
    )
    with pytest.raises(BareMetalConfigError):
        parse_storage_volume_output("no marker\n")


def test_read_storage_volume_command_reports_an_unmounted_root_without_failing() -> None:
    # findmnt exits non-zero on a non-mount-point, which must read as "nothing
    # mounted" rather than fail the probe (a locked box is exactly that state).
    result = subprocess.run(["bash", "-c", build_read_storage_volume_command()], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert parse_storage_volume_output(result.stdout) == StorageVolumeState(mounted_source=None, is_encrypted=False)


def test_is_trusted_ca_correct_for_tier_requires_the_committed_ca_on_gen2_only() -> None:
    ca = "ssh-ed25519 AAAAca tier-ca"
    trusting = BoxManagementTrust(authorized_key_count=0, trusted_ca_public_key=ca)
    bare = BoxManagementTrust(authorized_key_count=1, trusted_ca_public_key=None)
    # Gen-1 has no CA trust to check.
    assert is_trusted_ca_correct_for_tier(bare, 1, None)
    assert is_trusted_ca_correct_for_tier(trusting, 2, ca + " different-comment")
    assert not is_trusted_ca_correct_for_tier(bare, 2, ca)
    assert not is_trusted_ca_correct_for_tier(trusting, 2, "ssh-ed25519 AAAAother other-tier")
    # No committed CA means a gen-2 box can never be correctly trusted.
    assert not is_trusted_ca_correct_for_tier(trusting, 2, None)


def _run_management_trust_read(home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", build_read_management_trust_command()],
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
    )


def test_read_management_trust_command_fails_on_an_unreadable_authorized_keys(tmp_path: Path) -> None:
    home = tmp_path / "home"
    authorized_keys = home / ".ssh" / "authorized_keys"
    authorized_keys.parent.mkdir(parents=True)
    authorized_keys.write_text("ssh-ed25519 AAAApool pool\n")
    readable = _run_management_trust_read(home)
    assert readable.returncode == 0, readable.stderr
    # Only the key half is asserted: the machine running this test may itself pin a CA.
    assert parse_management_trust_output(readable.stdout).authorized_key_count == 1

    # An authorized_keys that exists but cannot be read (a directory fails ``cat``
    # even for root, unlike a chmod) must fail the whole read rather than come
    # back as "zero keys", which is what a gen-2 box is expected to report.
    authorized_keys.unlink()
    authorized_keys.mkdir()
    unreadable = _run_management_trust_read(home)
    assert unreadable.returncode != 0
    assert "MNGR_MANAGEMENT_TRUST_SPLIT" not in unreadable.stdout


def test_gen2_default_machine_fit_counts_the_machines_a_short_disk_still_holds() -> None:
    # The hil dev canary: 128GB RAM sells 14 default machines needing 756GiB,
    # its measured 792GB partition yields a 707GiB budget, so 13 of them fit.
    fit = compute_gen2_box_default_machine_fit(ram_gb=128, disk_gb=792)
    assert fit.machine_capacity == 14
    assert fit.required_disk_budget_gib == 756
    assert not fit.is_sufficient
    assert fit.machines_that_fit == 13
    assert compute_gen2_box_default_machine_fit(ram_gb=128, disk_gb=872).is_sufficient


def test_gen2_disk_shortfall_message_names_the_gib_estimate_and_the_partial_fit() -> None:
    fit = compute_gen2_box_default_machine_fit(ram_gb=128, disk_gb=792)
    message = describe_gen2_box_disk_shortfall(fit, ram_gb=128, disk_gb=792)
    assert "disk_gb=792 (usable GB, compared as an estimated 707GiB storage budget" in message
    assert "holds 13 of them" in message
