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
from imbue.mngr_imbue_cloud.primitives import tier_for_env_name

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
# (the DEFAULT_WORKSPACE_TEMPLATE image + build cache + container layers -- ~11GiB observed, sized with
# headroom for build spikes) and a btrfs data disk (the rest of the budget) mounted
# at the host_dir for the agent's per-host volume. lima would otherwise default the
# boot disk to 100GiB, which (unaccounted) would massively overcommit the box.
SLICE_BOOT_DISK_GIB: Final[int] = 32

# Default RAM (GB) each slice advertises / is sized to. A box's slot count is
# floor(total_RAM / this), so it also sets how many slices a box yields. Used as the
# default for the pricing table and the natural slice size for our workspaces.
DEFAULT_MEMORY_PER_SLICE_GB: Final[int] = 8

# RAM (MiB) held back from the workspace container's hard cap so the slice VM's own
# daemons (dockerd/containerd ~200MiB, lima-guestagent, sshd/systemd/journald,
# uncharged kernel slab, plus a little file cache) always have room. Without a cap,
# a workspace at memory capacity collapses the VM-wide page cache and wedges the
# VM's sshd -- making the slice unreachable AND unrecoverable (a live incident, not
# a hypothesis). The reserve is a fixed delta, not a fraction: the VM-side
# footprint does not scale with slice size. Measured steady state is ~470-530MiB;
# 1024 leaves headroom for dockerd build/load spikes. Note ~244MiB of the lima
# memory size is already consumed by boot-time kernel/firmware reservation, so the
# guest-visible room left for VM daemons is roughly this reserve minus that.
SLICE_CONTAINER_MEMORY_RESERVE_MIB: Final[int] = 1024

# Default CPU overcommit factor used to size each slice's vCPUs (vCPUs/slice =
# floor(threads * ratio / slots)). Overridable per box at ``minds-admin server
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

# Box dir holding the per-box cached DEFAULT_WORKSPACE_TEMPLATE image tar (a ``docker save`` of the built
# image), so slices on the box ``docker load`` it instead of each rebuilding from
# the Dockerfile. Under the lima service user's home (the box has no Docker, only a
# tar file); created once at ``server prep``. Shared by the prep script and the box
# image cache so they always agree.
_SLICE_DEFAULT_WORKSPACE_TEMPLATE_CACHE_RELDIR: Final[str] = ".cache/mngr-slice-default-workspace-template"


def slice_base_image_path(lima_service_user: str) -> str:
    """Absolute path of the box-staged slice guest OS image for ``lima_service_user``."""
    return f"/home/{lima_service_user}/{_SLICE_BASE_IMAGE_RELPATH}"


def box_default_workspace_template_cache_dir(lima_service_user: str) -> str:
    """Absolute path of the box dir holding the cached DEFAULT_WORKSPACE_TEMPLATE image tar for ``lima_service_user``."""
    return f"/home/{lima_service_user}/{_SLICE_DEFAULT_WORKSPACE_TEMPLATE_CACHE_RELDIR}"


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
def compute_slice_container_memory_cap_mib(slice_memory_mib: int) -> int:
    """The workspace container's hard memory cap: the slice VM's RAM minus the VM-side reserve."""
    cap_mib = slice_memory_mib - SLICE_CONTAINER_MEMORY_RESERVE_MIB
    if cap_mib <= 0:
        raise BareMetalConfigError(
            f"slice_memory_mib={slice_memory_mib} leaves no container memory after the "
            f"{SLICE_CONTAINER_MEMORY_RESERVE_MIB}MiB VM reserve"
        )
    return cap_mib


@pure
def build_slice_container_memory_start_args(slice_memory_mib: int) -> tuple[str, ...]:
    """The ``docker run`` args that hard-cap the workspace container's memory.

    ``--memory-swap`` equals ``--memory`` (memcg ``swap.max=0``) so the container can
    never swap: under pressure it is shed fast (earlyoom, then the cgroup OOM killer,
    both steered by the workspace's ``oom_score_adj`` bands) instead of thrashing.
    """
    cap_mib = compute_slice_container_memory_cap_mib(slice_memory_mib)
    return (f"--memory={cap_mib}m", f"--memory-swap={cap_mib}m")


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

# How much of the host id's 32-char uuid hex is embedded in slice lima names.
# Truncated (not the full hex) because the name budget is tight -- see
# MAX_SLICE_INSTANCE_NAME_LENGTH below -- and 16 hex chars (64 bits) is far
# beyond collision range for the <=14 slices a box holds. Slices baked before
# the truncation carry the full 32 hex; the owner parse accepts both.
SLICE_HOST_ID_HEX_LENGTH: Final[int] = 16

