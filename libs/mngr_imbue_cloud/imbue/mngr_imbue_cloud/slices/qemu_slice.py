"""Pure renderers for generation-2 slices: raw qemu VMs under systemd on a trixie box.

Gen-2 slices (specs/slice-fleet-gen2) replace lima with directly-managed qemu:

- One systemd template unit (``mngr-slice@<ordinal>.service``) runs each VM as its
  own per-slice unix user, with a root ``ExecStartPre`` creating the VM's routed
  tap + per-VM nftables rules and a root ``ExecStopPost`` tearing them down.
- Cloud-init material is generated ONCE at carve time with a stable instance-id
  and carries no placement (the guest gets its address by DHCP from the box), so
  nothing ever replays on later boots or restores -- host keys and
  authorized_keys simply persist, and the gen-1 reconciler problem class does
  not exist here.
- Each VM gets its own /30 on its own tap (no shared L2 segment); the box routes
  and NATs, hands out the /30 address over DHCP on the tap, and the public
  contract (box IP + two forwarded ports) is preserved via kernel DNAT.

This module holds the renderers only the plugin uses: the prep-installed
artifacts (unit / helper / sudoers / the DHCP server's config, unit and
udp/67 policy, installed by ``minds-admin server prep``) and the carve-time
reserve / start / boot-wait / status commands. Everything a box-side script
shares with the remote_service_connector's stop/start supervisor -- the
layout constants, the sizing math, the cloud-init material, the env file, the
destroy and listing commands, the transfer scripts -- lives in
:mod:`imbue.mngr_imbue_cloud.slices.gen2_scripts`, which ships into the
connector container. The SSH driving lives in
:mod:`imbue.mngr_imbue_cloud.slices.qemu_slice_client`.
"""

import base64
import ipaddress
import shlex
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.mngr_imbue_cloud.errors import SliceReserveOutputError
from imbue.mngr_imbue_cloud.slices.gen2_scripts.box_commands import render_gen2_budget_guard_lines
from imbue.mngr_imbue_cloud.slices.gen2_scripts.box_commands import render_gen2_ordinal_derivation_lines
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_ALLOC_LOCK_RELPATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_BASE_IMAGE_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_BY_ORDINAL_DIR
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_CONTAINER_SSH_GUEST_PORT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_CONTAINER_SSH_PORT_PLACEHOLDER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_DHCP_CLIENT_PORT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_DHCP_CONFIG_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_DHCP_LEASE_FILE_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_DHCP_LEASE_TIME
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_DHCP_NFT_TABLE
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_DHCP_SERVER_PORT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_DHCP_STATE_DIRECTORY_NAME
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_DHCP_UNIT_NAME
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_DHCP_USER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_GUEST_DNS_SERVERS
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_HELPER_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_INSTANCES_DIR
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_MAX_CONCURRENT_CONNECTIONS
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_MAX_NEW_CONNECTIONS_PER_SECOND
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_MAX_SLICE_COUNT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_NFT_TABLE
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_NO_PORTS_MARKER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_NO_SPACE_MARKER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_NO_UNITS_MARKER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_OVMF_CODE_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_RESERVED_MARKER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_SLICE_SERVICE_USER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_SLICE_SUBNET_BASE
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_STORAGE_ROOT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_TC_MARK_BASE
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_VM_SSH_GUEST_PORT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_VM_SSH_PORT_PLACEHOLDER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import derive_slice_network
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import gen2_instance_dir
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import slice_unit_name
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import DEFAULT_MACHINE_UNITS
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import GEN2_UPLINK_SHAPING_PERCENT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import PER_VM_RAM_OVERHEAD_MIB

# Fixed safety margin the df guard requires beyond the slice's own virtual size.
_DF_GUARD_MARGIN_GIB: Final[int] = 2

# ---------------------------------------------------------------------------
# Prep-installed artifacts: the template unit, the root helper, sudoers, and the
# slice DHCP server's config, unit and udp/67 policy
# ---------------------------------------------------------------------------


# Thread ceiling for one VM's unit: qemu itself runs one thread per vCPU plus
# a handful of I/O and RCU threads (well under 100 even at 128 vCPUs), and it
# never spawns (`-sandbox ... spawn=deny`), so a fork bomb inside the unit is
# not possible -- the cap is cheap defense in depth against a qemu bug.
GEN2_UNIT_TASKS_MAX: Final[int] = 1024

# The process-hardening baseline both gen-2 unit sandboxes (the slice VM's and
# the slice DHCP server's) spread into, so a tightening lands on both at once.
# Each sandbox adds its own capability, socket, device and write-path rules.
_GEN2_SANDBOX_BASELINE_DIRECTIVES: Final[tuple[str, ...]] = (
    "RestrictRealtime=yes",
    "RestrictSUIDSGID=yes",
    "LockPersonality=yes",
    "ProtectSystem=strict",
    "ProtectHome=yes",
    "PrivateTmp=yes",
    "ProtectKernelTunables=yes",
    "ProtectKernelModules=yes",
    "ProtectKernelLogs=yes",
    "ProtectControlGroups=yes",
    "ProtectClock=yes",
    "ProtectHostname=yes",
    "ProtectProc=invisible",
    "ProcSubset=pid",
    "SystemCallArchitectures=native",
    "SystemCallFilter=@system-service",
    "SystemCallFilter=~@privileged @resources",
)

# The systemd sandbox the qemu process runs in (`systemd-analyze security`
# 9.2 UNSAFE -> 1.0 OK). The `+`-prefixed helper steps are exempt from these
# (they run as root outside the sandbox), so only qemu itself is confined:
#
# - ProtectSystem=strict makes the whole filesystem read-only to qemu except
#   the paths in ReadWritePaths: its qmp socket dir and the two disk images
#   (through the by-ordinal symlink, which systemd follows). The env file and
#   cidata deliberately stay read-only to it. Without the write paths qemu dies
#   at `-qmp`: "Failed to unlink socket .../run/qmp.sock: Read-only file system".
#   The paths carry the `-` prefix (ignore when absent): systemd sets the bind
#   mounts up for the `+` helper steps too, and at a slice's very first start
#   the run/ dir does not exist until the setup step creates it -- without the
#   prefix the start dies at "Failed to set up mount namespacing ... run: No
#   such file or directory".
# - DevicePolicy=closed with exactly /dev/kvm and /dev/net/tun: qemu is KVM-only
#   (no TCG), so a box without /dev/kvm fails loudly instead of emulating.
# - MemoryDenyWriteExecute holds because there is no TCG code buffer.
# - RestrictAddressFamilies=AF_UNIX: the QMP socket is the only socket qemu
#   opens itself (the tap is a file descriptor, not a socket), and
#   IPAddressDeny=any does not touch tap traffic for the same reason.
# - SystemCallFilter=@system-service minus @privileged/@resources: the
#   resource limits it forbids are applied by the root helper via
#   `systemctl set-property` instead.
GEN2_UNIT_SANDBOX_DIRECTIVES: Final[tuple[str, ...]] = (
    "NoNewPrivileges=yes",
    "CapabilityBoundingSet=",
    "RestrictNamespaces=yes",
    "RestrictAddressFamilies=AF_UNIX",
    "IPAddressDeny=any",
    *_GEN2_SANDBOX_BASELINE_DIRECTIVES,
    "DevicePolicy=closed",
    "DeviceAllow=/dev/kvm rw",
    "DeviceAllow=/dev/net/tun rw",
    "UMask=0077",
    "MemoryDenyWriteExecute=yes",
    f"ReadWritePaths=-{GEN2_BY_ORDINAL_DIR}/%i/run -{GEN2_BY_ORDINAL_DIR}/%i/disk.qcow2 -{GEN2_BY_ORDINAL_DIR}/%i/datadisk.qcow2",
)


