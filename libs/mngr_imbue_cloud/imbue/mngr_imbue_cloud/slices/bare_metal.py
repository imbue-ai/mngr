import math
import re
from collections.abc import Sequence
from typing import AbstractSet
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.mngr.primitives import HostId
from imbue.mngr_imbue_cloud.data_types import BareMetalServer
from imbue.mngr_imbue_cloud.data_types import BareMetalServerCapacity
from imbue.mngr_imbue_cloud.errors import BareMetalConfigError
from imbue.mngr_imbue_cloud.errors import SliceCapacityError
from imbue.mngr_imbue_cloud.primitives import BareMetalServerDbId
from imbue.mngr_imbue_cloud.primitives import BareMetalServerStatus
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_DELIVERED
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_FAILED
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_INSTALLING
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_ORDERED
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_READY

# RAM overhead is modeled in two parts so a box's slot count reflects what it can
# REALISTICALLY run without overcommitting RAM:
#  - PER-MACHINE (``HOST_RAM_RESERVE_GIB``): a fixed reserve for the kernel/OS plus
#    page-cache/network headroom, subtracted once from the box's total RAM. (Measured
#    ~3GiB kernel baseline on a busy box; 8 leaves a safety buffer so the box never
#    runs at the ragged edge with the OOM killer.)
#  - PER-VM (``PER_VM_RAM_OVERHEAD_MIB``): host-side overhead for EACH slice on top of
#    its guest RAM -- the QEMU process (control structures + page tables) and the
#    per-VM lima supervisor. (Measured ~0.2GiB/VM; 512 is conservative.) The guest
#    itself gets the full advertised ``memory_per_slice_gb``.
HOST_RAM_RESERVE_GIB: Final[int] = 8
PER_VM_RAM_OVERHEAD_MIB: Final[int] = 512

