import math
from collections.abc import Sequence
from typing import AbstractSet
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.mngr.primitives import HostId
from imbue.mngr_imbue_cloud.data_types import BareMetalServer
from imbue.mngr_imbue_cloud.data_types import BareMetalServerCapacity
from imbue.mngr_imbue_cloud.errors import BareMetalConfigError
from imbue.mngr_imbue_cloud.errors import SliceCapacityError
from imbue.mngr_imbue_cloud.primitives import BareMetalServerStatus
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_DELIVERED
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_FAILED
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_INSTALLING
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_ORDERED
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_READY

# Per-slice RAM (MiB) held back from the VM for host + per-VM QEMU overhead: each
# slice advertises ``memory_per_slice_gb`` but is allocated that minus this, so a
# box's slices fit without RAM overcommit (the held-back total is the host's share).
PER_SLICE_MEMORY_OVERHEAD_MIB: Final[int] = 512

# Disk (GiB) held back on each box for the OS + lima/management before the rest of
# the usable disk is divided evenly among the box's slices.
DISK_RESERVE_GB: Final[int] = 20

# Range of host ports on each box reserved for slice port-forwards. Each slice
# claims two: one -> the VM's root sshd, one -> the inner container sshd. Wide
# enough (~10k ports) for large boxes carved into many slices.
DEFAULT_SLICE_PORT_RANGE_START: Final[int] = 22000
DEFAULT_SLICE_PORT_RANGE_END: Final[int] = 32000

_RAID_MIRROR: Final[str] = "RAID1"
_RAID_STRIPED_MIRROR: Final[str] = "RAID10"

# Forward lifecycle: each non-terminal status advances to exactly one next status.
_NEXT_STATUS_BY_CURRENT: Final[dict[str, str]] = {
    SERVER_STATUS_ORDERED: SERVER_STATUS_DELIVERED,
    SERVER_STATUS_DELIVERED: SERVER_STATUS_INSTALLING,
    SERVER_STATUS_INSTALLING: SERVER_STATUS_READY,
}
_TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({SERVER_STATUS_READY, SERVER_STATUS_FAILED})


@pure
def compute_slot_count(ram_gb: int, memory_per_slice_gb: int) -> int:
    """Return how many slices of ``memory_per_slice_gb`` a box with ``ram_gb`` total RAM holds."""
    if ram_gb < 0:
        raise BareMetalConfigError(f"ram_gb must be non-negative, got {ram_gb}")
    if memory_per_slice_gb <= 0:
        raise BareMetalConfigError(f"memory_per_slice_gb must be positive, got {memory_per_slice_gb}")
    return ram_gb // memory_per_slice_gb


@pure
def compute_slice_memory_mib(memory_per_slice_gb: int) -> int:
    """Return the MiB to allocate each slice VM: the advertised RAM minus host/QEMU overhead."""
    if memory_per_slice_gb <= 0:
        raise BareMetalConfigError(f"memory_per_slice_gb must be positive, got {memory_per_slice_gb}")
    memory_mib = memory_per_slice_gb * 1024 - PER_SLICE_MEMORY_OVERHEAD_MIB
    if memory_mib <= 0:
        raise BareMetalConfigError(
            f"memory_per_slice_gb={memory_per_slice_gb} is too small for the "
            f"{PER_SLICE_MEMORY_OVERHEAD_MIB}MiB per-slice overhead"
        )
    return memory_mib


@pure
def compute_slice_disk_gib(disk_gb: int, slot_count: int) -> int:
    """Return the per-slice btrfs data-disk size: usable disk (minus reserve) split across slots."""
    if slot_count <= 0:
        raise BareMetalConfigError(f"slot_count must be positive, got {slot_count}")
    usable_disk_gb = disk_gb - DISK_RESERVE_GB
    per_slice_disk_gib = usable_disk_gb // slot_count
    if per_slice_disk_gib <= 0:
        raise BareMetalConfigError(
            f"disk_gb={disk_gb} minus {DISK_RESERVE_GB}GB reserve cannot be split across {slot_count} slot(s)"
        )
    return per_slice_disk_gib