@pure
def render_slice_unit_file() -> str:
    """The ``mngr-slice@.service`` template unit (installed once by prep).

    The instance parameter is the slice ORDINAL, which lets ``User=`` and every
    path use ``%i`` directly (systemd expands specifiers there, but not
    environment variables). qemu runs in the foreground under the per-slice
    user -- and that user's OWN groups (kvm comes from its supplementary
    groups; deliberately not ``Group=<service user>``, which would hand a VM escapee
    group access to every slice's files) -- with its seccomp sandbox on --
    valid here precisely because systemd means no ``-daemonize`` (the
    sandbox's ``spawn=deny`` kills the daemonize fork; spike-verified). The
    ``+`` prefixes run the net/user setup as root around the unprivileged VM
    process. qemu's one writable runtime file (its QMP socket) lives in the
    ``run/`` subdir the setup step creates for it; the systemd sandbox
    (:data:`GEN2_UNIT_SANDBOX_DIRECTIVES`) makes everything else read-only to
    it except the two disk images, and the guest console lands in journald.
    ``RequiresMountsFor`` on the storage root keeps a VM from ever starting
    against an absent (locked) storage volume: its disks live on the LUKS
    mapper mounted there, and a box whose TPM unlock failed at boot must
    leave its slices down rather than boot them off the bare mountpoint.
    """
    qemu_command = " \\\n    ".join(
        [
            "/usr/bin/qemu-system-x86_64",
            # No default devices (floppy, parallel, the default NIC/VGA/cdrom)
            # and no user config: the VM is exactly the devices listed here.
            "-nodefaults",
            "-no-user-config",
            "-m ${MNGR_SLICE_MEMORY_MIB}",
            # Nested virtualization is hidden from the guest (belt and braces
            # with the box's kvm nested=0 module option): an agent host has no
            # business running its own hypervisor.
            "-cpu host,vmx=off,svm=off",
            "-machine q35,accel=kvm,usb=off,smm=off",
            "-global ICH9-LPC.disable_s3=1",
            "-global ICH9-LPC.disable_s4=1",
            "-smp ${MNGR_SLICE_VCPUS}",
            f"-drive if=pflash,format=raw,readonly=on,file={GEN2_OVMF_CODE_PATH}",
            # cache=none (O_DIRECT): the guest already caches its own disks, so
            # host page cache for the images would only double-cache inside the
            # unit's memory cgroup and eat into MemoryMax; aio=native pairs with it.
            f"-drive file={GEN2_BY_ORDINAL_DIR}/%i/disk.qcow2,format=qcow2,if=none,discard=on,"
            "cache=none,aio=native,id=boot-disk",
            "-device virtio-blk-pci,drive=boot-disk,bootindex=1",
            f"-drive file={GEN2_BY_ORDINAL_DIR}/%i/datadisk.qcow2,format=qcow2,if=virtio,discard=on,"
            "cache=none,aio=native",
            # The cidata ISO as a read-only virtio-blk disk (no SCSI controller
            # or CD-ROM emulation in the VM); cloud-init finds the NoCloud
            # source by its filesystem label, not by device type.
            f"-drive file={GEN2_BY_ORDINAL_DIR}/%i/cidata.iso,format=raw,readonly=on,if=virtio",
            "-netdev tap,id=net0,ifname=${MNGR_SLICE_TAP},script=no,downscript=no",
            "-device virtio-net-pci,netdev=net0,mac=${MNGR_SLICE_MAC}",
            "-device virtio-rng-pci",
            "-display none",
            "-vga none",
            # The guest console goes to the unit's stdout, i.e. journald
            # (`journalctl -u mngr-slice@N`), where journald's rate limiting
            # bounds it; a console log file on the shared storage partition
            # would let a guest fill the box's disk by spamming its console.
            "-chardev stdio,id=ser0,signal=off",
            "-serial chardev:ser0",
            f"-qmp unix:{GEN2_BY_ORDINAL_DIR}/%i/run/qmp.sock,server=on,wait=off",
            "-sandbox on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny",
        ]
    )
    sandbox_directives = "\n".join(GEN2_UNIT_SANDBOX_DIRECTIVES)
    return f"""\
[Unit]
Description=mngr gen-2 slice VM (ordinal %i)
After=network-online.target {GEN2_DHCP_UNIT_NAME}
Wants=network-online.target {GEN2_DHCP_UNIT_NAME}
RequiresMountsFor={GEN2_STORAGE_ROOT}

[Service]
Type=simple
User=mngr-slice-%i
EnvironmentFile={GEN2_BY_ORDINAL_DIR}/%i/env
ExecStartPre=+{GEN2_HELPER_PATH} setup %i
ExecStart={qemu_command}
ExecStop=+{GEN2_HELPER_PATH} powerdown %i
ExecStopPost=+{GEN2_HELPER_PATH} teardown %i
TimeoutStopSec=180
Restart=no
{sandbox_directives}

[Install]
WantedBy=multi-user.target
"""


