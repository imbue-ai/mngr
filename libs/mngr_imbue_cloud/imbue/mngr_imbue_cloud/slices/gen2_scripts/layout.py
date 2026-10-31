import ipaddress
import json
import re
from collections.abc import Sequence
from enum import auto
from typing import Final

from pydantic import Field

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr_imbue_cloud.slices.gen2_scripts.errors import InvalidSliceOrdinalError
from imbue.mngr_imbue_cloud.slices.gen2_scripts.errors import MalformedBoxOutputError
from imbue.mngr_imbue_cloud.slices.gen2_scripts.errors import MalformedPublishedPortsError

# The first slice-fleet generation that carves raw-qemu VMs under systemd
# instead of lima VMs (specs/slice-fleet-gen2). A box only ever runs one
# generation, stamped on its bare_metal_servers row.
FIRST_QEMU_BOX_GENERATION: Final[int] = 2

# Box layout (the XFS storage partition prep mounts at the storage root)

# Root of all gen-2 slice state on the box (the XFS storage partition's mount
# point). Everything under it is owned by the slice service user except each
# VM's own disk media and runtime dir, which the unit's root setup step hands
# to that VM's unix user (a VM escapee must not reach anything else).
GEN2_STORAGE_ROOT: Final[str] = "/srv/mngr-slices"
# The box's dedicated non-root service user, which owns the slice state, drives
# the reserve/destroy scripts over SSH, and holds the scoped sudoers grant. Prep
# creates it; the unit, the root helper, and the sudoers entry must all agree on
# it, so they all read it from here.
GEN2_SLICE_SERVICE_USER: Final[str] = "slicehost"
# Per-instance state dirs, keyed by the slice's full instance name.
GEN2_INSTANCES_DIR: Final[str] = f"{GEN2_STORAGE_ROOT}/instances"
# Ordinal -> instance-dir symlinks, so the systemd template unit (whose instance
# parameter is the ordinal) can reference per-slice files via ``%i``.
GEN2_BY_ORDINAL_DIR: Final[str] = f"{GEN2_STORAGE_ROOT}/by-ordinal"
# The staged trixie guest image every slice's boot disk is reflink-copied from
# (``cp --reflink=auto``: instant on XFS, a plain copy elsewhere; either way the
# copy is fully independent, so re-staging the base never touches existing VMs).
GEN2_BASE_IMAGE_PATH: Final[str] = f"{GEN2_STORAGE_ROOT}/base/debian-13-base.qcow2"

# The box swapfile and the per-tag ``docker save`` image tar cache both live on
# the storage partition (the root partition is sized for the OS alone); the
# gen-2 reserve in ``sizing`` names their budgets.
GEN2_SWAPFILE_PATH: Final[str] = f"{GEN2_STORAGE_ROOT}/swapfile"
# The storage partition is a LUKS2 volume: the gen-2 prep formats the box's
# md-mirrored storage partition as LUKS (unlocked at boot by the box's TPM,
# with a per-box recovery passphrase in the tier's Vault) and mounts the
# opened mapper device at the storage root, so every slice disk, the base
# image, the tar cache and the swapfile are ciphertext at rest. The mapper
# name is fixed so crypttab, fstab, the unlock command and the audit all
# agree on it.
GEN2_STORAGE_LUKS_MAPPER_NAME: Final[str] = "mngr-storage"
GEN2_STORAGE_LUKS_MAPPER_PATH: Final[str] = f"/dev/mapper/{GEN2_STORAGE_LUKS_MAPPER_NAME}"
# The tree on the encrypted volume that takes over the root partition's
# user-adjacent state: the box journal (which carries the guest consoles), the
# slice service user's home (where transfers stage credentials and decrypted
# cidata), and the two temp directories. Each is bind-mounted over its usual
# path by a prep-installed mount unit, so nothing else on the box changes.
GEN2_STORAGE_SYSTEM_DIR: Final[str] = f"{GEN2_STORAGE_ROOT}/system"
GEN2_IMAGE_TAR_CACHE_DIR: Final[str] = f"{GEN2_STORAGE_ROOT}/image-cache"
# The gen-2 prep echoes the mounted storage partition's size (whole GiB) on
# this marker line; the operator tooling records it as the box row's
# ``disk_gb``, the input of ``sizing.compute_gen2_disk_budget_gib``.
GEN2_STORAGE_PARTITION_MARKER: Final[str] = "MNGR_STORAGE_PARTITION_GIB"

