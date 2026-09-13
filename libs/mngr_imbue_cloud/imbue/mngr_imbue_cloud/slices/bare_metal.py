import math
import re
from collections.abc import Mapping
from collections.abc import Sequence
from typing import AbstractSet
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.mngr.primitives import HostId
from imbue.mngr_imbue_cloud.data_types import BareMetalServer
from imbue.mngr_imbue_cloud.data_types import BareMetalServerCapacity
from imbue.mngr_imbue_cloud.data_types import BoxManagementTrust
from imbue.mngr_imbue_cloud.data_types import Gen2BoxDefaultMachineFit
from imbue.mngr_imbue_cloud.data_types import StorageVolumeState
from imbue.mngr_imbue_cloud.errors import BareMetalConfigError
from imbue.mngr_imbue_cloud.errors import SliceCapacityError
from imbue.mngr_imbue_cloud.primitives import BareMetalServerDbId
from imbue.mngr_imbue_cloud.primitives import BareMetalServerStatus
from imbue.mngr_imbue_cloud.primitives import GEN1_EXPECTED_AUTHORIZED_KEY_COUNT
from imbue.mngr_imbue_cloud.primitives import GEN2_EXPECTED_AUTHORIZED_KEY_COUNT
from imbue.mngr_imbue_cloud.primitives import OVH_DATACENTER_CODE_BY_US_REGION
from imbue.mngr_imbue_cloud.primitives import OVH_US_DATACENTER_CODES
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_DELIVERED
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_FAILED
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_INSTALLING
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_ORDERED
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_READY
from imbue.mngr_imbue_cloud.primitives import SliceContainerRuntime
from imbue.mngr_imbue_cloud.primitives import US_REGION_BY_OVH_DATACENTER_CODE
from imbue.mngr_imbue_cloud.primitives import tier_for_env_name
from imbue.mngr_imbue_cloud.slices.gen2_scripts.box_commands import SliceInstanceObservation
from imbue.mngr_imbue_cloud.slices.gen2_scripts.errors import InvalidMachineSizeError
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import FIRST_QEMU_BOX_GENERATION
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_IMAGE_TAR_CACHE_DIR
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_SLICE_SERVICE_USER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_STORAGE_LUKS_MAPPER_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_STORAGE_ROOT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import DEFAULT_MACHINE_UNITS
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import DISK_RESERVE_FRACTION
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import DISK_RESERVE_GB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import GEN2_BOOT_DISK_GIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import HOST_RAM_RESERVE_GIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import PER_VM_RAM_OVERHEAD_MIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import SLICE_BOOT_DISK_GIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import SLICE_CONTAINER_MEMORY_RESERVE_MIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_default_machine_capacity
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_gen2_disk_budget_gib
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_gen2_storage_partition_estimate_gib
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_machine_data_disk_gib
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import SSH_CA_PUBLIC_KEY_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import is_same_ssh_public_key

# The sizing model lives in ``slices.gen2_scripts.sizing`` so the
# remote_service_connector can ship it.

# Gen-2 agent host containers run under gVisor from the bake (a property of the
# fleet, like trixie), with /run and /tmp as tmpfs: runsc is registered with
# --overlay2=none, which leaves the container rootfs on gVisor's gofer-backed
# filesystem, and that filesystem refuses the hard link supervisord installs its
# control socket with (it would wedge on "Unlinking stale socket" forever).
# Ephemeral dirs on tmpfs also never ride a backup. Applied by the bake's per-box
# `-S providers.imbue_cloud_slice.*` overrides and the slow-path rebuild alike.
GEN2_CONTAINER_RUNTIME: Final[SliceContainerRuntime] = SliceContainerRuntime.RUNSC
GEN2_CONTAINER_TMPFS_START_ARGS: Final[tuple[str, ...]] = ("--tmpfs", "/run", "--tmpfs", "/tmp")


@pure
def docker_runtime_name(runtime: SliceContainerRuntime) -> str:
    """The lowercase name Docker knows the runtime by (``--runtime runsc``)."""
    return runtime.value.lower()


# Default RAM (GB) each slice advertises / is sized to. A box's slot count is
# floor(total_RAM / this), so it also sets how many slices a box yields. Used as the
# default for the pricing table and the natural slice size for our agent hosts.
DEFAULT_MEMORY_PER_SLICE_GB: Final[int] = 8