@pure
def render_slice_helper_script() -> str:
    """The root helper (``mngr-slice-helper``) the unit runs around each VM.

    Verbs (each takes the slice ordinal):

    - ``setup``: hand the VM's disk media (and a ``run/`` dir for its qmp
      socket) to its unix user -- the slice dir and env file stay the service user's,
      see below -- pin the unit's cgroup resource limits from the machine's
      size, create its tap + addressing + per-interface forwarding, install
      its nftables rules, and add its HTB class when an uplink rate is
      declared. The rules: DNAT in prerouting AND output (plus
      ``route_localnet`` on the tap and a local-source masquerade, so box-local
      connections to the forwarded ports -- loopback-addressed ones included --
      reach the VM), masquerade, the guest->box input drop (which exempts
      established/related replies to box-initiated connections, the guest's
      DHCP requests to the box's DHCP server on its tap, and ICMP to the
      gateway), the anti-spoofing drop and the direct-to-MX SMTP block, the
      connection ceilings, per-VM named counters, and the tc fair-share mark.
    - ``powerdown``: graceful QMP ``system_powerdown``, waiting for qemu to
      exit (systemd escalates per ``TimeoutStopSec`` if the guest ignores it).
    - ``teardown``: delete the slice's nftables rules (by comment tag), its tc
      class/filter, and its tap. Idempotent; tolerates everything being absent.

    All per-slice inputs come from the slice's env file, so the helper itself is
    generic and changes rarely (it is one of the few box-installed artifacts).
    The env file is NEVER sourced: this helper runs as root, and a VM escapee
    lands exactly in the slice's unix user, so every value is extracted with a
    strict-shape validator and anything the slice user could influence is
    refused rather than executed.
    """
    return f"""\
#!/bin/bash
# Managed by mngr (gen-2 slices). Root helper run by mngr-slice@.service.
set -euo pipefail

VERB="${{1:?usage: mngr-slice-helper <setup|powerdown|teardown> <ordinal>}}"
ORDINAL="${{2:?usage: mngr-slice-helper <setup|powerdown|teardown> <ordinal>}}"
case "$ORDINAL" in
''|*[!0-9]*) echo "ordinal must be numeric, got: $ORDINAL" >&2; exit 2 ;;
esac
ENV_FILE="{GEN2_BY_ORDINAL_DIR}/$ORDINAL/env"
NFT_TABLE="{GEN2_NFT_TABLE}"
RULE_TAG="mngr-slice-ord-$ORDINAL"

# Never source the env file: this helper runs as root and the file sits where
# the slice's own unix user could (via a VM escape) try to tamper with it.
# Each needed value is extracted here and refused unless it matches the strict
# shape the reserve script wrote.
env_get() {{
    local key="$1" pattern="$2" value
    value=$(grep -E "^$key=" "$ENV_FILE" | head -n 1 | cut -d= -f2-)
    # printf '%s\\n' so an empty value still forms a line for patterns that allow it.
    if ! printf '%s\\n' "$value" | grep -qE "^$pattern$"; then
        echo "refusing: $key in $ENV_FILE is missing or malformed" >&2
        exit 3
    fi
    printf '%s' "$value"
}}

uplink_interface() {{
    ip route | awk '/^default/ {{print $5; exit}}'
}}

delete_tagged_nft_rules() {{
    # Delete every rule carrying this slice's comment tag, chain by chain. The
    # listing tolerates an absent table (|| true, or pipefail+errexit would
    # abort the teardown verb, which promises to converge from any state).
    {{ nft -a list table inet "$NFT_TABLE" 2>/dev/null || true; }} \\
        | awk -v tag="$RULE_TAG" '
            /^\\tchain / {{chain=$2}}
            $0 ~ ("comment \\"" tag "\\"") {{
                for (i = 1; i <= NF; i++) if ($i == "handle") print chain, $(i + 1)
            }}' \\
        | while read -r chain handle; do
            nft delete rule inet "$NFT_TABLE" "$chain" handle "$handle"
        done
}}

case "$VERB" in
setup)
    # The user/tap names are pinned to the (systemd-supplied, trusted) ordinal;
    # everything else must be numeric (or dotted-numeric for the addresses).
    MNGR_SLICE_USER=$(env_get MNGR_SLICE_USER "mngr-slice-$ORDINAL")
    MNGR_SLICE_TAP=$(env_get MNGR_SLICE_TAP "mslice$ORDINAL")
    MNGR_SLICE_VM_IP=$(env_get MNGR_SLICE_VM_IP '[0-9.]+')
    MNGR_SLICE_GATEWAY_IP=$(env_get MNGR_SLICE_GATEWAY_IP '[0-9.]+')
    MNGR_SLICE_PREFIX_LENGTH=$(env_get MNGR_SLICE_PREFIX_LENGTH '[0-9]+')
    MNGR_SLICE_VM_SSH_HOST_PORT=$(env_get MNGR_SLICE_VM_SSH_HOST_PORT '[0-9]+')
    MNGR_SLICE_CONTAINER_SSH_HOST_PORT=$(env_get MNGR_SLICE_CONTAINER_SSH_HOST_PORT '[0-9]+')
    MNGR_SLICE_UNITS=$(env_get MNGR_SLICE_UNITS '[0-9]+')
    MNGR_SLICE_TOTAL_UNITS=$(env_get MNGR_SLICE_TOTAL_UNITS '[0-9]+')
    MNGR_SLICE_UPLINK_MBPS=$(env_get MNGR_SLICE_UPLINK_MBPS '[0-9]*')
    SLICE_DIR="{GEN2_BY_ORDINAL_DIR}/$ORDINAL"

    # Ownership: the slice dir and the env file stay the service user's -- the VM's own
    # unix user (where an escapee lands) must never be able to replace the env
    # file this helper and systemd consume. The VM's user gets only its disk
    # media, plus a setgid run/ dir for its qmp socket (group-owned by the service user so the
    # destroy's rm -rf can still clear it). Parent dirs are made
    # traversable-not-listable so qemu can open its exact paths.
    chmod 751 {GEN2_STORAGE_ROOT} {GEN2_INSTANCES_DIR} {GEN2_BY_ORDINAL_DIR}
    chown "{GEN2_SLICE_SERVICE_USER}:{GEN2_SLICE_SERVICE_USER}" "$SLICE_DIR/"
    chmod 751 "$SLICE_DIR/"
    chown "$MNGR_SLICE_USER:{GEN2_SLICE_SERVICE_USER}" \\
        "$SLICE_DIR"/disk.qcow2 "$SLICE_DIR"/datadisk.qcow2 "$SLICE_DIR"/cidata.iso
    chmod 660 "$SLICE_DIR"/disk.qcow2 "$SLICE_DIR"/datadisk.qcow2
    chmod 640 "$SLICE_DIR"/cidata.iso
    mkdir -p "$SLICE_DIR/run"
    chown "$MNGR_SLICE_USER:{GEN2_SLICE_SERVICE_USER}" "$SLICE_DIR/run"
    chmod 2770 "$SLICE_DIR/run"

    # cgroup resource limits from the machine's size. The unit file cannot
    # read env values into resource directives, so they are pinned here as
    # runtime properties (they live until the next daemon-reload/reboot, and
    # this step runs at every start). MemoryMax is a backstop that must never
    # fire -- the guest's own earlyoom/cgroup limits resolve pressure inside
    # the container, and an OOM kill of the whole VM is a telemetry signal
    # (SLICE_UNIT_OOM_KILLED). It is exactly the machine's budget share
    # (units x 1024 + the per-VM overhead), so the caps of a full box's machines
    # sum to the box budget and the box is never overcommitted; the guest is
    # booted GUEST_RAM_HOLDBACK_MIB below its units so qemu's own memory fits
    # under the cap, the disks bypass the host page cache (cache=none), guest
    # RAM never goes to the box swap, and there is no MemoryHigh throttling.
    # The CPU/IO weights are the machine's units-proportional share, scaled so
    # the default machine sits at systemd's default weight of 100.
    systemctl set-property --runtime "mngr-slice@$ORDINAL" \\
        "MemoryMax=$(( MNGR_SLICE_UNITS * 1024 + {PER_VM_RAM_OVERHEAD_MIB} ))M" \\
        "MemorySwapMax=0" \\
        "CPUWeight=$(( MNGR_SLICE_UNITS * 100 / {DEFAULT_MACHINE_UNITS} ))" \\
        "IOWeight=$(( MNGR_SLICE_UNITS * 100 / {DEFAULT_MACHINE_UNITS} ))" \\
        "TasksMax={GEN2_UNIT_TASKS_MAX}"

    # Routed tap: a point-to-point link owned by the slice user, the box-side
    # gateway address on it, and forwarding enabled on exactly the interfaces
    # involved (never the global toggle).
    if ! ip link show "$MNGR_SLICE_TAP" >/dev/null 2>&1; then
        ip tuntap add dev "$MNGR_SLICE_TAP" mode tap user "$MNGR_SLICE_USER"
    fi
    ip addr replace "$MNGR_SLICE_GATEWAY_IP/$MNGR_SLICE_PREFIX_LENGTH" dev "$MNGR_SLICE_TAP"
    ip link set "$MNGR_SLICE_TAP" up
    UPLINK="$(uplink_interface)"
    sysctl -q -w "net.ipv4.conf.$MNGR_SLICE_TAP.forwarding=1"
    sysctl -q -w "net.ipv4.conf.$UPLINK.forwarding=1"
    # Loopback-addressed connections (the image-cache transfer targets
    # 127.0.0.1:<vm_port>) are output-DNAT'd into the tap; their de-NAT'd
    # replies carry 127/8 addresses on the tap, which the kernel treats as
    # martian unless route_localnet is on for the interface. Scoped to this
    # tap only; it disappears with the tap at teardown.
    sysctl -q -w "net.ipv4.conf.$MNGR_SLICE_TAP.route_localnet=1"

    # The shared table + base chains (idempotent; -f with create guards).
    nft list table inet "$NFT_TABLE" >/dev/null 2>&1 || nft add table inet "$NFT_TABLE"
    nft list chain inet "$NFT_TABLE" prerouting >/dev/null 2>&1 \\
        || nft "add chain inet $NFT_TABLE prerouting {{ type nat hook prerouting priority dstnat; }}"
    nft list chain inet "$NFT_TABLE" output >/dev/null 2>&1 \\
        || nft "add chain inet $NFT_TABLE output {{ type nat hook output priority -100; }}"
    nft list chain inet "$NFT_TABLE" postrouting >/dev/null 2>&1 \\
        || nft "add chain inet $NFT_TABLE postrouting {{ type nat hook postrouting priority srcnat; }}"
    nft list chain inet "$NFT_TABLE" input >/dev/null 2>&1 \\
        || nft "add chain inet $NFT_TABLE input {{ type filter hook input priority filter; }}"
    nft list chain inet "$NFT_TABLE" forward >/dev/null 2>&1 \\
        || nft "add chain inet $NFT_TABLE forward {{ type filter hook forward priority filter; }}"

    # Per-VM named counters (created once; survive rule re-installation).
    nft list counter inet "$NFT_TABLE" "slice_${{ORDINAL}}_egress" >/dev/null 2>&1 \\
        || nft add counter inet "$NFT_TABLE" "slice_${{ORDINAL}}_egress"
    nft list counter inet "$NFT_TABLE" "slice_${{ORDINAL}}_ingress" >/dev/null 2>&1 \\
        || nft add counter inet "$NFT_TABLE" "slice_${{ORDINAL}}_ingress"
    nft list counter inet "$NFT_TABLE" "slice_${{ORDINAL}}_new_connections" >/dev/null 2>&1 \\
        || nft add counter inet "$NFT_TABLE" "slice_${{ORDINAL}}_new_connections"
    nft list counter inet "$NFT_TABLE" "slice_${{ORDINAL}}_smtp_blocked" >/dev/null 2>&1 \\
        || nft add counter inet "$NFT_TABLE" "slice_${{ORDINAL}}_smtp_blocked"

    # Re-install this slice's rules from scratch (idempotent across restarts).
    delete_tagged_nft_rules
    TC_MARK=$(( {GEN2_TC_MARK_BASE} + ORDINAL ))
    # Inbound: the two public forwarded ports DNAT to the VM. The output-hook
    # copies make box-local connections (e.g. the image-cache transfer) work,
    # since locally-generated traffic never traverses prerouting. `fib daddr
    # type local` keeps both hooks to traffic addressed to the box itself, so a
    # box-originated connection to some REMOTE host that happens to use a port
    # in the slice range is never redirected into a VM.
    for CHAIN in prerouting output; do
        nft add rule inet "$NFT_TABLE" "$CHAIN" fib daddr type local \\
            tcp dport "$MNGR_SLICE_VM_SSH_HOST_PORT" \\
            counter dnat ip to "$MNGR_SLICE_VM_IP:{GEN2_VM_SSH_GUEST_PORT}" comment "\\"$RULE_TAG\\""
        nft add rule inet "$NFT_TABLE" "$CHAIN" fib daddr type local \\
            tcp dport "$MNGR_SLICE_CONTAINER_SSH_HOST_PORT" \\
            counter dnat ip to "$MNGR_SLICE_VM_IP:{GEN2_CONTAINER_SSH_GUEST_PORT}" comment "\\"$RULE_TAG\\""
    done
    # Outbound: masquerade guest egress to the box's address.
    nft add rule inet "$NFT_TABLE" postrouting ip saddr "$MNGR_SLICE_VM_IP" oifname != "$MNGR_SLICE_TAP" \\
        counter masquerade comment "\\"$RULE_TAG\\""
    # Box-originated traffic entering the tap (the output-hook DNAT's flows) is
    # masqueraded to the tap's own address: a VM handed src 127.0.0.1 would
    # route its reply to itself. Locally-sourced only, so DNAT'd traffic
    # forwarded from real clients keeps its source address.
    nft add rule inet "$NFT_TABLE" postrouting oifname "$MNGR_SLICE_TAP" fib saddr type local \\
        counter masquerade comment "\\"$RULE_TAG\\""
    # Guest -> box-local: replies to box-initiated connections (the boot-wait
    # probe, the image-cache transfer), the guest's DHCP requests to the box's
    # DHCP server on this tap (the only guest-initiated traffic the box
    # answers: a fresh DISCOVER is ct state new from 0.0.0.0, so it needs its
    # own accept), and ICMP to the gateway (diagnostics); everything else
    # drops. This is the management-plane block -- the incident's exact vector,
    # a guest-INITIATED connection, opens as ct state new, hits the drop, and
    # so is never confirmed: only box-opened flows can ever reach the
    # established state this exemption matches. (That invariant lives in the
    # drop below, not in conntrack -- ct state is direction-agnostic, so never
    # add an accept ahead of the drop beyond the DHCP and ICMP allowances.)
    nft add rule inet "$NFT_TABLE" input iifname "$MNGR_SLICE_TAP" ct state established,related \\
        counter accept comment "\\"$RULE_TAG\\""
    nft add rule inet "$NFT_TABLE" input iifname "$MNGR_SLICE_TAP" \\
        udp sport {GEN2_DHCP_CLIENT_PORT} udp dport {GEN2_DHCP_SERVER_PORT} \\
        counter accept comment "\\"$RULE_TAG\\""
    nft add rule inet "$NFT_TABLE" input iifname "$MNGR_SLICE_TAP" \\
        ip daddr "$MNGR_SLICE_GATEWAY_IP" ip protocol icmp \\
        counter accept comment "\\"$RULE_TAG\\""
    nft add rule inet "$NFT_TABLE" input iifname "$MNGR_SLICE_TAP" \\
        counter drop comment "\\"$RULE_TAG\\""
    # Anti-spoofing: only the VM's own /30 address may enter from its tap.
    # Without this the masquerade above (which rewrites only saddr == vm_ip)
    # would forward spoofed-source packets verbatim, letting a guest join
    # reflection/amplification attacks. First among the forward rules so a
    # spoofed packet never reaches the ceilings, counters, or fair-share mark.
    # (IPv4-scoped is sufficient: the box never enables IPv6 forwarding.)
    nft add rule inet "$NFT_TABLE" forward iifname "$MNGR_SLICE_TAP" \\
        ip saddr != "$MNGR_SLICE_VM_IP" counter drop comment "\\"$RULE_TAG\\""
    # Direct-to-MX SMTP is blocked outright (the universal free-compute spam
    # control): every tenant shares the box's public IP via masquerade, so one
    # spammer poisons the address for the whole box and draws OVH abuse
    # action. Authenticated submission (587/465) stays open -- it needs relay
    # credentials, which is the spam-resistant path legitimate mail uses. The
    # named counter makes blocked attempts an abuse signal for the telemetry
    # pipeline.
    nft add rule inet "$NFT_TABLE" forward iifname "$MNGR_SLICE_TAP" tcp dport 25 \\
        counter name "slice_${{ORDINAL}}_smtp_blocked" drop comment "\\"$RULE_TAG\\""
    # Enforced ceilings (drop the excess new connections; established flows are
    # untouched), then inter-VM isolation: DNAT'd flows (public-port hairpin)
    # pass, direct tap-to-tap traffic does not.
    nft add rule inet "$NFT_TABLE" forward iifname "$MNGR_SLICE_TAP" ct state new \\
        ct count over {GEN2_MAX_CONCURRENT_CONNECTIONS} counter drop comment "\\"$RULE_TAG\\""
    nft add rule inet "$NFT_TABLE" forward iifname "$MNGR_SLICE_TAP" ct state new \\
        limit rate over {GEN2_MAX_NEW_CONNECTIONS_PER_SECOND}/second burst {GEN2_MAX_NEW_CONNECTIONS_PER_SECOND} packets \\
        counter drop comment "\\"$RULE_TAG\\""
    nft add rule inet "$NFT_TABLE" forward iifname "$MNGR_SLICE_TAP" oifname "mslice*" \\
        ct status dnat counter accept comment "\\"$RULE_TAG\\""
    nft add rule inet "$NFT_TABLE" forward iifname "$MNGR_SLICE_TAP" oifname "mslice*" \\
        counter drop comment "\\"$RULE_TAG\\""
    # Accounting + the fair-share mark tc classifies on.
    nft add rule inet "$NFT_TABLE" forward iifname "$MNGR_SLICE_TAP" ct state new \\
        counter name "slice_${{ORDINAL}}_new_connections" comment "\\"$RULE_TAG\\""
    nft add rule inet "$NFT_TABLE" forward iifname "$MNGR_SLICE_TAP" \\
        counter name "slice_${{ORDINAL}}_egress" meta mark set "$TC_MARK" comment "\\"$RULE_TAG\\""
    nft add rule inet "$NFT_TABLE" forward oifname "$MNGR_SLICE_TAP" \\
        counter name "slice_${{ORDINAL}}_ingress" comment "\\"$RULE_TAG\\""

    # Fair-share bandwidth: HTB classes on the uplink, classified by the mark
    # above. The root class runs at a fixed percentage of the declared uplink
    # so the shaper, not the NIC queue, is where packets wait (otherwise the
    # per-machine classes never get to arbitrate). Guaranteed share ~ shaped x
    # units/total_units (the machine's proportional slice of the box), borrow
    # to the full shaped rate when idle; non-guest traffic rides the default
    # class.
    # CLEANUP: drop the empty-value guard once every gen-2 slice carved before
    # uplink_mbps became mandatory has been restored (a restore re-renders the
    # env file with the box's uplink).
    if [ -n "$MNGR_SLICE_UPLINK_MBPS" ]; then
        if ! tc qdisc show dev "$UPLINK" | grep -q "htb 1:"; then
            tc qdisc add dev "$UPLINK" root handle 1: htb default 2
        fi
        SHAPED_MBIT=$(( MNGR_SLICE_UPLINK_MBPS * {GEN2_UPLINK_SHAPING_PERCENT} / 100 ))
        [ "$SHAPED_MBIT" -lt 1 ] && SHAPED_MBIT=1
        tc class replace dev "$UPLINK" parent 1: classid 1:1 htb \\
            rate "${{SHAPED_MBIT}}mbit" ceil "${{SHAPED_MBIT}}mbit"
        tc class replace dev "$UPLINK" parent 1:1 classid 1:2 htb \\
            rate "${{SHAPED_MBIT}}mbit" ceil "${{SHAPED_MBIT}}mbit"
        GUARANTEED_MBIT=$(( SHAPED_MBIT * MNGR_SLICE_UNITS / MNGR_SLICE_TOTAL_UNITS ))
        [ "$GUARANTEED_MBIT" -lt 1 ] && GUARANTEED_MBIT=1
        CLASS_MINOR=$(printf '%x' $(( 16 + ORDINAL )))
        tc class replace dev "$UPLINK" parent 1:1 classid "1:$CLASS_MINOR" htb \\
            rate "${{GUARANTEED_MBIT}}mbit" ceil "${{SHAPED_MBIT}}mbit"
        tc filter replace dev "$UPLINK" parent 1: protocol all prio 10 \\
            handle "$TC_MARK" fw classid "1:$CLASS_MINOR"
    fi
    ;;
powerdown)
    UNIT="mngr-slice@$ORDINAL"
    MAIN_PID="$(systemctl show -p MainPID --value "$UNIT")"
    SLICE_DIR="{GEN2_BY_ORDINAL_DIR}/$ORDINAL"
    if [ -S "$SLICE_DIR/run/qmp.sock" ]; then
        python3 - "$SLICE_DIR/run/qmp.sock" <<'QMP_POWERDOWN'
import json
import socket
import sys

qmp = socket.socket(socket.AF_UNIX)
qmp.settimeout(10)
qmp.connect(sys.argv[1])
stream = qmp.makefile("rw")
stream.readline()
stream.write(json.dumps({{"execute": "qmp_capabilities"}}) + "\\n")
stream.flush()
stream.readline()
stream.write(json.dumps({{"execute": "system_powerdown"}}) + "\\n")
stream.flush()
QMP_POWERDOWN
    fi
    # Wait for qemu to exit; if the guest ignores the ACPI signal, exiting here
    # lets systemd escalate (SIGTERM then SIGKILL) within TimeoutStopSec.
    if [ -n "$MAIN_PID" ] && [ "$MAIN_PID" != "0" ]; then
        for _ in $(seq 1 120); do
            kill -0 "$MAIN_PID" 2>/dev/null || exit 0
            sleep 1
        done
    fi
    ;;
teardown)
    delete_tagged_nft_rules
    TAP="mslice$ORDINAL"
    UPLINK="$(uplink_interface)"
    CLASS_MINOR=$(printf '%x' $(( 16 + ORDINAL )))
    TC_MARK=$(( {GEN2_TC_MARK_BASE} + ORDINAL ))
    tc filter del dev "$UPLINK" parent 1: protocol all prio 10 handle "$TC_MARK" fw 2>/dev/null || true
    tc class del dev "$UPLINK" parent 1:1 classid "1:$CLASS_MINOR" 2>/dev/null || true
    ip link del "$TAP" 2>/dev/null || true
    ;;
*)
    echo "unknown verb: $VERB" >&2
    exit 2
    ;;
esac
"""