# Prep-installed root-owned artifacts (with the slice DHCP server's, below, the
# ONLY box-installed pieces -- everything day-to-day is caller-rendered). Prep
# converges each on content.
GEN2_UNIT_PATH: Final[str] = "/etc/systemd/system/mngr-slice@.service"
GEN2_HELPER_PATH: Final[str] = "/usr/local/sbin/mngr-slice-helper"
GEN2_SUDOERS_PATH: Final[str] = "/etc/sudoers.d/mngr-slice"

# Prep pre-creates the per-slice unix users (``mngr-slice-0`` .. ``-511``, each in
# the ``kvm`` group) so the unit's ``User=mngr-slice-%i`` always resolves; the
# reserve script never allocates an ordinal at or above this bound. Sized far
# above any real box's machine count (the unit budget binds first: even a 4TB
# box holds fewer than 512 minimum-size machines), so ordinal exhaustion is
# never the binding capacity limit.
GEN2_MAX_SLICE_COUNT: Final[int] = 512

# The OVMF code-only pflash image the template unit boots qemu with. Trixie's
# ovmf package ships ONLY the _4M builds (verified live on the first gen-2 box,
# 2026-08-23; the plain OVMF_CODE.fd of the bookworm-era spike is gone), and
# every gen-2 box is trixie by construction, so the _4M path is unconditional.
# Prep verifies the file exists right after installing ovmf, so a future
# packaging move fails the prep loudly instead of surfacing at first VM start.
GEN2_OVMF_CODE_PATH: Final[str] = "/usr/share/OVMF/OVMF_CODE_4M.fd"

# Range of host ports on each box reserved for slice port-forwards. Each slice
# claims two: one -> the VM's root sshd, one -> the inner container sshd. Wide
# enough (~10k ports) for large boxes carved into many slices.
DEFAULT_SLICE_PORT_RANGE_START: Final[int] = 22000
DEFAULT_SLICE_PORT_RANGE_END: Final[int] = 32000

# Per-VM networking

# Box-local range the per-VM /30s are carved from (ordinal N owns the /30 at
# offset 4*N). /16 leaves room far beyond GEN2_MAX_SLICE_COUNT; the range is
# private and never routed off the box.
GEN2_SLICE_SUBNET_BASE: Final[str] = "10.201.0.0"
# Guest-side service ports. The VM's sshd listens on plain 22 (no lima, so no
# reserved-port workaround), and the agent host container's sshd is published
# inside the VM on 2222 (matches VpsProviderConfig.container_ssh_port).
GEN2_VM_SSH_GUEST_PORT: Final[int] = 22
GEN2_CONTAINER_SSH_GUEST_PORT: Final[int] = 2222
# The one nftables table all per-VM rules live in; per-slice rules are tagged
# with a ``mngr-slice-ord-<N>`` comment so teardown can delete them by handle.
GEN2_NFT_TABLE: Final[str] = "mngr_slices"