@pure
def _gen2_default_machine_disk_budgets(*, ram_gb: int, disk_gb: int) -> tuple[int, int, int]:
    """(default-machine capacity, the disk budget those machines need, the box's estimated disk budget).

    ``disk_gb`` is the catalog's usable-disk figure (the guard runs before the
    box exists), so the storage partition it will carry is estimated from it.
    """
    machine_capacity = compute_default_machine_capacity(ram_gb)
    required_disk_budget_gib = machine_capacity * (
        GEN2_BOOT_DISK_GIB + compute_machine_data_disk_gib(DEFAULT_MACHINE_UNITS)
    )
    disk_budget_gib = compute_gen2_disk_budget_gib(compute_gen2_storage_partition_estimate_gib(disk_gb))
    return machine_capacity, required_disk_budget_gib, disk_budget_gib


@pure
def assert_region_label_matches_box_datacenter(*, region_label: str, box_datacenter: str) -> None:
    """Refuse a bake whose lease-region label names a different datacenter than the box it targets.

    The label is what the connector's region-filtered lease and restore match
    against, so a row stamped with the wrong region is permanently unleasable
    from where its box actually is. A box whose datacenter the pairing does
    not know is refused too: no label could ever match it.
    """
    expected_datacenter = OVH_DATACENTER_CODE_BY_US_REGION.get(region_label)
    if expected_datacenter is None:
        raise BareMetalConfigError(
            f"region label {region_label!r} is not a known lease region; expected one of "
            f"{sorted(OVH_DATACENTER_CODE_BY_US_REGION)}"
        )
    if box_datacenter not in OVH_US_DATACENTER_CODES:
        raise BareMetalConfigError(
            f"the box's datacenter {box_datacenter!r} is not in the region map "
            f"{dict(OVH_DATACENTER_CODE_BY_US_REGION)}; fix the bare_metal_servers row before baking on it"
        )
    if box_datacenter != expected_datacenter:
        raise BareMetalConfigError(
            f"region label {region_label!r} maps to datacenter {expected_datacenter!r}, but the box is in "
            f"{box_datacenter!r}; bake it with --region {US_REGION_BY_OVH_DATACENTER_CODE[box_datacenter]}"
        )


@pure
def compute_gen2_box_default_machine_fit(*, ram_gb: int, disk_gb: int) -> Gen2BoxDefaultMachineFit:
    """How ``disk_gb`` (the catalog's usable-disk GB) fits the default machines ``ram_gb`` sells.

    The figure is compared as the GiB storage partition it is estimated to
    yield, minus the fixed storage reserve; the gen-2 prep later measures the
    real partition and records that as the box's ``disk_gb``.
    """
    try:
        machine_capacity, required_disk_budget_gib, disk_budget_gib = _gen2_default_machine_disk_budgets(
            ram_gb=ram_gb, disk_gb=disk_gb
        )
    except InvalidMachineSizeError as e:
        raise BareMetalConfigError(str(e)) from e
    return Gen2BoxDefaultMachineFit(
        machine_capacity=machine_capacity,
        required_disk_budget_gib=required_disk_budget_gib,
        disk_budget_gib=disk_budget_gib,
    )


@pure
def describe_gen2_box_disk_shortfall(fit: Gen2BoxDefaultMachineFit, *, ram_gb: int, disk_gb: int) -> str:
    return (
        f"disk_gb={disk_gb} (usable GB, compared as an estimated {fit.disk_budget_gib}GiB storage budget after the "
        f"reserve) is below the {fit.required_disk_budget_gib}GiB the box's {fit.machine_capacity} default-size "
        f"machine(s) ({ram_gb}GB RAM) need; it holds {fit.machines_that_fit} of them"
    )