# The exact systemctl verbs the slice service user may run on the per-ordinal slice units:
# the single source of truth for both the sudoers grants below and the box
# telemetry collector's sudo-anomaly allowlist (minds_admin's
# slices/box_telemetry.py), which must never drift from the grants.
GEN2_SLICE_SUDO_VERBS: Final[tuple[str, ...]] = ("start", "stop", "enable", "disable", "reset-failed")


@pure
def render_slice_sudoers() -> str:
    """The scoped sudoers entries: the slice service user may drive exactly the slice units, nothing else.

    Every grant is an exact-argument command spec, one line per ordinal: a
    sudoers ``*`` matches across whitespace, so a ``mngr-slice@*`` wildcard
    would also match ``stop mngr-slice@0 ssh.service`` and hand the service user every
    unit on the box. Flags would break sudoers argument matching too, so
    callers use separate ``stop`` + ``disable`` invocations rather than
    ``disable --now``.
    """
    lines = []
    for ordinal in range(GEN2_MAX_SLICE_COUNT):
        commands = ", ".join(f"/usr/bin/systemctl {verb} {slice_unit_name(ordinal)}" for verb in GEN2_SLICE_SUDO_VERBS)
        lines.append(f"{GEN2_SLICE_SERVICE_USER} ALL=(root) NOPASSWD: {commands}")
    return "\n".join(lines) + "\n"


