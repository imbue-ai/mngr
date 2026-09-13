import math
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.mngr_imbue_cloud.slices.gen2_scripts.errors import InvalidMachineSizeError

# RAM overhead is modeled in two parts so a box's capacity reflects what it can
# REALISTICALLY run without overcommitting RAM:
#  - PER-BOX (``HOST_RAM_RESERVE_GIB``): a fixed reserve for the kernel/OS plus
#    page-cache/network headroom, subtracted once from the box's total RAM. (Measured
#    ~3GiB kernel baseline on a busy box; 8 leaves a safety buffer so the box never
#    runs at the ragged edge with the OOM killer.)
#  - PER-VM (``PER_VM_RAM_OVERHEAD_MIB``): host-side overhead for EACH slice on top of
#    its guest RAM -- the QEMU process (control structures + page tables) and, on
#    gen-1, the per-VM lima supervisor. (Measured ~0.2GiB/VM; 512 is conservative.)
HOST_RAM_RESERVE_GIB: Final[int] = 8
PER_VM_RAM_OVERHEAD_MIB: Final[int] = 512
# RAM held back from the guest so a machine's whole footprint fits its budget
# share: the unit's ``MemoryMax`` is exactly ``units x 1024 + PER_VM_RAM_OVERHEAD_MIB``
# (so the caps of every machine on a full box sum to the budget and the box is
# never overcommitted), and inside that the guest gets ``units x 1024`` minus
# this, leaving ~1 GiB per machine for qemu's own ~300-400 MiB plus whatever
# host-side cache O_DIRECT still lets through. An "8 GiB" machine boots with 7.5
# GiB of guest RAM the way any cloud VM's OS takes its cut; the container's cap
# follows the guest's visible RAM.
GUEST_RAM_HOLDBACK_MIB: Final[int] = 512

# Gen-1 disk held back on each box before the rest is split among slices, in two
# parts so a per-slice allocation never exceeds the box's REAL usable filesystem:
#  - ``DISK_RESERVE_GB``: a fixed floor for the OS + management tooling, and
#  - ``DISK_RESERVE_FRACTION``: a fraction of the registered ``disk_gb`` that absorbs
#    the GB-vs-GiB gap (an "N TB" spec is N*10^9 bytes ~= 0.93*N GiB) plus partition +
#    filesystem metadata, so a nominally-registered disk_gb does not overcommit the
#    actual disk. The reserve used is the larger of the two.
DISK_RESERVE_GB: Final[int] = 20
DISK_RESERVE_FRACTION: Final[float] = 0.10

# Gen-2 disk accounting works on the MEASURED storage partition (the XFS
# partition prep mounts at the storage root; its GiB is recorded as the box
# row's ``disk_gb`` at prep), not on the catalog's usable-disk figure: the
# gen-2 reinstall carves the root and boot partitions out of that figure
# first. The reserve is the sum of everything that lives on the storage
# partition outside the machines' disks, each part named so the total is
# auditable:
#  - the box swapfile (moved off the small root partition),
#  - the per-tag image tar cache (one ``docker save`` tar per baked tag),
#  - the staged guest base image,
#  - a margin for transfer staging (stop/start artifacts stream through the
#    slice service user's home, which is on the root partition, but a restore
#    lands its disks on the storage partition before the reserve's df guard
#    sees them) and filesystem metadata.
GEN2_SWAPFILE_GIB: Final[int] = 32
GEN2_IMAGE_TAR_CACHE_GIB: Final[int] = 16
GEN2_BASE_IMAGE_GIB: Final[int] = 4
GEN2_STAGING_MARGIN_GIB: Final[int] = 12
GEN2_STORAGE_RESERVE_GIB: Final[int] = (
    GEN2_SWAPFILE_GIB + GEN2_IMAGE_TAR_CACHE_GIB + GEN2_BASE_IMAGE_GIB + GEN2_STAGING_MARGIN_GIB
)
# The gen-2 reinstall's fixed partitions (``build_gen2_reinstall_storage`` in the
# operator tooling): what the pre-delivery ordering guard subtracts from the
# catalog's usable figure to estimate the storage partition a not-yet-delivered
# box will have. The root holds only the OS, journald, apt and the prep
# artifacts; the swapfile and the image tar cache live on the storage partition.
GEN2_ROOT_PARTITION_GIB: Final[int] = 20
GEN2_BOOT_PARTITION_GIB: Final[int] = 1

# Each slice VM has TWO disks whose sizes must sum to the slice's disk budget (no
# disk overcommit, just like RAM): a boot disk and a btrfs data disk mounted at
# the host_dir for the agent's per-host volume.
#
# Gen-1 (lima) boot disk: it holds the guest OS AND Docker (the agent host image
# + build cache + container layers, ~11GiB observed), so it needs room; the data
# disk is the rest of the slot's budget. Every deployed gen-1 slice was carved
# at this size and migration 039 derives a gen-1 row's data-disk size from it,
# so it must not change while gen-1 slices exist.
SLICE_BOOT_DISK_GIB: Final[int] = 32
# Gen-2 boot disk: only the guest OS, its logs (journald capped at 512 MiB) and
# whatever we install at the box level -- docker's data-root lives on the data
# disk (see GEN2_GUEST_DOCKER_DATA_ROOT), so everything an agent host can grow is
# on the one disk a resize can grow. ~1.5GiB used at bake; the rest is headroom.
GEN2_BOOT_DISK_GIB: Final[int] = 10