@pure
def assert_gen2_box_disk_fits_default_machines(*, ram_gb: int, disk_gb: int) -> None:
    """Refuse a gen-2 box config whose disk cannot hold its RAM's full complement of default machines.

    The ordering guard of specs/slice-fleet: a box whose memory budget yields
    N default machines must have a disk budget of at least ``N x (boot +
    default data disk)``, or the RAM the box was priced for can never be sold
    (there is deliberately no storage add-on ordering -- the config is
    refused instead). Registration of a box that already exists only warns
    (see ``minds-admin server register``): the gen-2 prep re-measures its
    ``disk_gb`` from the storage partition, and the two-budget accounting
    fits fewer machines on it.
    """
    fit = compute_gen2_box_default_machine_fit(ram_gb=ram_gb, disk_gb=disk_gb)
    if not fit.is_sufficient:
        raise BareMetalConfigError(
            describe_gen2_box_disk_shortfall(fit, ram_gb=ram_gb, disk_gb=disk_gb)
            + "; pick a larger storage config (specs/slice-fleet: insufficient configs are refused, not ordered "
            "with add-ons)"
        )


# Default CPU overcommit factor used to size each slice's vCPUs (vCPUs/slice =
# floor(threads * ratio / slots); per machine, its proportional unit share of
# ``threads * ratio``). 4.0 -- every thread sold four times -- gives an 8-unit
# machine on a 16-thread/120-unit box 4 vCPUs (and stays at 4 on a slightly
# larger box, where a ratio tuned to the exact boundary would floor to 3): under gVisor's systrap platform every syscall hands off
# between the application thread and a sentry thread, so a 2-vCPU machine
# spends much of its time context-switching between the two; 4 vCPUs measured
# 1.5-3x faster on interpreter startup and dependency installs
# (``blueprint/slice-fleet-cutover/benchmark-summary-2026-08-27-tuning.md``).
# The real CPU share is still governed by the unit's proportional
# ``CPUWeight``, so the overcommit only relaxes a per-VM bound. Overridable per
# box at ``minds-admin server register --cpu-overcommit``; RAM is never
# overcommitted.
DEFAULT_SLICE_CPU_OVERCOMMIT_RATIO: Final[float] = 4.0

# The gen-1 box's dedicated non-root service user, which owns the lima VMs and
# drives limactl. Gen-2 boxes use ``GEN2_SLICE_SERVICE_USER`` instead; a box's
# row records which user it runs, and this is only the fallback for a gen-1 row
# without one.
# CLEANUP: delete with the rest of the gen-1 lima code in phase 6 of
# blueprint/slice-fleet-cutover.
GEN1_SLICE_SERVICE_USER: Final[str] = "limahost"


@pure
def default_slice_service_user(box_generation: int) -> str:
    """The service user a box of ``box_generation`` runs when its row records none."""
    if box_generation >= FIRST_QEMU_BOX_GENERATION:
        return GEN2_SLICE_SERVICE_USER
    return GEN1_SLICE_SERVICE_USER


@pure
def box_service_user(server: BareMetalServer) -> str:
    """The unix user box commands SSH as: the row's recorded service user, else its generation's default."""
    return server.slice_service_user or default_slice_service_user(server.box_generation)


# The slice guest OS image is staged once on each box (at prep) and referenced by
# the slice bake via ``file://`` so VM boots never depend on the Debian mirror
# (lima otherwise does a per-boot last-modified HEAD to cloud.debian.org for a
# digest-less image, which fatally fails when the mirror is flaky). Stored under
# the gen-1 service user's home so prep can write it without root, and read by
# limactl (which runs as that user). Path is shared by the prep script and the
# slice provider so they always agree.
_SLICE_BASE_IMAGE_RELPATH: Final[str] = ".cache/mngr-slice-base/debian-base.qcow2"

# Box dir holding the per-box cached DEFAULT_WORKSPACE_TEMPLATE image tar (a ``docker save`` of the built
# image), so slices on the box ``docker load`` it instead of each rebuilding from
# the Dockerfile. Under the gen-1 service user's home (the box has no Docker, only a
# tar file); created once at ``server prep``. Shared by the prep script and the box
# image cache so they always agree.
_SLICE_DEFAULT_WORKSPACE_TEMPLATE_CACHE_RELDIR: Final[str] = ".cache/mngr-slice-default-workspace-template"


def slice_base_image_path(slice_service_user: str) -> str:
    """Absolute path of the box-staged slice guest OS image for ``slice_service_user``."""
    return f"/home/{slice_service_user}/{_SLICE_BASE_IMAGE_RELPATH}"