@pure
def render_slice_dhcp_config() -> str:
    """The dnsmasq config for the box's slice DHCP server (installed once by prep).

    DHCP only (``port=0`` disables DNS entirely, so the box exposes no
    resolver), bound dynamically to the ``mslice*`` taps as the helper creates
    and deletes them (the DHCP socket itself is wildcard-bound on udp/67; the
    box-level policy of :func:`render_slice_dhcp_nftables_policy` keeps every
    other interface's udp/67 from ever reaching it). One single-address range
    per ordinal, each with the /30's netmask: dnsmasq picks the range by the
    subnet of the tap the request arrived on, so a guest can only ever be
    offered its own tap's address, whatever MAC it presents. The router option
    defaults to the tap's box-side address (the gateway); the public resolvers
    ride option 6. Leases are keyed by MAC (``dhcp-ignore-clid``): a re-carve
    of the same ordinal presents the same ordinal-derived MAC, so a stale lease
    from the previous VM never blocks the one address in its range.

    Two options exist so the server needs no capability beyond binding its
    port (:data:`GEN2_DHCP_UNIT_SANDBOX_DIRECTIVES`): ``dhcp-broadcast``
    answers an unconfigured guest by broadcast (each tap is a point-to-point
    link with one guest, so broadcast reaches exactly it), which is the only
    way to reply to a client that has no address yet without injecting its
    MAC into the ARP cache (``CAP_NET_ADMIN``); and ``no-ping`` skips the
    ICMP probe of a candidate address (``CAP_NET_RAW``), which a
    single-address range keyed to one MAC can never need.
    """
    range_lines = "\n".join(_render_dhcp_range_line(ordinal) for ordinal in range(GEN2_MAX_SLICE_COUNT))
    return f"""\
# Managed by mngr (gen-2 slices): DHCP for the slice taps. No DNS.
port=0
bind-dynamic
interface=mslice*
no-hosts
dhcp-authoritative
dhcp-ignore-clid
dhcp-broadcast
no-ping
dhcp-leasefile={GEN2_DHCP_LEASE_FILE_PATH}
dhcp-option=option:dns-server,{",".join(GEN2_GUEST_DNS_SERVERS)}
{range_lines}
"""