# Two limactl limits bound a slice's lima names; both derivations live here so
# the fail-fast guard below can never drift from what limactl enforces:
#
# 1. The ssh control socket path must fit a unix socket address: limactl
#    validates ``<lima-home>/<instance>/ssh.sock.<16-digit-suffix>`` against
#    UNIX_PATH_MAX (108, "must be less than"), reserving 16 digits for the
#    suffix. With the fleet's standard lima home (``/home/limahost/.lima/``)
#    that caps the INSTANCE name at 60 chars -- the binding constraint.
# 2. Any instance/disk identifier must be at most 76 chars (its ``identifier
#    greater than maximum length`` fatal); the data disk (instance + "-data")
#    is the longest, and at instance <= 60 it is 65 -- never binding, kept in
#    the derivation as a min() so a future re-balance cannot silently break it.
_UNIX_PATH_MAX: Final[int] = 108
_LIMA_SSH_SOCK_RESERVED_SUFFIX_LENGTH: Final[int] = len("/ssh.sock.") + 16
_STANDARD_LIMA_HOME_PREFIX: Final[str] = "/home/limahost/.lima/"
_LIMA_MAX_IDENTIFIER_LENGTH: Final[int] = 76
MAX_SLICE_INSTANCE_NAME_LENGTH: Final[int] = min(
    _UNIX_PATH_MAX - 1 - len(_STANDARD_LIMA_HOME_PREFIX) - _LIMA_SSH_SOCK_RESERVED_SUFFIX_LENGTH,
    _LIMA_MAX_IDENTIFIER_LENGTH - len(SLICE_LIMA_DISK_SUFFIX),
)
# The extra 1 is the "-" between the env stamp and the host id hex.
MAX_SLICE_ENV_NAME_LENGTH: Final[int] = (
    MAX_SLICE_INSTANCE_NAME_LENGTH - len(SLICE_LIMA_INSTANCE_PREFIX) - 1 - SLICE_HOST_ID_HEX_LENGTH
)


@pure
def assert_env_name_fits_slice_names(env_name: str) -> None:
    """Raise ``SliceCapacityError`` when ``env_name`` is too long to stamp into slice lima names.

    Checked before anything is carved: limactl only rejects the over-long name
    at reserve time, deep inside the bake, with a message that says nothing
    about the env name being the variable part. CI env names
    (``ci-<timestamp>-<short>``) sit near the cap, which is how this was found.
    """
    if len(env_name) > MAX_SLICE_ENV_NAME_LENGTH:
        raise SliceCapacityError(
            f"env name {env_name!r} is {len(env_name)} chars; at most {MAX_SLICE_ENV_NAME_LENGTH} fit into a "
            f"slice's lima instance name (limactl caps the instance name at {MAX_SLICE_INSTANCE_NAME_LENGTH} "
            "chars -- its ssh socket path must fit UNIX_PATH_MAX). Use a shorter env name."
        )


# A slice's host id stamp is uuid hex with no hyphens: SLICE_HOST_ID_HEX_LENGTH
# chars on current slices, the full 32 on slices baked before truncation. Tried
# longest-first so a (wildly implausible) legacy env ending in "-<16 hex>"
# still parses as the legacy 32-hex shape rather than donating hex to its env.
_STAMPED_SLICE_CORE_RES: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^(?P<env>.+)-(?P<host>[0-9a-f]{32})$"),
    re.compile(rf"^(?P<env>.+)-(?P<host>[0-9a-f]{{{SLICE_HOST_ID_HEX_LENGTH}}})$"),
)