# Guest addressing is handed out by a box-side DHCP server (dnsmasq, DHCP only,
# no DNS) listening on the slice taps: each ordinal's tap answers with exactly
# the /30 address the anti-spoof and DNAT rules expect, so the guest's cidata
# carries no placement (no address, MAC, or gateway) and is consumed exactly
# once, at first boot -- a restore onto another ordinal or box never replays
# cloud-init. Prep installs the config, unit and udp/67 policy like the other
# root-owned artifacts (content-converged, in the telemetry integrity manifest).
GEN2_DHCP_UNIT_NAME: Final[str] = "mngr-slice-dhcp.service"
GEN2_DHCP_UNIT_PATH: Final[str] = f"/etc/systemd/system/{GEN2_DHCP_UNIT_NAME}"
GEN2_DHCP_CONFIG_PATH: Final[str] = "/etc/mngr/slice-dhcp.conf"
# The dedicated unprivileged system user the DHCP server runs as for its whole
# life (the unit starts it as this user; it is never root). Prep creates it.
GEN2_DHCP_USER: Final[str] = "mngr-dhcp"
# The unit's systemd StateDirectory (relative to /var/lib): the one writable
# path of the sandboxed server, holding its lease file.
GEN2_DHCP_STATE_DIRECTORY_NAME: Final[str] = "mngr-slice-dhcp"
GEN2_DHCP_LEASE_DIR: Final[str] = f"/var/lib/{GEN2_DHCP_STATE_DIRECTORY_NAME}"
GEN2_DHCP_LEASE_FILE_PATH: Final[str] = f"{GEN2_DHCP_LEASE_DIR}/leases"
# The box-level nftables policy that keeps udp/67 off every interface but the
# slice taps (the DHCP socket is wildcard-bound), in its own table alongside
# the management lockdown's, boot-persistent through nftables.service.
GEN2_DHCP_NFT_TABLE: Final[str] = "mngr_slice_dhcp"
GEN2_DHCP_NFT_POLICY_PATH: Final[str] = "/etc/nftables.d/mngr-slice-dhcp.nft"
# Long but finite: leases are keyed by the ordinal-derived MAC, which a
# re-carve of the same ordinal reuses, so a stale lease never blocks the one
# address in the range; finite so the lease table still self-cleans.
GEN2_DHCP_LEASE_TIME: Final[str] = "12h"
# The DHCPv4 server/client ports the slice helper's input chain admits from
# the tap (everything else guest-initiated stays dropped).
GEN2_DHCP_SERVER_PORT: Final[int] = 67
GEN2_DHCP_CLIENT_PORT: Final[int] = 68
# The public resolvers DHCP pushes to every guest (option 6). The box runs no
# caching resolver for the slices.
GEN2_GUEST_DNS_SERVERS: Final[tuple[str, ...]] = ("1.1.1.1", "8.8.8.8")
# Day-one enforced ceilings (specs/slice-fleet-gen2): no legitimate agent host
# approaches these; finer rules stay alert-only until baselined.
GEN2_MAX_CONCURRENT_CONNECTIONS: Final[int] = 50000
GEN2_MAX_NEW_CONNECTIONS_PER_SECOND: Final[int] = 300
# skb-mark base for the per-VM tc fair-share filter ("m" for mngr, shifted clear
# of common small marks); mark = base + ordinal.
GEN2_TC_MARK_BASE: Final[int] = 0x6D0000

# Reserve-script contract

# Box-wide advisory lock serializing the reservation critical section across all
# bakes on one box.
GEN2_ALLOC_LOCK_RELPATH: Final[str] = ".mngr-slice-alloc.lock"
GEN2_RESERVED_MARKER: Final[str] = "MNGR_SLICE_RESERVED"
GEN2_NO_PORTS_MARKER: Final[str] = "MNGR_SLICE_NO_PORTS"
# The two-budget capacity refusals (specs/slice-fleet): the box's memory-unit
# budget or its disk budget cannot fit the new machine. Distinct markers so
# callers can report which resource ran out.
GEN2_NO_UNITS_MARKER: Final[str] = "MNGR_SLICE_NO_UNITS"
GEN2_NO_DISK_MARKER: Final[str] = "MNGR_SLICE_NO_DISK"
# Distinct from the budget refusals: the budget math says there is room, but
# the storage filesystem's real free space cannot hold the new slice's full
# virtual size (the carve-time df guard -- something outside the budget model
# leaked space).
GEN2_NO_SPACE_MARKER: Final[str] = "MNGR_SLICE_NO_SPACE"