@pure
def _render_dhcp_range_line(ordinal: int) -> str:
    """The ordinal's single-address ``dhcp-range``, with its /30 netmask so dnsmasq matches it to the tap by subnet."""
    network = derive_slice_network(ordinal)
    netmask = ipaddress.IPv4Network((0, network.prefix_length)).netmask
    return f"dhcp-range={network.vm_ip},{network.vm_ip},{netmask},{GEN2_DHCP_LEASE_TIME}"


# The systemd sandbox the slice DHCP server runs in, the sibling of
# GEN2_UNIT_SANDBOX_DIRECTIVES. dnsmasq's reachable attack surface here is its
# DHCPv4 parser, fed by attacker-controlled guests, so a compromise must land
# in a process that can do nothing to the box:
#
# - User=/Group= start dnsmasq as the unprivileged service user outright, so
#   it is never root (dnsmasq only runs its own privilege-drop code when
#   started as root, so its ``user=`` option plays no part). The one
#   capability it still needs -- binding udp/67 -- arrives as an ambient
#   capability, and the bounding set holds nothing else: no CAP_NET_ADMIN
#   (the rendered config broadcasts to unconfigured guests instead of
#   injecting ARP entries) and no CAP_NET_RAW (no ICMP address probing), so a
#   compromised server cannot touch the mngr_slices rules, routes, or taps.
# - ProtectSystem=strict + StateDirectory: the whole filesystem is read-only
#   to it except its lease directory (systemd creates it owned by the service
#   user), which is where dnsmasq opens the lease file as that user.
# - RestrictAddressFamilies: the DHCP socket (AF_INET), the interface
#   tracking bind-dynamic does over netlink (AF_NETLINK), and syslog
#   (AF_UNIX). Nothing else; in particular no raw packet sockets.
# - IPAddressAllow/Deny: the unit's sockets may only exchange packets with
#   the slice /30s, the unconfigured-guest source (0.0.0.0) and the broadcast
#   address, so even a compromised dnsmasq cannot talk to the internet.
# - MemoryDenyWriteExecute holds because dnsmasq has no JIT.
# - SystemCallFilter=@system-service minus @privileged/@resources: running
#   unprivileged from the start, it never changes uid, groups or limits.
GEN2_DHCP_UNIT_SANDBOX_DIRECTIVES: Final[tuple[str, ...]] = (
    f"User={GEN2_DHCP_USER}",
    f"Group={GEN2_DHCP_USER}",
    "NoNewPrivileges=yes",
    "CapabilityBoundingSet=CAP_NET_BIND_SERVICE",
    "AmbientCapabilities=CAP_NET_BIND_SERVICE",
    "RestrictNamespaces=yes",
    "RestrictAddressFamilies=AF_INET AF_NETLINK AF_UNIX",
    f"IPAddressAllow={GEN2_SLICE_SUBNET_BASE}/16 0.0.0.0/32 255.255.255.255/32",
    "IPAddressDeny=any",
    *_GEN2_SANDBOX_BASELINE_DIRECTIVES,
    "PrivateDevices=yes",
    "UMask=0077",
    "MemoryDenyWriteExecute=yes",
    f"StateDirectory={GEN2_DHCP_STATE_DIRECTORY_NAME}",
)