@pure
def compute_slice_vcpus(cpu_threads: int, slot_count: int, overcommit_ratio: float) -> int:
    """Return the vCPU count to give each slice, applying mild CPU overcommit."""
    if cpu_threads <= 0:
        raise BareMetalConfigError(f"cpu_threads must be positive, got {cpu_threads}")
    if slot_count <= 0:
        raise BareMetalConfigError(f"slot_count must be positive, got {slot_count}")
    if overcommit_ratio <= 0:
        raise BareMetalConfigError(f"overcommit_ratio must be positive, got {overcommit_ratio}")
    return max(1, math.floor(cpu_threads * overcommit_ratio / slot_count))


@pure
def choose_raid_level(disk_count: int) -> str:
    """Pick a mirror-based RAID level for disk-failure robustness: RAID1 (2 disks) or RAID10 (4+)."""
    if disk_count < 2:
        raise BareMetalConfigError(f"need at least 2 disks for redundancy, got {disk_count}")
    if disk_count == 2:
        return _RAID_MIRROR
    if disk_count % 2 == 0:
        return _RAID_STRIPED_MIRROR
    raise BareMetalConfigError(
        f"odd disk count {disk_count} cannot be evenly mirrored (need 2 or an even number >= 4)"
    )


@pure
def slice_lima_instance_name(host_id: HostId) -> str:
    """Deterministic lima instance name for a slice, embedding the mngr host id."""
    return f"mngr-slice-{host_id.get_uuid().hex}"


@pure
def slice_lima_disk_name(host_id: HostId) -> str:
    """Deterministic lima additional-disk name (the slice's btrfs data disk)."""
    return f"mngr-slice-{host_id.get_uuid().hex}-data"


@pure
def allocate_slice_ports(
    used_ports: AbstractSet[int],
    port_range_start: int,
    port_range_end: int,
) -> tuple[int, int]:
    """Pick two distinct free host ports in ``[start, end)`` for a slice's two forwards.

    The first is for the VM's root sshd, the second for the inner container sshd.
    Raises ``SliceCapacityError`` if fewer than two free ports remain in the range.
    """
    if port_range_end <= port_range_start:
        raise BareMetalConfigError(f"empty port range [{port_range_start}, {port_range_end})")
    free_ports = [port for port in range(port_range_start, port_range_end) if port not in used_ports]
    if len(free_ports) < 2:
        raise SliceCapacityError(
            f"need 2 free ports in [{port_range_start}, {port_range_end}) but only {len(free_ports)} remain"
        )
    return free_ports[0], free_ports[1]


@pure
def next_server_status(current: BareMetalServerStatus) -> BareMetalServerStatus | None:
    """Return the next forward lifecycle status, or None if ``current`` is terminal (ready/failed)."""
    next_value = _NEXT_STATUS_BY_CURRENT.get(str(current))
    return BareMetalServerStatus(next_value) if next_value is not None else None


@pure
def is_valid_status_transition(current: BareMetalServerStatus, target: BareMetalServerStatus) -> bool:
    """Whether advancing a server from ``current`` to ``target`` is allowed.

    Forward moves follow the fixed ordered->delivered->installing->ready chain;
    a move to ``failed`` is allowed from any non-terminal state; terminal states
    (ready/failed) admit no further transitions.
    """
    current_value = str(current)
    target_value = str(target)
    if current_value in _TERMINAL_STATUSES:
        return False
    if target_value == SERVER_STATUS_FAILED:
        return True
    return _NEXT_STATUS_BY_CURRENT.get(current_value) == target_value


@pure
def compute_capacity(server: BareMetalServer, used_slots: int) -> BareMetalServerCapacity:
    """Pair a server with its slot accounting (used / free)."""
    if used_slots < 0:
        raise BareMetalConfigError(f"used_slots must be non-negative, got {used_slots}")
    free_slots = max(0, server.slot_count - used_slots)
    return BareMetalServerCapacity(server=server, used_slots=used_slots, free_slots=free_slots)


@pure
def choose_server_for_new_slice(capacities: Sequence[BareMetalServerCapacity]) -> BareMetalServerCapacity:
    """Pick the ready server with the most free slots to bake the next slice onto.

    Raises ``SliceCapacityError`` if no ready server has any free slots.
    """
    eligible = [
        capacity
        for capacity in capacities
        if str(capacity.server.status) == SERVER_STATUS_READY and capacity.free_slots > 0
    ]
    if not eligible:
        raise SliceCapacityError("no ready bare-metal server has free slots; order or install more capacity")
    return max(eligible, key=lambda capacity: capacity.free_slots)
