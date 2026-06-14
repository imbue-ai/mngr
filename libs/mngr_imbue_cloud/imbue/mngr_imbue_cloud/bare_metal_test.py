from datetime import datetime
from datetime import timezone

import pytest

from imbue.mngr.primitives import HostId
from imbue.mngr_imbue_cloud.bare_metal import allocate_slice_ports
from imbue.mngr_imbue_cloud.bare_metal import choose_raid_level
from imbue.mngr_imbue_cloud.bare_metal import choose_server_for_new_slice
from imbue.mngr_imbue_cloud.bare_metal import compute_capacity
from imbue.mngr_imbue_cloud.bare_metal import compute_slice_disk_gib
from imbue.mngr_imbue_cloud.bare_metal import compute_slice_memory_mib
from imbue.mngr_imbue_cloud.bare_metal import compute_slice_vcpus
from imbue.mngr_imbue_cloud.bare_metal import compute_slot_count
from imbue.mngr_imbue_cloud.bare_metal import is_valid_status_transition
from imbue.mngr_imbue_cloud.bare_metal import next_server_status
from imbue.mngr_imbue_cloud.bare_metal import slice_lima_disk_name
from imbue.mngr_imbue_cloud.bare_metal import slice_lima_instance_name
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


def _server(status: str, slot_count: int = 8) -> BareMetalServer:
    now = datetime.now(timezone.utc)
    return BareMetalServer(
        id=BareMetalServerDbId("11111111-1111-1111-1111-111111111111"),
        plan_code="24rise02-v1-us",
        region="vin",
        slot_count=slot_count,
        status=BareMetalServerStatus(status),
        created_at=now,
        updated_at=now,
    )


def test_compute_slot_count_floors_by_memory_per_slice() -> None:
    assert compute_slot_count(64, 8) == 8
    assert compute_slot_count(128, 8) == 16
    assert compute_slot_count(32, 8) == 4
    # 70GB only yields 8 whole 8GB slices (the remainder is host headroom).
    assert compute_slot_count(70, 8) == 8
    assert compute_slot_count(4, 8) == 0
    # A larger per-slice RAM yields fewer slots.
    assert compute_slot_count(64, 16) == 4


def test_compute_slot_count_rejects_negative_ram_and_nonpositive_per_slice() -> None:
    with pytest.raises(BareMetalConfigError):
        compute_slot_count(-1, 8)
    with pytest.raises(BareMetalConfigError):
        compute_slot_count(64, 0)


def test_compute_slice_memory_mib_subtracts_overhead() -> None:
    # 8 GiB advertised minus the 512 MiB per-slice overhead.
    assert compute_slice_memory_mib(8) == 8 * 1024 - 512
    assert compute_slice_memory_mib(16) == 16 * 1024 - 512


def test_compute_slice_memory_mib_rejects_too_small() -> None:
    with pytest.raises(BareMetalConfigError):
        compute_slice_memory_mib(0)


def test_compute_slice_disk_gib_splits_usable_disk() -> None:
    # (500 - 20 reserve) / 8 slots = 60 GiB each.
    assert compute_slice_disk_gib(500, 8) == 60
    # Remainder is dropped (floor).
    assert compute_slice_disk_gib(477, 8) == (477 - 20) // 8


def test_compute_slice_disk_gib_rejects_when_no_room() -> None:
    with pytest.raises(BareMetalConfigError):
        compute_slice_disk_gib(20, 8)
    with pytest.raises(BareMetalConfigError):
        compute_slice_disk_gib(500, 0)


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


def test_allocate_slice_ports_returns_two_lowest_free_ports() -> None:
    first, second = allocate_slice_ports(used_ports={22000, 22001}, port_range_start=22000, port_range_end=22010)
    assert (first, second) == (22002, 22003)
    assert first != second


def test_allocate_slice_ports_raises_when_fewer_than_two_free() -> None:
    with pytest.raises(SliceCapacityError):
        allocate_slice_ports(used_ports={22000}, port_range_start=22000, port_range_end=22002)


def test_allocate_slice_ports_rejects_empty_range() -> None:
    with pytest.raises(BareMetalConfigError):
        allocate_slice_ports(used_ports=set(), port_range_start=22000, port_range_end=22000)


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


def test_choose_server_for_new_slice_picks_most_free_ready_server() -> None:
    nearly_full = compute_capacity(_server(SERVER_STATUS_READY, slot_count=8), used_slots=7)
    roomy = compute_capacity(_server(SERVER_STATUS_READY, slot_count=16), used_slots=2)
    chosen = choose_server_for_new_slice([nearly_full, roomy])
    assert chosen.free_slots == 14


def test_choose_server_for_new_slice_ignores_non_ready_and_full_servers() -> None:
    installing = compute_capacity(_server(SERVER_STATUS_INSTALLING, slot_count=16), used_slots=0)
    full_ready = compute_capacity(_server(SERVER_STATUS_READY, slot_count=8), used_slots=8)
    with pytest.raises(SliceCapacityError):
        choose_server_for_new_slice([installing, full_ready])