@pure
def render_slice_dhcp_unit() -> str:
    """The ``mngr-slice-dhcp.service`` unit running dnsmasq in the foreground on the rendered config.

    Our own unit rather than the distro's ``dnsmasq.service`` (which reads
    ``/etc/dnsmasq.conf`` and would serve DNS on every interface with the
    package defaults): ``--conf-file`` pins exactly the rendered config, the
    config is syntax-checked before every start, and the slice units
    ``Wants=`` this one so a rebooted box never starts a VM before its DHCP
    server. It starts after ``nftables.service`` so the udp/67 policy
    (:func:`render_slice_dhcp_nftables_policy`) is loaded before the socket
    exists, and runs under :data:`GEN2_DHCP_UNIT_SANDBOX_DIRECTIVES`.
    """
    sandbox_directives = "\n".join(GEN2_DHCP_UNIT_SANDBOX_DIRECTIVES)
    return f"""\
[Unit]
Description=mngr gen-2 slice DHCP server (dnsmasq on the slice taps)
After=network-online.target nftables.service
Wants=network-online.target nftables.service

[Service]
Type=simple
ExecStartPre=/usr/sbin/dnsmasq --test --conf-file={GEN2_DHCP_CONFIG_PATH}
ExecStart=/usr/sbin/dnsmasq --keep-in-foreground --conf-file={GEN2_DHCP_CONFIG_PATH} --pid-file=
Restart=on-failure
RestartSec=2
{sandbox_directives}

[Install]
WantedBy=multi-user.target
"""


@pure
def render_slice_dhcp_nftables_policy() -> str:
    """The nftables policy keeping udp/67 off every interface but the slice taps (installed once by prep).

    dnsmasq binds udp/67 on the wildcard address and discards a packet from a
    non-listened interface only after receiving and parsing its header, so
    without this the public interface would feed the DHCP parser too. Its own
    table (never the slice helper's ``mngr_slices`` or the lockdown's
    ``mngr_mgmt``), boot-persistent through ``nftables.service`` exactly like
    the management policy, with the same add-then-delete-then-add pattern so
    re-loading the file converges. Only the server port is dropped: dnsmasq
    listens on 67 alone, so a udp/68 packet never reaches it, and the box's
    own uplink IS a DHCP client (the supplier hands out the public address by
    DHCP), whose replies arrive on udp/68 and must keep flowing.
    """
    return f"""\
#!/usr/sbin/nft -f
# Managed by mngr (gen-2 slice DHCP server; specs/slice-fleet-gen2).
# The slice DHCP server (dnsmasq, wildcard-bound on udp/67) answers only the
# slice taps: udp/67 arriving on any other interface drops here, before the
# socket ever receives it. Separate from the per-VM mngr_slices table and
# the management-plane mngr_mgmt table.
add table inet {GEN2_DHCP_NFT_TABLE}
delete table inet {GEN2_DHCP_NFT_TABLE}
add table inet {GEN2_DHCP_NFT_TABLE} {{
    chain input {{
        type filter hook input priority -10; policy accept;
        iifname != "mslice*" udp dport {GEN2_DHCP_SERVER_PORT} counter drop
    }}
}}
"""


# ---------------------------------------------------------------------------
# Caller-rendered box commands (carve reserve / start / boot wait / status)
# ---------------------------------------------------------------------------