# Identifier suffix for a slice's data disk.
GEN2_DISK_SUFFIX: Final[str] = "-data"

# Placeholder tokens for the two box host ports in the env-file *template*; the
# real ports are chosen on the box under the reservation lock and substituted.
GEN2_VM_SSH_PORT_PLACEHOLDER: Final[str] = "__MNGR_VM_SSH_PORT__"
GEN2_CONTAINER_SSH_PORT_PLACEHOLDER: Final[str] = "__MNGR_CONTAINER_SSH_PORT__"
# Placeholders for the ordinal and its derived values inside the env-file
# *template*: the ordinal is chosen on the box under the lock, and the box-side
# script computes the MAC and /30 addresses from it with shell arithmetic --
# ONE template ships instead of a per-candidate-ordinal payload table, which
# does not scale to 512 ordinals. The cidata itself carries no placement (the
# guest gets its address by DHCP), so no cidata file is ever a template.
GEN2_ORDINAL_PLACEHOLDER: Final[str] = "__MNGR_SLICE_ORDINAL__"
GEN2_MAC_PLACEHOLDER: Final[str] = "__MNGR_SLICE_MAC__"
GEN2_VM_IP_PLACEHOLDER: Final[str] = "__MNGR_SLICE_VM_IP__"
GEN2_GATEWAY_IP_PLACEHOLDER: Final[str] = "__MNGR_SLICE_GATEWAY_IP__"

# Published ports (specs/published-ports): the box ports a slice forwards to
# its workload's declared guest ports, beside the two sshd ports.

# The env-file key holding the slice's published ports: a compact JSON list
# of ``{"guest_port", "protocol", "host_port"}`` objects (no whitespace), so
# the root helper can validate it with one strict regex and read it without
# a JSON parser. A slice carved before published ports existed has no such
# key, which every reader treats as the empty list.
GEN2_PUBLISHED_PORTS_ENV_KEY: Final[str] = "MNGR_SLICE_PUBLISHED_PORTS"
# Whole-value placeholder for the env-file *template* of a script that
# preserves a slice's recorded published ports verbatim (the in-place resize).
GEN2_PUBLISHED_PORTS_PLACEHOLDER: Final[str] = "__MNGR_PUBLISHED_PORTS__"
# Per-entry host-port placeholder prefix for the env-file template of a
# script that picks the box ports itself under the reservation lock (the
# carve and restore reserves); ``published_port_placeholder(index)`` renders
# the token of the index-th distinct guest port.
_GEN2_PUBLISHED_PORT_PLACEHOLDER_PREFIX: Final[str] = "__MNGR_PUBLISHED_PORT_"
# The bash ERE matching exactly one rendered entry (``{``/``}`` are literal in
# a bash ERE only when escaped, hence the backslashes).
GEN2_PUBLISHED_PORT_ENTRY_BASH_RE: Final[str] = '\\{"guest_port":[0-9]+,"protocol":"(tcp|udp)","host_port":[0-9]+\\}'
_PUBLISHED_PORT_ENTRY_RE: Final[re.Pattern[str]] = re.compile(
    r'^\{"guest_port":(\d+),"protocol":"(tcp|udp)","host_port":(\d+)\}$'
)


class SlicePortProtocol(LowerCaseStrEnum):
    """The transport protocol of one published slice port, in its wire and env-file spelling."""

    TCP = auto()
    UDP = auto()


class SlicePortDeclaration(FrozenModel):
    """A guest port and protocol a slice's workload listens on; the box port is picked at reserve."""

    guest_port: int = Field(ge=1, le=65535, description="The port the workload binds inside the container")
    protocol: SlicePortProtocol = Field(description="The transport protocol")