@pure
def slice_lima_instance_name(host_id: HostId, env_name: str | None = None) -> str:
    """Deterministic lima instance name for a slice, embedding the mngr host id.

    When ``env_name`` is given the owning env is stamped in
    (``mngr-slice-<env>-<host-hex>``) so the box can attribute the slice to an
    environment and reconciliation can scope itself to one env's slices. Without it
    the legacy un-stamped name (``mngr-slice-<host-hex>``) is produced, for
    backwards compatibility with slices baked before env stamping. The host hex is
    truncated (see :data:`SLICE_HOST_ID_HEX_LENGTH`) so long env names fit
    limactl's instance-name budget; existing slices keep their stored full-hex
    names (teardown always reads the recorded name, never re-derives it).
    """
    host_hex = host_id.get_uuid().hex[:SLICE_HOST_ID_HEX_LENGTH]
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
    hex is a hyphen-free uuid (truncated on current slices, full 32 on older
    ones), so the env is everything between the prefix and the trailing
    ``-<host-hex>``.
    """
    core = _slice_resource_core(name)
    if core is None:
        return None
    for pattern in _STAMPED_SLICE_CORE_RES:
        match = pattern.match(core)
        if match is not None:
            return match.group("env")
    return None


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
def find_first_ready_server_in_datacenter(
    servers: Sequence[BareMetalServer], datacenter: str
) -> BareMetalServer | None:
    """The first ready server in the given OVH datacenter, or None when the datacenter has none.

    The deterministic CI box selection shared by the cache pre-warm job and the
    bake stage (specs/remote-workspaces-in-ci.md): given the same server rows
    (``fetch_servers`` orders by created_at ASC) and the same datacenter, both
    steps pick the same box, so the warm job's tar lands on the box the bake
    will use. One box per datacenter today; if several exist the first ready one
    wins -- the bake's on-box occupancy check is what actually guards capacity.
    """
    for server in servers:
        if str(server.status) == SERVER_STATUS_READY and server.region == datacenter:
            return server
    return None


# /proc/mdstat structure: an array header line (``md3 : active raid1 ...``)
# followed by a status line whose ``[expected/active]`` bracket reports member
# counts (``... blocks super 1.2 [2/1] [_U]``). Fewer active members than
# expected means the array is degraded (a member disk has failed or dropped).
_MD_ARRAY_HEADER_RE: Final[re.Pattern[str]] = re.compile(r"^(md\d+)\s*:")
_MD_MEMBER_COUNTS_RE: Final[re.Pattern[str]] = re.compile(r"\[(\d+)/(\d+)\]")


@pure
def parse_degraded_md_arrays(mdstat_text: str) -> list[str]:
    """The md arrays in a ``/proc/mdstat`` dump that are running with a failed member.

    A degraded array still serves reads/writes from its surviving mirror, so
    nothing else on the box makes the failure visible -- this is how a slice box
    runs for days on one disk (the 2026-08-07 production incident) unless
    something reads mdstat and reports it.
    """
    degraded: list[str] = []
    current_array: str | None = None
    for line in mdstat_text.splitlines():
        header_match = _MD_ARRAY_HEADER_RE.match(line)
        if header_match:
            current_array = header_match.group(1)
            continue
        counts_match = _MD_MEMBER_COUNTS_RE.search(line)
        if counts_match and current_array is not None:
            expected_members, active_members = int(counts_match.group(1)), int(counts_match.group(2))
            if active_members < expected_members:
                degraded.append(current_array)
            current_array = None
    return degraded


@pure
def parse_raw_swap_devices(proc_swaps_text: str) -> list[str]:
    """The swap devices in a ``/proc/swaps`` dump that are raw (non-md) partitions.

    Swap on a raw partition sits outside the box's RAID mirrors: when that disk
    dies its swapped-out pages are permanently lost and every process touching
    one gets SIGBUS -- the mechanism that killed the slices in the 2026-08-07
    production incident. All swap belongs on the mirrored filesystem (the prep
    swapfile); a partition entry here means the box needs a prep re-run. Swap on
    an md device would itself be mirrored, so ``/dev/md*`` entries are not
    flagged.
    """
    raw_devices: list[str] = []
    # The first line is the fixed "Filename Type Size ..." header.
    for line in proc_swaps_text.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "partition" and not fields[0].startswith("/dev/md"):
            raw_devices.append(fields[0])
    return raw_devices


@pure
def count_authorized_key_lines(authorized_keys_text: str) -> int:
    """Number of public keys an ``authorized_keys`` file authorizes.

    Blank lines and ``#`` comments carry no key, so they do not count;
    everything else is one authorized key. A correctly prepped box yields exactly
    :data:`EXPECTED_AUTHORIZED_KEY_COUNT` -- ``build_box_prep_script`` writes the
    file with a single-key overwrite (``cat >``, never an append) -- so any other
    count means a key was added out of band, which is how a box ends up reachable
    by a tier that does not own it.
    """
    return sum(1 for line in authorized_keys_text.splitlines() if line.strip() and not line.strip().startswith("#"))


@pure
def foreign_tier_slice_names(box_names: AbstractSet[str], env_name: str) -> set[str]:
    """Slice resources on the box whose env stamp belongs to a DIFFERENT tier than ``env_name``.

    Box sharing is legitimate *within* a tier -- several ``dev-<user>`` envs
    routinely carve slices on one dev box, which is why occupancy is read from the
    box rather than from one env's rows. It is never legitimate *across* tiers,
    and the reason is the pool keypair, not the database: a box carrying two
    tiers' slices is a box both tiers' pool keys can SSH, which is precisely the
    "zero cross-tier reach" boundary (see ``apps/minds/docs/deploy/reference/environments.md``) --
    each tier's operators and connector gain limactl, and so root, over the
    other's workspaces. Separate ``host_pool`` databases do not distinguish the
    two cases: every dev env has its own database too, and the orphan reap is
    scoped by env rather than by tier either way.

    Legacy un-stamped slices (``mngr-slice-<host-hex>``, no env) have no knowable
    tier, so they are excluded here: they still count toward occupancy, but a name
    that predates env stamping is not evidence of a cross-tier bake.
    """
    expected_tier = tier_for_env_name(env_name)
    return {
        name
        for name in box_names
        if (owner := slice_name_env_owner(name)) is not None and tier_for_env_name(owner) != expected_tier
    }


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
        f"no bare-metal server with id {server_id}; run the operator CLI's `minds-admin server list` to see the fleet"
    )