@pure
def build_qemu_reserve_script(
    *,
    instance_name: str,
    boot_disk_gib: int,
    data_disk_gib: int,
    units: int,
    unit_budget_mib: int,
    disk_budget_gib: int,
    port_range_start: int,
    port_range_end: int,
    user_data_text: str,
    meta_data_text: str,
    network_config_text: str,
    env_file_template_text: str,
) -> str:
    """Render the bash that atomically reserves one gen-2 slice's budgets, ports, and ordinal.

    Run as a single SSH command on the box as the slice service user (the ``flock`` is
    released the instant the command exits). Under the lock it:

    1. enforces the two-budget capacity accounting (memory units and disk)
       against the recorded per-slice env files (the authoritative
       over-allocation guard);
    2. picks the lowest free ordinal from the recorded env files;
    3. picks two free host ports (bound TCP ports plus every recorded env
       file's ports);
    4. runs the carve-time df guard: refuses unless the storage filesystem's
       real free space covers the slice's full virtual size plus a margin;
    5. materializes the slice dir: reflink-copies the staged base image and
       resizes it, creates the data disk, builds the cidata ISO, writes the env
       file, links the ordinal, and ``systemctl enable``s the unit (registering
       boot autostart) WITHOUT starting it.

    The cidata files carry no placement (the guest is addressed by DHCP), so
    they are written as given; the one ordinal-dependent payload, the env
    file, ships as a single template whose MAC and /30 tokens the box-side
    script derives from the chosen ordinal with shell arithmetic and
    substitutes (plus the two chosen ports).

    Prints ``MNGR_SLICE_RESERVED <vm_port> <container_port> <ordinal>`` on
    success.
    """
    encoded_user_data = base64.b64encode(user_data_text.encode()).decode()
    encoded_meta_data = base64.b64encode(meta_data_text.encode()).decode()
    encoded_network_config = base64.b64encode(network_config_text.encode()).decode()
    encoded_env_file_template = base64.b64encode(env_file_template_text.encode()).decode()
    required_bytes = (boot_disk_gib + data_disk_gib + _DF_GUARD_MARGIN_GIB) * 1024**3
    budget_guard_lines = render_gen2_budget_guard_lines(
        units=units,
        data_disk_gib=data_disk_gib,
        unit_budget_mib=unit_budget_mib,
        disk_budget_gib=disk_budget_gib,
        excluded_instance_name="",
    )
    return f"""\
#!/bin/bash
set -euo pipefail
export PATH=/usr/local/bin:$HOME/.local/bin:$PATH

exec 9>"$HOME/{GEN2_ALLOC_LOCK_RELPATH}"
flock 9

INSTANCES_DIR={GEN2_INSTANCES_DIR}
BY_ORDINAL_DIR={GEN2_BY_ORDINAL_DIR}
BASE_IMAGE={GEN2_BASE_IMAGE_PATH}
if [ ! -f "$BASE_IMAGE" ]; then
    echo "gen-2 base image $BASE_IMAGE is not staged on this box; run server prep" >&2
    exit 6
fi
mkdir -p "$INSTANCES_DIR" "$BY_ORDINAL_DIR"

# 1. Two-budget capacity guard: memory units and disk, summed from the
# recorded env files (every env's machines count).
{budget_guard_lines}
# 2. Lowest free ordinal from the recorded env files.
used_ordinals=$(grep -sh '^MNGR_SLICE_ORDINAL=' "$INSTANCES_DIR"/*/env 2>/dev/null | cut -d= -f2 || true)
ordinal=""
for candidate in $(seq 0 {GEN2_MAX_SLICE_COUNT - 1}); do
    if ! printf '%s\\n' "$used_ordinals" | grep -qx "$candidate"; then
        ordinal="$candidate"
        break
    fi
done
if [ -z "$ordinal" ]; then
    echo "{GEN2_NO_UNITS_MARKER} no free ordinal" >&2
    exit 4
fi

# 3. Free host ports: bound TCP ports + every recorded env file's ports.
used_ports_file=$(mktemp)
trap 'rm -f "$used_ports_file"' EXIT
ss -Htln 2>/dev/null | awk '{{print $4}}' | sed 's/.*://' | grep -E '^[0-9]+$' >> "$used_ports_file" || true
grep -sh '_SSH_HOST_PORT=' "$INSTANCES_DIR"/*/env 2>/dev/null | cut -d= -f2 \\
    | grep -E '^[0-9]+$' >> "$used_ports_file" || true
pick_port() {{
    local p
    for ((p={port_range_start}; p<{port_range_end}; p++)); do
        if ! grep -qx "$p" "$used_ports_file"; then
            echo "$p"
            return 0
        fi
    done
    return 1
}}
vm_port=$(pick_port) || {{ echo "{GEN2_NO_PORTS_MARKER}" >&2; exit 3; }}
echo "$vm_port" >> "$used_ports_file"
container_port=$(pick_port) || {{ echo "{GEN2_NO_PORTS_MARKER}" >&2; exit 3; }}

# 4. The carve-time df guard: the budget math is the primary no-overcommit
# guarantee, but consumption outside its model (bases, transfer staging, leaks)
# must surface as a refusal here, never as a running VM's ENOSPC later.
available_bytes=$(df --output=avail -B1 "$INSTANCES_DIR" | tail -1 | tr -d ' ')
if [ "$available_bytes" -lt {required_bytes} ]; then
    echo "{GEN2_NO_SPACE_MARKER} available=$available_bytes required={required_bytes}" >&2
    exit 5
fi

# 5. Materialize the slice (0700 while it holds the cidata's embedded host
# private key; the unit's root setup step opens the dir to 751 and hands the
# VM's user only its disk media -- the dir and env stay the service user's).
{render_gen2_ordinal_derivation_lines()}
umask 077
slice_dir="$INSTANCES_DIR/{instance_name}"
mkdir "$slice_dir"
echo {shlex.quote(encoded_user_data)} | base64 -d > "$slice_dir/user-data"
echo {shlex.quote(encoded_meta_data)} | base64 -d > "$slice_dir/meta-data"
echo {shlex.quote(encoded_network_config)} | base64 -d > "$slice_dir/network-config"
echo {shlex.quote(encoded_env_file_template)} | base64 -d | substitute_ordinal_tokens \\
    | sed "s/{GEN2_VM_SSH_PORT_PLACEHOLDER}/$vm_port/g; s/{GEN2_CONTAINER_SSH_PORT_PLACEHOLDER}/$container_port/g" \\
    > "$slice_dir/env"
genisoimage -quiet -output "$slice_dir/cidata.iso" -volid cidata -joliet -rock \\
    "$slice_dir/user-data" "$slice_dir/meta-data" "$slice_dir/network-config"
cp --reflink=auto "$BASE_IMAGE" "$slice_dir/disk.qcow2"
qemu-img resize -q "$slice_dir/disk.qcow2" {boot_disk_gib}G
qemu-img create -q -f qcow2 "$slice_dir/datadisk.qcow2" {data_disk_gib}G
ln -sfn "$slice_dir" "$BY_ORDINAL_DIR/$ordinal"
sudo /usr/bin/systemctl enable "mngr-slice@$ordinal"

echo "{GEN2_RESERVED_MARKER} $vm_port $container_port $ordinal"
"""


@pure
def parse_gen2_reserved_line(stdout: str) -> tuple[int, int, int]:
    """Parse ``MNGR_SLICE_RESERVED <vm> <container> <ordinal>`` from a reserve run's stdout.

    Raises ``SliceReserveOutputError`` if the marker line is missing or malformed.
    """
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(GEN2_RESERVED_MARKER):
            parts = stripped.split()
            if len(parts) == 4 and all(part.isdigit() for part in parts[1:]):
                return int(parts[1]), int(parts[2]), int(parts[3])
            raise SliceReserveOutputError(f"malformed {GEN2_RESERVED_MARKER} line: {stripped!r}")
    raise SliceReserveOutputError(f"no {GEN2_RESERVED_MARKER} line in reserve output: {stdout[-500:]!r}")


@pure
def build_qemu_start_command(ordinal: int) -> str:
    """The box command that starts a reserved slice's unit (qemu launches; the guest boots on)."""
    return f"sudo /usr/bin/systemctl start {shlex.quote(slice_unit_name(ordinal))}"


@pure
def build_qemu_boot_wait_script(*, ordinal: int, timeout_seconds: int) -> str:
    """A box-side poll that waits for the guest's sshd banner on its routed address.

    Run on the box because the DNAT'd public port is the externally-visible
    surface while the tap address is the box's direct path -- polling the tap
    address needs no NAT and confirms the guest itself (not just the rules) is
    up. Exits 0 the moment a banner is served, 7 on timeout. The loop is
    deadlined on wall time (bash's SECONDS), not a round count: a round costs
    anywhere from ~5s to ~8s depending on whether the connect fails fast or
    eats its full 3s, and the client's SSH deadline is only slightly beyond
    ``timeout_seconds``, so the script must finish (and print its marker)
    within it.
    """
    network = derive_slice_network(ordinal)
    return f"""\
while [ "$SECONDS" -lt {timeout_seconds} ]; do
    if timeout 3 bash -c "exec 3<>/dev/tcp/{network.vm_ip}/{GEN2_VM_SSH_GUEST_PORT} && head -c 4 <&3" >/dev/null 2>&1; then
        exit 0
    fi
    sleep 5
done
echo "MNGR_SLICE_BOOT_TIMEOUT after {timeout_seconds}s" >&2
exit 7
"""


@pure
def build_qemu_status_command(instance_name: str) -> str:
    """The box command reporting a gen-2 slice's status: ``active``/``inactive``/``absent``.

    Prints ``absent`` when the instance dir does not exist, else the unit's
    ``systemctl is-active`` output (which is ``active`` for a running VM and
    ``inactive``/``failed`` for a stopped one).
    """
    quoted_dir = shlex.quote(gen2_instance_dir(instance_name))
    return (
        f"slice_dir={quoted_dir}; "
        f'if [ ! -d "$slice_dir" ]; then echo absent; '
        f'else ordinal=$(grep -s "^MNGR_SLICE_ORDINAL=" "$slice_dir/env" | cut -d= -f2); '
        f'systemctl is-active "mngr-slice@$ordinal" || true; fi'
    )