# Fair-share bandwidth shaping runs the box's HTB root class slightly below the
# declared uplink so the shaper, not the NIC's transmit queue, is where packets
# queue; only then do the per-machine classes actually arbitrate. The stored
# ``uplink_mbps`` stays nominal (the link-speed audit and the egress signal
# compare against the real link rate); the percentage is applied only where the
# slice helper renders the classes, with integer arithmetic floored at 1 mbit.
GEN2_UPLINK_SHAPING_PERCENT: Final[int] = 95


# Variable machine sizing (specs/slice-fleet): a "unit" is 1GiB of guest RAM
# and is the single sizing knob for a gen-2 machine -- it drives RAM
# (units GiB), vCPUs (proportional share of the box's threads), and
# fair-share bandwidth (proportional HTB guarantee). Disk is a second,
# separate grow-only factor, sized once at carve from the unit count.

# A gen-2 machine's data disk = a fixed base + a per-unit share (8 units -> 44GiB).
# The base covers what is not the user's: the host's docker image as it sits on
# disk (~13GiB: containerd's overlayfs snapshots plus its compressed content
# blobs) plus DATA_DISK_SYSTEM_RESERVE_GIB, the slack the guest keeps OUTSIDE the
# host quota for the engines' own metadata, docker's (GC-bounded) build cache,
# the newest backup snapshot's delta and btrfs metadata -- so a host that fills
# its quota never stops the engines or the snapshot helper. Everything the host
# writes (its home subvolume, and the image + container layers and content
# blobs in containerd's root) shares ONE qgroup limited to ``disk - reserve``;
# the guest's grow oneshot re-derives that limit from the filesystem size every
# boot, so a resize grows it. btrfs *simple* quotas: extents are charged to the
# subvolume that created them, so the hourly backup snapshots neither double
# count nor (unlike classic qgroups) leave the accounting inconsistent when
# deleted. After the carve, disk is decoupled from units: resizing units never
# changes disk, and disk only ever grows.
DATA_DISK_GIB_PER_UNIT: Final[float] = 3.5
DATA_DISK_BASE_GIB: Final[int] = 16
DATA_DISK_SYSTEM_RESERVE_GIB: Final[int] = 4

# The size every pool bake carves (and every create leases): the pool stays
# uniform; other sizes are reached by resize-then-restart.
DEFAULT_MACHINE_UNITS: Final[int] = 8

# The allowed-size set at launch: any multiple of the step in [step, max].
# The architectural floor is lower (2 units someday), so nothing outside the
# wire/CLI validation may assume the step -- internal code treats units as a
# plain positive int.
MACHINE_UNITS_STEP: Final[int] = 8
MAX_MACHINE_UNITS: Final[int] = 128

# RAM (MiB) held back from the agent host container's hard cap so the slice VM's
# own daemons (dockerd/containerd ~200MiB, sshd/systemd/journald, uncharged
# kernel slab, plus a little file cache) always have room. Without a cap, a
# container at memory capacity collapses the VM-wide page cache and wedges the
# VM's sshd -- making the slice unreachable AND unrecoverable (a live incident,
# not a hypothesis). The reserve is a fixed delta, not a fraction: the VM-side
# footprint does not scale with slice size. Measured steady state is ~470-530MiB;
# 1024 leaves headroom for dockerd build/load spikes.
SLICE_CONTAINER_MEMORY_RESERVE_MIB: Final[int] = 1024


@pure
def is_allowed_machine_units(units: int) -> bool:
    """Whether ``units`` is in the launch allowed-size set (a multiple of the step in [step, max])."""
    return MACHINE_UNITS_STEP <= units <= MAX_MACHINE_UNITS and units % MACHINE_UNITS_STEP == 0


@pure
def compute_box_total_units(ram_gb: int) -> int:
    """The box's sellable unit budget: its RAM minus the host reserve, in 1GiB units.

    The budget is consumed as ``units x 1024 + PER_VM_RAM_OVERHEAD_MIB`` MiB per
    machine (see :func:`compute_box_unit_budget_mib`), so the uniform 128GB box
    still yields exactly its historical 14 default-size machines.
    """
    total_units = ram_gb - HOST_RAM_RESERVE_GIB
    if total_units <= 0:
        raise InvalidMachineSizeError(
            f"ram_gb={ram_gb} leaves no sellable units after the {HOST_RAM_RESERVE_GIB}GiB host reserve"
        )
    return total_units


@pure
def compute_box_unit_budget_mib(ram_gb: int) -> int:
    """The box's memory budget in MiB, against which each machine consumes ``units x 1024 + overhead``."""
    return compute_box_total_units(ram_gb) * 1024