class SlicePublishedPort(FrozenModel):
    """One declared guest port with the box port the reserve forwarded to it (the connector's row entry)."""

    guest_port: int = Field(ge=1, le=65535, description="The declared port inside the container")
    protocol: SlicePortProtocol = Field(description="The declared transport protocol")
    host_port: int = Field(ge=1, le=65535, description="The box port DNAT'd to the guest port")

    @property
    def declaration(self) -> SlicePortDeclaration:
        return SlicePortDeclaration(guest_port=self.guest_port, protocol=self.protocol)


class Gen2Reservation(FrozenModel):
    """What a reserve script printed on its marker line: the sshd ports, the ordinal, and the published box ports."""

    vm_ssh_host_port: int = Field(description="Box port forwarded to the VM's root sshd")
    container_ssh_host_port: int = Field(description="Box port forwarded to the inner container sshd")
    ordinal: int = Field(description="The slice's chosen box-local ordinal")
    published_host_ports: tuple[int, ...] = Field(
        default=(),
        description=(
            "One box port per distinct declared guest port, in the order of ``distinct_guest_ports``; a "
            "reserve rendered before published ports existed prints none"
        ),
    )


@pure
def distinct_guest_ports(declarations: Sequence[SlicePortDeclaration]) -> tuple[int, ...]:
    """The declared guest ports without duplicates, in first-appearance order.

    A guest port declared for both protocols gets ONE box port serving both
    (one DNAT rule per protocol), so the reserve picks one port per distinct
    guest port rather than per declaration.
    """
    ordered: list[int] = []
    for declaration in declarations:
        if declaration.guest_port not in ordered:
            ordered.append(declaration.guest_port)
    return tuple(ordered)


@pure
def published_port_placeholder(index: int) -> str:
    """The env-template token the reserve substitutes with the box port of the index-th distinct guest port."""
    return f"{_GEN2_PUBLISHED_PORT_PLACEHOLDER_PREFIX}{index}__"


@pure
def assign_published_host_ports(
    declarations: Sequence[SlicePortDeclaration], host_ports: Sequence[int]
) -> tuple[SlicePublishedPort, ...]:
    """Pair every declaration with the box port the reserve picked for its guest port.

    ``host_ports`` is the reserve's marker-line list, one per distinct guest
    port in ``distinct_guest_ports`` order; a count mismatch means the reserve
    script and the declarations it was rendered from disagree.
    """
    guest_ports = distinct_guest_ports(declarations)
    if len(guest_ports) != len(host_ports):
        raise MalformedBoxOutputError(
            f"the reserve printed {len(host_ports)} published box port(s) for {len(guest_ports)} declared guest port(s)"
        )
    host_port_by_guest_port = dict(zip(guest_ports, host_ports, strict=True))
    return tuple(
        SlicePublishedPort(
            guest_port=declaration.guest_port,
            protocol=declaration.protocol,
            host_port=host_port_by_guest_port[declaration.guest_port],
        )
        for declaration in declarations
    )


@pure
def _render_published_port_entry(guest_port: int, protocol: SlicePortProtocol, host_port: int | str) -> str:
    return f'{{"guest_port":{guest_port},"protocol":"{protocol.value}","host_port":{host_port}}}'


@pure
def render_published_ports_env_value(entries: Sequence[SlicePublishedPort]) -> str:
    """The compact JSON list the env file and the connector row hold for a slice's published ports."""
    return "[" + ",".join(_render_published_port_entry(e.guest_port, e.protocol, e.host_port) for e in entries) + "]"


@pure
def render_published_ports_env_template_value(declarations: Sequence[SlicePortDeclaration]) -> str:
    """The env-template form of the published ports: each host port is its per-guest-port placeholder token.

    Not valid JSON until the reserve substitutes the tokens with the box ports
    it picked (``sed`` on the box, like the two sshd-port tokens).
    """
    guest_ports = distinct_guest_ports(declarations)
    return (
        "["
        + ",".join(
            _render_published_port_entry(
                d.guest_port, d.protocol, published_port_placeholder(guest_ports.index(d.guest_port))
            )
            for d in declarations
        )
        + "]"
    )