def box_default_workspace_template_cache_dir(slice_service_user: str) -> str:
    """Absolute path of the gen-1 box dir holding the cached DEFAULT_WORKSPACE_TEMPLATE image tar for ``slice_service_user``."""
    return f"/home/{slice_service_user}/{_SLICE_DEFAULT_WORKSPACE_TEMPLATE_CACHE_RELDIR}"


@pure
def box_image_cache_dir_for_generation(box_generation: int, slice_service_user: str) -> str:
    """The box dir holding the cached image tar(s): on the storage partition for gen-2, the service user's home for gen-1.

    Gen-2 boxes keep the multi-GiB tars off their small root partition (the
    reserve in ``gen2_scripts.sizing`` budgets the cache on the storage
    partition); gen-1 boxes keep the historical home-dir location.
    """
    if box_generation >= FIRST_QEMU_BOX_GENERATION:
        return GEN2_IMAGE_TAR_CACHE_DIR
    return box_default_workspace_template_cache_dir(slice_service_user)


def slice_base_image_file_url(slice_service_user: str) -> str:
    """``file://`` URL the slice lima YAML uses for the box-staged guest OS image."""
    return f"file://{slice_base_image_path(slice_service_user)}"


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


# Instance-name prefix for slices (both generations). Used both to derive a
# slice's deterministic instance name and to recognize slice VMs on the box, so
# reconciliation never touches a non-slice VM.
SLICE_INSTANCE_PREFIX: Final[str] = "mngr-slice-"

