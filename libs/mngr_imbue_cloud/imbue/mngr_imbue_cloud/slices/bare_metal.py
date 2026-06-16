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

# Each slice VM has TWO disks whose sizes must sum to the slice's disk budget (no
# disk overcommit, just like RAM): a fixed boot disk holding the guest OS + Docker
# (the FCT image + build cache + container layers -- ~11GiB observed, sized with
# headroom for build spikes) and a btrfs data disk (the rest of the budget) mounted
# at the host_dir for the agent's per-host volume. lima would otherwise default the
# boot disk to 100GiB, which (unaccounted) would massively overcommit the box.
SLICE_BOOT_DISK_GIB: Final[int] = 32

# Default RAM (GB) each slice advertises / is sized to. A box's slot count is
# floor(total_RAM / this), so it also sets how many slices a box yields. Used as the
# default for the pricing table and the natural slice size for our workspaces.
DEFAULT_MEMORY_PER_SLICE_GB: Final[int] = 8

# Default CPU overcommit factor used to size each slice's vCPUs (vCPUs/slice =
# floor(threads * ratio / slots)). Overridable per box at ``admin server
# register --cpu-overcommit``; RAM is never overcommitted.
DEFAULT_SLICE_CPU_OVERCOMMIT_RATIO: Final[float] = 2.0

# Range of host ports on each box reserved for slice port-forwards. Each slice
# claims two: one -> the VM's root sshd, one -> the inner container sshd. Wide
# enough (~10k ports) for large boxes carved into many slices.
DEFAULT_SLICE_PORT_RANGE_START: Final[int] = 22000
DEFAULT_SLICE_PORT_RANGE_END: Final[int] = 32000

# The slice guest OS image is staged once on each box (at prep) and referenced by
# the slice bake via ``file://`` so VM boots never depend on the Debian mirror
# (lima otherwise does a per-boot last-modified HEAD to cloud.debian.org for a
# digest-less image, which fatally fails when the mirror is flaky). Stored under
# the lima service user's home so prep can write it without root, and read by
# limactl (which runs as that user). Path is shared by the prep script and the
# slice provider so they always agree.
_SLICE_BASE_IMAGE_RELPATH: Final[str] = ".cache/mngr-slice-base/debian-base.qcow2"


def slice_base_image_path(lima_service_user: str) -> str:
    """Absolute path of the box-staged slice guest OS image for ``lima_service_user``."""
    return f"/home/{lima_service_user}/{_SLICE_BASE_IMAGE_RELPATH}"


def slice_base_image_file_url(lima_service_user: str) -> str:
    """``file://`` URL the slice lima YAML uses for the box-staged guest OS image."""
    return f"file://{slice_base_image_path(lima_service_user)}"


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
def compute_slice_disk_budget_gib(disk_gb: int, slot_count: int) -> int:
    """Return the TOTAL disk budget for one slice: usable disk (minus reserve) split across slots.

    This budget is the slice VM's whole disk allocation -- boot disk + data disk
    must sum to it, so the box is never over-provisioned on disk.
    """
    if slot_count <= 0:
        raise BareMetalConfigError(f"slot_count must be positive, got {slot_count}")
    per_slice_budget_gib = (disk_gb - DISK_RESERVE_GB) // slot_count
    if per_slice_budget_gib <= 0:
        raise BareMetalConfigError(
            f"disk_gb={disk_gb} minus {DISK_RESERVE_GB}GB reserve cannot be split across {slot_count} slot(s)"
        )
    return per_slice_budget_gib


@pure
def compute_slice_disk_gib(disk_gb: int, slot_count: int) -> int:
    """Return the per-slice btrfs DATA-disk size: the disk budget minus the fixed boot disk.

    Boot disk (``SLICE_BOOT_DISK_GIB``) + this data disk = the per-slice budget, so
    the two disks together never exceed the box's allocated-per-slice disk.
    """
    data_disk_gib = compute_slice_disk_budget_gib(disk_gb, slot_count) - SLICE_BOOT_DISK_GIB
    if data_disk_gib <= 0:
        raise BareMetalConfigError(
            f"per-slice disk budget for disk_gb={disk_gb} across {slot_count} slot(s) is too small to fit the "
            f"{SLICE_BOOT_DISK_GIB}GiB boot disk plus any data disk"
        )
    return data_disk_gib


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


# Lima instance-name prefix for slices (embeds the mngr host id). Used both to
# derive a slice's deterministic instance name and to recognize slice VMs on the
# box when reaping orphans, so reconciliation never touches a non-slice lima VM.
SLICE_LIMA_INSTANCE_PREFIX: Final[str] = "mngr-slice-"


@pure
def slice_lima_instance_name(host_id: HostId) -> str:
    """Deterministic lima instance name for a slice, embedding the mngr host id."""
    return f"{SLICE_LIMA_INSTANCE_PREFIX}{host_id.get_uuid().hex}"


@pure
def slice_lima_disk_name(host_id: HostId) -> str:
    """Deterministic lima additional-disk name (the slice's btrfs data disk)."""
    return f"{SLICE_LIMA_INSTANCE_PREFIX}{host_id.get_uuid().hex}-data"


@pure
def compute_orphan_slice_instance_names(
    box_instance_names: AbstractSet[str],
    tracked_instance_names: AbstractSet[str],
) -> set[str]:
    """Slice VMs present on the box but absent from the pool DB -- safe to reap.

    Filters to slice-owned instances (the ``mngr-slice-`` prefix) so reconciliation
    never touches an unrelated lima VM, then subtracts the tracked set (every
    instance that has a pool_hosts row, any status). A ``mngr create`` killed by its
    own timeout after carving the VM but before the row insert leaves exactly such
    an orphan -- the provider's rollback never ran. Assumes no other bake invocation
    is concurrently mid-carve against the same box (an in-flight VM not yet inserted
    would otherwise look like an orphan).
    """
    return {
        name
        for name in box_instance_names
        if name.startswith(SLICE_LIMA_INSTANCE_PREFIX) and name not in tracked_instance_names
    }


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
def partition_port_range(
    port_range_start: int, port_range_end: int, partition_count: int, index: int
) -> tuple[int, int]:
    """Split ``[start, end)`` into ``partition_count`` disjoint sub-ranges; return the ``index``-th.

    ``allocate-slice`` bakes several slices on one box concurrently, each in its
    own ``mngr create`` process that picks the lowest free ports in the range it is
    given. If every process saw the whole range it would deterministically pick the
    same two lowest ports (the in-process probe only sees ports already bound, not a
    sibling bake's chosen-but-not-yet-bound ports), so the bakes would collide.
    Giving each bake a disjoint window removes that collision. Each window must hold
    at least the two ports a slice needs.
    """
    if partition_count <= 0:
        raise BareMetalConfigError(f"partition_count must be positive, got {partition_count}")
    if not 0 <= index < partition_count:
        raise BareMetalConfigError(f"index {index} out of range for {partition_count} partition(s)")
    total = port_range_end - port_range_start
    window = total // partition_count
    if window < 2:
        raise SliceCapacityError(
            f"port range [{port_range_start}, {port_range_end}) is too small to split across "
            f"{partition_count} slice(s) (each needs at least 2 ports)"
        )
    window_start = port_range_start + index * window
    # The last partition absorbs any remainder so the full range is covered.
    window_end = port_range_end if index == partition_count - 1 else window_start + window
    return window_start, window_end


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