@pure
def parse_published_ports_env_value(value: str) -> tuple[SlicePublishedPort, ...]:
    """Parse the env-file (or connector row) JSON list back into entries; the empty string reads as none.

    Raises :class:`MalformedPublishedPortsError` when the value is not a JSON
    list of ``{"guest_port", "protocol", "host_port"}`` objects.
    """
    if not value.strip():
        return ()
    try:
        parsed = json.loads(value)
    except ValueError as e:
        raise MalformedPublishedPortsError(f"published ports value is not JSON: {value!r}") from e
    if not isinstance(parsed, list):
        raise MalformedPublishedPortsError(f"published ports value is not a JSON list: {value!r}")
    entries: list[SlicePublishedPort] = []
    for item in parsed:
        if not isinstance(item, dict):
            raise MalformedPublishedPortsError(f"published ports entry is not an object: {item!r}")
        try:
            entries.append(SlicePublishedPort.model_validate(item))
        except ValueError as e:
            raise MalformedPublishedPortsError(f"malformed published ports entry {item!r}: {e}") from e
    return tuple(entries)


@pure
def is_rendered_published_port_entry(text: str) -> bool:
    """Whether ``text`` is exactly one entry as :func:`render_published_ports_env_value` spells it."""
    return _PUBLISHED_PORT_ENTRY_RE.match(text) is not None


@pure
def parse_reservation_marker_line(stdout: str, marker: str) -> Gen2Reservation | None:
    """Parse ``<marker> <vm_port> <container_port> <ordinal> [<published_port>...]`` out of a reserve's stdout.

    None when no marker line is present or the line is malformed. The
    published ports follow the ordinal so the three-field line an older
    reserve script prints still parses (with no published ports).
    """
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped.startswith(marker):
            continue
        parts = stripped.split()
        if parts[0] != marker or len(parts) < 4 or not all(part.isdigit() for part in parts[1:]):
            return None
        return Gen2Reservation(
            vm_ssh_host_port=int(parts[1]),
            container_ssh_host_port=int(parts[2]),
            ordinal=int(parts[3]),
            published_host_ports=tuple(int(part) for part in parts[4:]),
        )
    return None


# Guest layout (inside the slice VM)

# Where the gen-2 guest mounts its data disk, and the two container-engine roots
# on it: docker's data-root (metadata, volumes, build cache) and containerd's
# root, whose overlayfs snapshotter holds the image layers and every container's
# writable layer (Docker's image store is containerd's; ``/var/lib/docker`` holds
# no layers).
GEN2_GUEST_DATA_MOUNT: Final[str] = "/mnt/mngr-data"
GEN2_GUEST_DATA_FS_LABEL: Final[str] = "mngr-data"
GEN2_GUEST_DOCKER_DATA_ROOT: Final[str] = f"{GEN2_GUEST_DATA_MOUNT}/docker"
GEN2_GUEST_CONTAINERD_ROOT: Final[str] = f"{GEN2_GUEST_DATA_MOUNT}/containerd"
GEN2_GUEST_CONTAINERD_SNAPSHOTS_SUBVOLUME: Final[str] = (
    f"{GEN2_GUEST_CONTAINERD_ROOT}/io.containerd.snapshotter.v1.overlayfs"
)
GEN2_GUEST_CONTAINERD_CONTENT_SUBVOLUME: Final[str] = f"{GEN2_GUEST_CONTAINERD_ROOT}/io.containerd.content.v1.content"

# The btrfs qgroup every subvolume the agent host writes is charged to, the
# docker label that marks the agent host's container, and where its host volume
# is mounted inside it. All three are contracts owned by
# ``imbue.mngr_vps.container_setup`` (``HOST_QUOTA_QGROUP`` / ``LABEL_HOST_ID`` /
# ``HOST_VOLUME_MOUNT_PATH``), which does not ship into the connector container;
# the plugin's tests pin these copies to the originals.
GEN2_GUEST_HOST_QUOTA_QGROUP: Final[str] = "1/0"
GEN2_HOST_ID_CONTAINER_LABEL: Final[str] = "com.imbue.mngr.host-id"
GEN2_CONTAINER_HOST_VOLUME_MOUNT_PATH: Final[str] = "/mngr-vol"