# Disk held back on each box before the rest is split among slices, in two parts so a
# per-slice allocation never exceeds the box's REAL usable filesystem:
#  - ``DISK_RESERVE_GB``: a fixed floor for the OS + lima/management, and
#  - ``DISK_RESERVE_FRACTION``: a fraction of the registered ``disk_gb`` that absorbs
#    the GB-vs-GiB gap (an "N TB" spec is N*10^9 bytes ~= 0.93*N GiB) plus partition +
#    filesystem metadata, so a nominally-registered disk_gb does not overcommit the
#    actual disk. The reserve used is the larger of the two.
DISK_RESERVE_GB: Final[int] = 20
DISK_RESERVE_FRACTION: Final[float] = 0.10

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
    """Return how many slices of ``memory_per_slice_gb`` a box with ``ram_gb`` total RAM holds.

    Subtracts the per-machine host reserve (``HOST_RAM_RESERVE_GIB``) once, then divides
    the rest by the per-slice footprint -- the guest's advertised RAM PLUS the per-VM
    host overhead (``PER_VM_RAM_OVERHEAD_MIB``). So the count is what the box can run
    without overcommitting RAM, not just ``total / slice`` (which left no host headroom).
    """
    if ram_gb < 0:
        raise BareMetalConfigError(f"ram_gb must be non-negative, got {ram_gb}")
    if memory_per_slice_gb <= 0:
        raise BareMetalConfigError(f"memory_per_slice_gb must be positive, got {memory_per_slice_gb}")
    usable_mib = ram_gb * 1024 - HOST_RAM_RESERVE_GIB * 1024
    per_slice_footprint_mib = memory_per_slice_gb * 1024 + PER_VM_RAM_OVERHEAD_MIB
    return max(0, usable_mib // per_slice_footprint_mib)


@pure
def compute_slice_memory_mib(memory_per_slice_gb: int) -> int:
    """Return the MiB to allocate each slice VM: the full advertised RAM.

    The per-VM host overhead (QEMU + lima supervisor) is accounted on top in
    ``compute_slot_count``, NOT taken from the guest -- so the guest gets exactly the
    advertised ``memory_per_slice_gb``.
    """
    if memory_per_slice_gb <= 0:
        raise BareMetalConfigError(f"memory_per_slice_gb must be positive, got {memory_per_slice_gb}")
    return memory_per_slice_gb * 1024


@pure
def compute_slice_disk_budget_gib(disk_gb: int, slot_count: int) -> int:
    """Return the TOTAL disk budget for one slice: usable disk (minus reserve) split across slots.

    This budget is the slice VM's whole disk allocation -- boot disk + data disk
    must sum to it, so the box is never over-provisioned on disk.
    """
    if slot_count <= 0:
        raise BareMetalConfigError(f"slot_count must be positive, got {slot_count}")
    reserve_gib = max(DISK_RESERVE_GB, math.ceil(disk_gb * DISK_RESERVE_FRACTION))
    per_slice_budget_gib = (disk_gb - reserve_gib) // slot_count
    if per_slice_budget_gib <= 0:
        raise BareMetalConfigError(
            f"disk_gb={disk_gb} minus {reserve_gib}GiB reserve cannot be split across {slot_count} slot(s)"
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


# Lima instance-name prefix for slices. Used both to derive a slice's
# deterministic instance name and to recognize slice VMs on the box, so
# reconciliation never touches a non-slice lima VM.
SLICE_LIMA_INSTANCE_PREFIX: Final[str] = "mngr-slice-"

# Suffix appended to a slice's instance name to name its btrfs data disk.
SLICE_LIMA_DISK_SUFFIX: Final[str] = "-data"

# A slice's host id is a uuid hex (exactly 32 lowercase hex chars, no hyphens), so
# it cleanly delimits the optional env stamp that precedes it in a stamped name.
_SLICE_HOST_ID_PATTERN: Final[str] = r"[0-9a-f]{32}"
_STAMPED_SLICE_CORE_RE: Final[re.Pattern[str]] = re.compile(rf"^(?P<env>.+)-(?P<host>{_SLICE_HOST_ID_PATTERN})$")


@pure
def slice_lima_instance_name(host_id: HostId, env_name: str | None = None) -> str:
    """Deterministic lima instance name for a slice, embedding the mngr host id.

    When ``env_name`` is given the owning env is stamped in
    (``mngr-slice-<env>-<host-hex>``) so the box can attribute the slice to an
    environment and reconciliation can scope itself to one env's slices. Without it
    the legacy un-stamped name (``mngr-slice-<host-hex>``) is produced, for
    backwards compatibility with slices baked before env stamping.
    """
    host_hex = host_id.get_uuid().hex
    if env_name is None:
        return f"{SLICE_LIMA_INSTANCE_PREFIX}{host_hex}"
    return f"{SLICE_LIMA_INSTANCE_PREFIX}{env_name}-{host_hex}"


@pure
def slice_lima_disk_name(host_id: HostId, env_name: str | None = None) -> str:
    """Deterministic lima additional-disk name (the slice's btrfs data disk)."""
    return f"{slice_lima_instance_name(host_id, env_name)}{SLICE_LIMA_DISK_SUFFIX}"


@pure
def _slice_resource_core(name: str) -> str | None:
    """The identity part of a slice instance/disk name: prefix and optional ``-data`` stripped.

    Returns None for any name that is not a slice resource (wrong prefix), so a
    non-slice lima resource is never misclassified.
    """
    if not name.startswith(SLICE_LIMA_INSTANCE_PREFIX):
        return None
    core = name[len(SLICE_LIMA_INSTANCE_PREFIX) :]
    if core.endswith(SLICE_LIMA_DISK_SUFFIX):
        core = core[: -len(SLICE_LIMA_DISK_SUFFIX)]
    return core


@pure
def slice_name_env_owner(name: str) -> str | None:
    """The env a slice instance/disk name is stamped for, or None if legacy/foreign/not-a-slice.

    A stamped name is ``mngr-slice-<env>-<host-hex>``; a legacy name
    (``mngr-slice-<host-hex>``) and any non-slice name both return None. The host
    hex is a hyphen-free uuid, so the env is everything between the prefix and the
    trailing ``-<host-hex>``.
    """
    core = _slice_resource_core(name)
    if core is None:
        return None
    match = _STAMPED_SLICE_CORE_RE.match(core)
    return match.group("env") if match else None


@pure
def is_slice_owned_by_env(name: str, env_name: str) -> bool:
    """Whether a slice instance/disk name is stamped for exactly ``env_name``."""
    return slice_name_env_owner(name) == env_name


@pure
def count_slice_resource_names(names: AbstractSet[str]) -> int:
    """Count slice resources (``mngr-slice-`` prefix) regardless of env stamp.

    Used to derive a box's TRUE occupancy from its lima resources -- every env's
    slices plus any legacy un-stamped ones -- so independent envs sharing the box
    cannot collectively over-subscribe it.
    """
    return sum(1 for name in names if name.startswith(SLICE_LIMA_INSTANCE_PREFIX))


@pure
def _orphan_slice_resource_names(
    box_names: AbstractSet[str],
    tracked_names: AbstractSet[str],
    env_name: str,
) -> set[str]:
    """Slice resources on the box stamped for ``env_name`` with no pool DB row.

    Shared by the instance and disk reconciliation: only names stamped for this env
    are candidates, so reconciliation never touches another env's slices or legacy
    (un-stamped) slices; the tracked set (this env's rows) is then subtracted.
    """
    return {name for name in box_names if is_slice_owned_by_env(name, env_name) and name not in tracked_names}


@pure
def compute_orphan_slice_instance_names(
    box_instance_names: AbstractSet[str],
    tracked_instance_names: AbstractSet[str],
    env_name: str,
) -> set[str]:
    """This env's slice VMs present on the box but absent from the pool DB -- safe to reap.

    Filters to instances stamped for ``env_name`` so reconciliation never touches
    another env's slices, a legacy un-stamped slice, or an unrelated lima VM, then
    subtracts the tracked set (every instance that has a pool_hosts row in this env's
    DB, any status). A ``mngr create`` killed by its own timeout after carving the VM
    but before the row insert leaves exactly such an orphan -- the provider's rollback
    never ran. Assumes no other bake invocation of this same env is concurrently
    mid-carve against the box (an in-flight VM not yet inserted would otherwise look
    like an orphan); other envs' in-flight carves are stamped differently and ignored.
    """
    return _orphan_slice_resource_names(box_instance_names, tracked_instance_names, env_name)


@pure
def compute_orphan_slice_disk_names(
    box_disk_names: AbstractSet[str],
    tracked_disk_names: AbstractSet[str],
    env_name: str,
) -> set[str]:
    """This env's slice data disks present on the box but absent from the pool DB -- safe to reap.

    The disk analogue of :func:`compute_orphan_slice_instance_names`. Reaped separately
    because a disk can outlive its instance: if a failed carve's rollback ``limactl
    delete`` errors for a non-absent reason (e.g. the data disk is locked), it raises
    before deleting the disk, leaving the disk behind even though the VM is gone -- and
    a leaked disk permanently holds the box slot until reclaimed.
    """
    return _orphan_slice_resource_names(box_disk_names, tracked_disk_names, env_name)


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
def find_server_capacity_by_id(
    capacities: Sequence[BareMetalServerCapacity], server_id: BareMetalServerDbId
) -> BareMetalServerCapacity:
    """Return the capacity row for the explicitly chosen ``server_id``.

    Slice baking targets one operator-named box per invocation (its per-slice sizing is fixed at
    registration), rather than auto-selecting a server. Raises ``SliceCapacityError`` if no server in
    ``capacities`` has that id -- the readiness + free-slot checks are the caller's, so the error can
    name the count it needed.
    """
    for capacity in capacities:
        if capacity.server.id == server_id:
            return capacity
    raise SliceCapacityError(
        f"no bare-metal server with id {server_id}; run `mngr imbue_cloud admin server list` to see the fleet"
    )