# Suffix appended to a slice's instance name to name its data disk (both
# generations; ``gen2_scripts.layout.GEN2_DISK_SUFFIX`` is the connector-mounted copy).
SLICE_DISK_SUFFIX: Final[str] = "-data"

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
#    suffix. With the fleet's standard lima home (``/home/<gen-1 service user>/.lima/``)
#    that caps the INSTANCE name at 60 chars -- the binding constraint.
# 2. Any instance/disk identifier must be at most 76 chars (its ``identifier
#    greater than maximum length`` fatal); the data disk (instance + "-data")
#    is the longest, and at instance <= 60 it is 65 -- never binding, kept in
#    the derivation as a min() so a future re-balance cannot silently break it.
_UNIX_PATH_MAX: Final[int] = 108
_LIMA_SSH_SOCK_RESERVED_SUFFIX_LENGTH: Final[int] = len("/ssh.sock.") + 16
_STANDARD_LIMA_HOME_PREFIX: Final[str] = f"/home/{GEN1_SLICE_SERVICE_USER}/.lima/"
_LIMA_MAX_IDENTIFIER_LENGTH: Final[int] = 76
MAX_SLICE_INSTANCE_NAME_LENGTH: Final[int] = min(
    _UNIX_PATH_MAX - 1 - len(_STANDARD_LIMA_HOME_PREFIX) - _LIMA_SSH_SOCK_RESERVED_SUFFIX_LENGTH,
    _LIMA_MAX_IDENTIFIER_LENGTH - len(SLICE_DISK_SUFFIX),
)
# The extra 1 is the "-" between the env stamp and the host id hex.
MAX_SLICE_ENV_NAME_LENGTH: Final[int] = (
    MAX_SLICE_INSTANCE_NAME_LENGTH - len(SLICE_INSTANCE_PREFIX) - 1 - SLICE_HOST_ID_HEX_LENGTH
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
def slice_instance_name(host_id: HostId, env_name: str | None = None) -> str:
    """Deterministic VM instance name for a slice (both generations), embedding the mngr host id.

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
        return f"{SLICE_INSTANCE_PREFIX}{host_hex}"
    return f"{SLICE_INSTANCE_PREFIX}{env_name}-{host_hex}"


@pure
def slice_disk_name(host_id: HostId, env_name: str | None = None) -> str:
    """Deterministic data-disk name for a slice (the instance name plus the disk suffix)."""
    return f"{slice_instance_name(host_id, env_name)}{SLICE_DISK_SUFFIX}"


@pure
def _slice_resource_core(name: str) -> str | None:
    """The identity part of a slice instance/disk name: prefix and optional ``-data`` stripped.

    Returns None for any name that is not a slice resource (wrong prefix), so a
    non-slice lima resource is never misclassified.
    """
    if not name.startswith(SLICE_INSTANCE_PREFIX):
        return None
    core = name[len(SLICE_INSTANCE_PREFIX) :]
    if core.endswith(SLICE_DISK_SUFFIX):
        core = core[: -len(SLICE_DISK_SUFFIX)]
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
    return sum(1 for name in names if name.startswith(SLICE_INSTANCE_PREFIX))


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
    :func:`expected_static_authorized_key_count` for its generation, so any other
    count means a key was added out of band, which is how a box ends up reachable
    by a tier that does not own it.
    """
    return sum(1 for line in authorized_keys_text.splitlines() if line.strip() and not line.strip().startswith("#"))


@pure
def expected_static_authorized_key_count(box_generation: int) -> int:
    """Static keys a correctly prepped box authorizes for its service user: the pool key on gen-1, none on gen-2."""
    if box_generation >= FIRST_QEMU_BOX_GENERATION:
        return GEN2_EXPECTED_AUTHORIZED_KEY_COUNT
    return GEN1_EXPECTED_AUTHORIZED_KEY_COUNT


@pure
def _split_on_marker_line(stdout: str, marker: str, *, read_description: str) -> tuple[str, str]:
    """Split ``stdout`` on a marker line the box printed with ``echo <marker>``.

    Tries the exact ``<marker>\\n`` form first; a run whose marker was the very
    last thing printed (no trailing newline) falls back to a bare-marker split.
    """
    before, marker_found, after = stdout.partition(f"{marker}\n")
    if not marker_found:
        before, marker_found, after = stdout.partition(marker)
    if not marker_found:
        raise BareMetalConfigError(f"the {read_description} read printed no split marker; the box output is malformed")
    return before, after


# Separates the two files the management-trust read prints in one round trip.
_MANAGEMENT_TRUST_SPLIT_MARKER: Final[str] = "MNGR_MANAGEMENT_TRUST_SPLIT"


@pure
def build_read_management_trust_command() -> str:
    """Print the service user's ``authorized_keys`` and the box's trusted CA file, each tolerated absent.

    A missing ``authorized_keys`` is the gen-2 steady state (no static keys), and
    a missing CA file is the gen-1 one, so neither absence fails the command. The
    steps are ``&&``-chained so that a real read failure (an existing file ``cat``
    cannot read) short-circuits with ``cat``'s non-zero status and no marker,
    which the caller's exit-status check turns into an error; with ``;`` the
    shell would report the last step's status (always 0) and the unread file
    would parse as zero keys -- exactly what a gen-2 box is expected to have.
    """
    return (
        "if [ -e ~/.ssh/authorized_keys ]; then cat ~/.ssh/authorized_keys; fi && "
        f"echo {_MANAGEMENT_TRUST_SPLIT_MARKER} && "
        f"if [ -e {SSH_CA_PUBLIC_KEY_PATH} ]; then cat {SSH_CA_PUBLIC_KEY_PATH}; fi"
    )


@pure
def parse_management_trust_output(stdout: str) -> BoxManagementTrust:
    """Split :func:`build_read_management_trust_command`'s output into the key count and the trusted CA."""
    authorized_keys_text, ca_text = _split_on_marker_line(
        stdout, _MANAGEMENT_TRUST_SPLIT_MARKER, read_description="management-trust"
    )
    trusted_ca = ca_text.strip()
    return BoxManagementTrust(
        authorized_key_count=count_authorized_key_lines(authorized_keys_text),
        trusted_ca_public_key=trusted_ca if trusted_ca else None,
    )


# What the storage-volume probe prints: one line naming the block device
# mounted at the gen-2 storage root (empty when nothing is mounted there),
# then the marker, then that device's ``lsblk`` type (``crypt`` for an opened
# LUKS mapper; empty when nothing is mounted).
_STORAGE_VOLUME_SPLIT_MARKER: Final[str] = "MNGR_STORAGE_VOLUME_SPLIT"


@pure
def build_read_storage_volume_command() -> str:
    """Print the device mounted at the gen-2 storage root and its block-device type, tolerating an unmounted root.

    Both reads are unprivileged (mount tables and sysfs are world-readable),
    so the audit and the prep preflight can run them as the service user.
    ``findmnt`` exits non-zero when the path is not a mount point, which is
    the legitimate "locked box" answer, so that step's status is swallowed;
    a real read failure surfaces as an empty device line, which the parser
    reports as an unmounted (and therefore unencrypted) volume.
    """
    return (
        f"storage_source=$(findmnt -no SOURCE {GEN2_STORAGE_ROOT} 2>/dev/null || true); "
        'printf "%s\\n" "$storage_source"; '
        f"echo {_STORAGE_VOLUME_SPLIT_MARKER}; "
        'if [ -n "$storage_source" ]; then lsblk -no TYPE "$storage_source" 2>/dev/null | head -n 1; fi'
    )


@pure
def parse_storage_volume_output(stdout: str) -> StorageVolumeState:
    """Parse :func:`build_read_storage_volume_command`'s output into the mounted device and its encryption state."""
    source_text, type_text = _split_on_marker_line(
        stdout, _STORAGE_VOLUME_SPLIT_MARKER, read_description="storage-volume"
    )
    mounted_source = source_text.strip() or None
    device_type = type_text.strip()
    return StorageVolumeState(
        mounted_source=mounted_source,
        is_encrypted=mounted_source == GEN2_STORAGE_LUKS_MAPPER_PATH and device_type == "crypt",
    )


@pure
def is_trusted_ca_correct_for_tier(
    trust: BoxManagementTrust, box_generation: int, expected_ca_public_key: str | None
) -> bool:
    """Whether the box's CA trust is what its generation and tier call for.

    A gen-1 box has no CA trust to check. A gen-2 box must trust exactly the
    tier's committed CA; with no CA committed for the tier there is nothing it
    could correctly trust, so it is never correct.
    """
    if box_generation < FIRST_QEMU_BOX_GENERATION:
        return True
    if expected_ca_public_key is None or trust.trusted_ca_public_key is None:
        return False
    return is_same_ssh_public_key(trust.trusted_ca_public_key, expected_ca_public_key)


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


# How old a rowless slice must be before the orphan reap may touch it. A bake carves
# its VM and inserts the ``baking`` row before the carve, but a killed ``mngr create``
# or an operator's hand-carved slice has no row at all; an inactive rowless slice
# younger than this is still assumed to belong to someone's in-flight work.
ORPHAN_SLICE_MIN_AGE_SECONDS: Final[float] = 2 * 3600

# How old a CI-tier slice must be before the CI slice sweep destroys it, rowless or
# not, running or not: release runs are serialized and far shorter than this, so an
# older slice was certainly leaked by a run whose env (and DB) died before its own
# teardown. Deliberately longer than the reaper's guard above: that one protects an
# in-flight bake, this one protects an in-flight release run. Also the staleness
# threshold of the ci Modal-env sweep, so a leaked env and its slices age out
# together.
CI_SLICE_MAX_AGE_SECONDS: Final[float] = 4 * 3600


@pure
def compute_orphan_slice_instance_names(
    observations: Sequence[SliceInstanceObservation],
    tracked_instance_names: AbstractSet[str],
    env_name: str,
    min_age_seconds: float = ORPHAN_SLICE_MIN_AGE_SECONDS,
) -> set[str]:
    """This env's slice VMs on the box that are safe to reap: rowless, not running, and old.

    Filters to instances stamped for ``env_name`` so reconciliation never touches
    another env's slices, a legacy un-stamped slice, or an unrelated VM; subtracts
    the tracked set (every instance with a pool_hosts row in this env's DB, any
    status -- a bake's ``baking`` row included); and then keeps only instances whose
    VM is not running and whose on-box state is at least ``min_age_seconds`` old. A
    running or young rowless instance is somebody's in-flight carve until proven
    otherwise, so it is left alone (and logged by the caller).
    """
    rowless = _orphan_slice_resource_names(
        {observation.instance_name for observation in observations}, tracked_instance_names, env_name
    )
    return {
        observation.instance_name
        for observation in observations
        if observation.instance_name in rowless
        and not observation.is_active
        and observation.age_seconds >= min_age_seconds
    }


@pure
def compute_orphan_slice_disk_names(
    box_disk_names: AbstractSet[str],
    tracked_disk_names: AbstractSet[str],
    env_name: str,
    held_instance_names: AbstractSet[str],
) -> set[str]:
    """This env's slice data disks on the box that are safe to reap: rowless and not held by an instance the reap keeps.

    The disk analogue of :func:`compute_orphan_slice_instance_names`. Reaped
    separately because a disk can outlive its instance (a failed carve's rollback
    that could not unlock the disk leaves it behind, permanently holding the box
    slot). ``held_instance_names`` are the instances still on the box after the
    instance reap -- tracked, running, or spared as young; a disk whose owning
    instance (its name minus the ``-data`` suffix) is among them is never an orphan:
    unlinking it under a live VM keeps qemu running on the deleted inode and loses
    everything at its next restart, and a spared VM would be destroyed by losing its
    disk just the same.
    """
    rowless = _orphan_slice_resource_names(box_disk_names, tracked_disk_names, env_name)
    return {name for name in rowless if name.removesuffix(SLICE_DISK_SUFFIX) not in held_instance_names}


@pure
def partition_slice_names_by_tier_and_age(
    age_seconds_by_name: Mapping[str, float],
    tier: str,
    max_age_seconds: float,
) -> tuple[set[str], set[str], set[str]]:
    """Split slice resource names into (stale in ``tier``, young in ``tier``, owned by another tier).

    The tier-scoped sibling of :func:`compute_orphan_slice_instance_names`: no DB
    rows are consulted (a crashed CI run's DB is gone), so age alone decides,
    and a running VM is NOT spared -- a run that died leaves its VMs running.
    Names without a stamped owner (legacy slices, non-slice resources) are
    ignored entirely: they cannot be attributed, so an age-based sweep must not
    touch them.
    """
    stale: set[str] = set()
    young: set[str] = set()
    foreign: set[str] = set()
    for name, age_seconds in age_seconds_by_name.items():
        owner = slice_name_env_owner(name)
        if owner is None:
            continue
        if tier_for_env_name(owner) != tier:
            foreign.add(name)
        elif age_seconds > max_age_seconds:
            stale.add(name)
        else:
            young.add(name)
    return stale, young, foreign


@pure
def compute_tier_orphan_disk_names(
    box_disk_names: AbstractSet[str],
    tier: str,
    held_instance_names: AbstractSet[str],
) -> tuple[set[str], set[str]]:
    """Split slice data disks into (``tier``-owned disks whose instance is gone, disks owned by another tier).

    The tier-scoped sibling of :func:`compute_orphan_slice_disk_names`: a disk whose
    owning instance (its name minus the disk suffix) is still on the box is held
    by it and never touched, whatever its age; a disk that outlived its instance
    is the leak this exists to reclaim. Unowned names are ignored.
    """
    orphans: set[str] = set()
    foreign: set[str] = set()
    for name in box_disk_names:
        owner = slice_name_env_owner(name)
        if owner is None:
            continue
        if tier_for_env_name(owner) != tier:
            foreign.add(name)
        elif name.removesuffix(SLICE_DISK_SUFFIX) not in held_instance_names:
            orphans.add(name)
        else:
            # The instance is still on the box and holds its disk.
            pass
    return orphans, foreign


@pure
def next_server_status(current: BareMetalServerStatus) -> BareMetalServerStatus | None:
    """Return the next forward lifecycle status, or None if ``current`` is terminal (ready/failed)."""
    next_value = _NEXT_STATUS_BY_CURRENT.get(str(current))
    return BareMetalServerStatus(next_value) if next_value is not None else None


@pure
def is_valid_status_transition(current: BareMetalServerStatus, target: BareMetalServerStatus) -> bool:
    """Whether advancing a server from ``current`` to ``target`` is allowed.

    Covers the PROVISIONING chain only: forward moves follow the fixed
    ordered->delivered->installing->ready order, a move to ``failed`` is
    allowed from any non-terminal state, and the chain ends at ready/failed.
    The fleet-turnover exit (ready -> draining) is deliberately outside this
    helper -- it is driven by ``minds-admin server drain``, never by the
    step-forward provisioning commands.
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