# The file, at the root of the agent host's volume, where the VM publishes the
# container's memory headroom as its memory cgroup sees it, for the earlyoom
# inside to read at ``/mngr-vol/.host-meminfo`` (``--host-meminfo``): under
# gVisor the container's own /proc/meminfo cannot see the runtime's share of
# the limit. A workspace running under runc writes the same file for itself
# from its own cgroup (the template's publish_cgroup_meminfo program, which
# mirrors the formula of the VM-side publisher in ``guest.py`` and exits under
# gVisor).
GEN2_CONTAINER_HOST_MEMINFO_FILE_NAME: Final[str] = ".host-meminfo"


class SliceNetwork(FrozenModel):
    """A gen-2 slice's routed point-to-point /30: the box-side gateway and the VM's address."""

    gateway_ip: str = Field(description="The box-side address on the slice's tap (the VM's default gateway)")
    vm_ip: str = Field(description="The VM's address inside its /30")
    prefix_length: int = Field(description="The subnet prefix length (30: no other tenant shares the segment)")


@pure
def slice_tap_name(ordinal: int) -> str:
    """The host-side tap interface name for a slice ordinal (fits IFNAMSIZ)."""
    _assert_valid_ordinal(ordinal)
    return f"mslice{ordinal}"


@pure
def slice_unix_user(ordinal: int) -> str:
    """The per-slice unix user that qemu runs as (pre-created by prep, in the kvm group)."""
    _assert_valid_ordinal(ordinal)
    return f"mngr-slice-{ordinal}"


@pure
def slice_mac_address(ordinal: int) -> str:
    """A deterministic locally-administered MAC for the slice's virtio NIC."""
    _assert_valid_ordinal(ordinal)
    return f"52:54:00:6d:{(ordinal >> 8) & 0xFF:02x}:{ordinal & 0xFF:02x}"


@pure
def slice_unit_name(ordinal: int) -> str:
    """The slice's systemd unit instance name (``mngr-slice@<ordinal>``)."""
    _assert_valid_ordinal(ordinal)
    return f"mngr-slice@{ordinal}"


@pure
def _assert_valid_ordinal(ordinal: int) -> None:
    if not 0 <= ordinal < GEN2_MAX_SLICE_COUNT:
        raise InvalidSliceOrdinalError(f"slice ordinal must be in [0, {GEN2_MAX_SLICE_COUNT}), got {ordinal}")


@pure
def derive_slice_network(ordinal: int) -> SliceNetwork:
    """The slice's /30: the box-side gateway address and the VM's address.

    Ordinal N owns the four addresses at offset 4*N inside the reserved range;
    the gateway is the first usable address, the VM the second. Routed
    point-to-point (no bridge), so the /30 contains no other tenant.
    """
    _assert_valid_ordinal(ordinal)
    base = int(ipaddress.IPv4Address(GEN2_SLICE_SUBNET_BASE))
    network_address = base + 4 * ordinal
    return SliceNetwork(
        gateway_ip=str(ipaddress.IPv4Address(network_address + 1)),
        vm_ip=str(ipaddress.IPv4Address(network_address + 2)),
        prefix_length=30,
    )


@pure
def gen2_instance_dir(instance_name: str) -> str:
    """Absolute path of a gen-2 slice's per-instance state directory on the box."""
    return f"{GEN2_INSTANCES_DIR}/{instance_name}"


@pure
def gen2_disk_name(instance_name: str) -> str:
    """The identifier of a gen-2 slice's data disk (instance name + the shared suffix)."""
    return f"{instance_name}{GEN2_DISK_SUFFIX}"