@pure
def compute_machine_memory_footprint_mib(units: int) -> int:
    """What one machine consumes from the box's memory budget: its guest RAM plus the per-VM host overhead."""
    if units <= 0:
        raise InvalidMachineSizeError(f"units must be positive, got {units}")
    return units * 1024 + PER_VM_RAM_OVERHEAD_MIB


@pure
def compute_machine_vcpus(cpu_threads: int, overcommit_ratio: float, units: int, box_total_units: int) -> int:
    """A machine's vCPU count: its proportional share of the box's (overcommitted) threads.

    Floored at 1 and capped at the box's real thread count (a huge machine on a
    small box must not advertise threads the hardware does not have).
    """
    if cpu_threads <= 0:
        raise InvalidMachineSizeError(f"cpu_threads must be positive, got {cpu_threads}")
    if overcommit_ratio <= 0:
        raise InvalidMachineSizeError(f"overcommit_ratio must be positive, got {overcommit_ratio}")
    if units <= 0:
        raise InvalidMachineSizeError(f"units must be positive, got {units}")
    if box_total_units <= 0:
        raise InvalidMachineSizeError(f"box_total_units must be positive, got {box_total_units}")
    return min(cpu_threads, max(1, math.floor(cpu_threads * overcommit_ratio * units / box_total_units)))


@pure
def compute_machine_guest_memory_mib(units: int) -> int:
    """The RAM a machine's guest boots with: its units minus the per-machine holdback (qemu `-m`)."""
    if units <= 0:
        raise InvalidMachineSizeError(f"units must be positive, got {units}")
    return units * 1024 - GUEST_RAM_HOLDBACK_MIB


@pure
def compute_machine_data_disk_gib(units: int) -> int:
    """The data-disk GiB a machine is granted at carve time (grow-only afterwards): base + per-unit share."""
    if units <= 0:
        raise InvalidMachineSizeError(f"units must be positive, got {units}")
    return DATA_DISK_BASE_GIB + math.ceil(units * DATA_DISK_GIB_PER_UNIT)


@pure
def compute_gen1_migrated_data_disk_gib(gen1_data_disk_gib: int) -> int:
    """The data-disk GiB a gen-1 machine has once the cutover moves it to gen-2.

    A gen-1 data disk holds only the home volume (docker lives on the gen-1
    boot disk); the gen-2 data disk also holds the container engines' roots
    and the system reserve, which is exactly what ``DATA_DISK_BASE_GIB`` was
    sized for. The cutover therefore grows the transplanted disk by the base,
    so the machine keeps its home capacity -- and a gen-1 row records this
    number as its ``disk_gb`` from the start.
    """
    if gen1_data_disk_gib <= 0:
        raise InvalidMachineSizeError(f"gen1_data_disk_gib must be positive, got {gen1_data_disk_gib}")
    return gen1_data_disk_gib + DATA_DISK_BASE_GIB


@pure
def compute_default_machine_capacity(ram_gb: int) -> int:
    """How many default-size machines the box's memory budget holds."""
    return compute_box_unit_budget_mib(ram_gb) // compute_machine_memory_footprint_mib(DEFAULT_MACHINE_UNITS)


@pure
def compute_gen2_disk_budget_gib(storage_partition_gib: int) -> int:
    """The gen-2 box's disk budget in GiB: its measured storage partition minus the named reserve.

    Each machine consumes ``GEN2_BOOT_DISK_GIB + its data-disk GiB`` from it
    (the two-budget accounting's disk half). ``storage_partition_gib`` is the
    box row's ``disk_gb``, which the gen-2 prep records from the mounted XFS
    partition.
    """
    budget_gib = storage_partition_gib - GEN2_STORAGE_RESERVE_GIB
    if budget_gib <= 0:
        raise InvalidMachineSizeError(
            f"storage_partition_gib={storage_partition_gib} leaves no disk budget after the "
            f"{GEN2_STORAGE_RESERVE_GIB}GiB reserve"
        )
    return budget_gib


@pure
def compute_gen2_storage_partition_estimate_gib(usable_disk_gb: int) -> int:
    """Estimate a not-yet-delivered gen-2 box's storage partition from the catalog's usable-disk figure.

    The pre-delivery ordering and registration guards have only the catalog
    number; the reinstall carves the root and boot partitions out of it and
    XFS fills the rest. Once the box is prepped, the measured partition
    (recorded as the row's ``disk_gb``) replaces this estimate everywhere.
    """
    estimate_gib = usable_disk_gb - GEN2_ROOT_PARTITION_GIB - GEN2_BOOT_PARTITION_GIB
    if estimate_gib <= 0:
        raise InvalidMachineSizeError(
            f"usable_disk_gb={usable_disk_gb} cannot hold the {GEN2_ROOT_PARTITION_GIB}GiB root and "
            f"{GEN2_BOOT_PARTITION_GIB}GiB boot partitions"
        )
    return estimate_gib
