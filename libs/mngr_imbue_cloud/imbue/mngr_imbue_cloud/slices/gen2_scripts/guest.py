import textwrap
from typing import Final

import yaml
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_GUEST_CONTAINERD_CONTENT_SUBVOLUME
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_GUEST_CONTAINERD_ROOT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_GUEST_CONTAINERD_SNAPSHOTS_SUBVOLUME
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_GUEST_DATA_FS_LABEL
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_GUEST_DATA_MOUNT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_GUEST_DOCKER_DATA_ROOT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_GUEST_HOST_QUOTA_QGROUP
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_HOST_ID_CONTAINER_LABEL
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import DATA_DISK_SYSTEM_RESERVE_GIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import SLICE_CONTAINER_MEMORY_RESERVE_MIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import SSH_CA_PRINCIPAL_VM
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import SSH_CA_ROOT_USER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import ssh_ca_trust_files

# In-guest sizing oneshots (installed by cloud-init; run on EVERY boot)

# The two root-owned every-boot units that make a machine's size self-applying
# with no management-plane access into the guest (specs/slice-fleet): the data
# filesystem grows to fill its (possibly qemu-img-resized) disk, and the agent
# host container's memory cap follows the VM's own RAM. Installed via
# cloud-init write_files at carve; being every-boot units, a restore or an
# in-place resize applies the new size on boot with no cloud-init involvement.
GUEST_GROW_DATA_FS_UNIT_NAME: Final[str] = "mngr-grow-data-fs.service"
GUEST_CONTAINER_MEMORY_UNIT_NAME: Final[str] = "mngr-reconcile-container-memory.service"
_GUEST_GROW_DATA_FS_SCRIPT_PATH: Final[str] = "/usr/local/sbin/mngr-grow-data-fs.sh"
_GUEST_CONTAINER_MEMORY_SCRIPT_PATH: Final[str] = "/usr/local/sbin/mngr-reconcile-container-memory.sh"


@pure
def render_guest_grow_data_fs_script() -> str:
    """The every-boot in-guest script that grows the data filesystem to fill its disk.

    Idempotent (``btrfs filesystem resize max`` is a no-op at full size) and
    tolerant of the very first boot, where the data disk is only mounted later
    by cloud-init's first-boot script. Covers both mount conventions: the
    gen-2 carve's fixed mount and a transplanted gen-1 disk's original lima
    mount point.
    """
    return f"""\
#!/bin/bash
# Managed by mngr (gen-2 slices).
set -euo pipefail
for mount_point in {GEN2_GUEST_DATA_MOUNT} /mnt/lima-*; do
    [ -d "$mount_point" ] || continue
    # Resize only btrfs mounts: the lima glob is broad, and only the data
    # filesystem should ever be grown.
    if mountpoint -q "$mount_point" && [ "$(findmnt -no FSTYPE "$mount_point")" = "btrfs" ]; then
        # A transplanted gen-1 data disk carries a partition table, which a
        # qcow2 grow does not touch -- grow the partition to fill the disk first.
        # growpart exits 1 (NOCHANGE) when already full-size; tolerated.
        source_device="$(findmnt -no SOURCE "$mount_point")"
        parent_disk="$(lsblk -no PKNAME "$source_device" | head -1)"
        if [ -n "$parent_disk" ] && command -v growpart >/dev/null 2>&1; then
            partition_number="$(printf %s "$source_device" | grep -o '[0-9]*$')"
            growpart "/dev/$parent_disk" "$partition_number" || true
        fi
        btrfs filesystem resize max "$mount_point"
        # The host quota follows the filesystem: one limit over everything
        # the host writes, the disk minus the system reserve, so a resize
        # grows it. Only on a quota-enabled data filesystem (gen-2 carves).
        if [ "$mount_point" = "{GEN2_GUEST_DATA_MOUNT}" ] && btrfs qgroup show "$mount_point" >/dev/null 2>&1; then
            fs_bytes="$(df -B1 --output=size "$mount_point" | tail -1 | tr -d ' ')"
            btrfs qgroup limit "$(( fs_bytes - {DATA_DISK_SYSTEM_RESERVE_GIB} * 1024 * 1024 * 1024 ))" {GEN2_GUEST_HOST_QUOTA_QGROUP} "$mount_point"
        fi
    fi
done
"""


@pure
def render_guest_grow_data_fs_unit() -> str:
    """The systemd unit for the data-filesystem grow, ordered before docker sees the mount."""
    return f"""\
[Unit]
Description=mngr: grow the agent host data filesystem to fill its disk
After=local-fs.target
Before=docker.service

[Service]
Type=oneshot
ExecStart={_GUEST_GROW_DATA_FS_SCRIPT_PATH}

[Install]
WantedBy=multi-user.target
"""


@pure
def render_guest_container_memory_script() -> str:
    """The every-boot in-guest script that follows the VM's RAM with the container's memory cap.

    Computes the cap from the guest's own visible RAM minus the VM-side reserve
    (mirroring the bake-time cap) and ``docker update``s every mngr-labeled
    container whose recorded cap differs. A failed update is logged, not fatal:
    the kernel can refuse to shrink a cgroup below its current usage, and the
    memory pressure resolves itself (earlyoom / the cgroup OOM killer) either way.
    """
    return f"""\
#!/bin/bash
# Managed by mngr (gen-2 slices).
set -euo pipefail
for _ in $(seq 1 30); do
    docker info >/dev/null 2>&1 && break
    sleep 2
done
if ! docker info >/dev/null 2>&1; then
    exit 0
fi
total_kib=$(awk '/^MemTotal:/{{print $2}}' /proc/meminfo)
cap_mib=$(( total_kib / 1024 - {SLICE_CONTAINER_MEMORY_RESERVE_MIB} ))
if [ "$cap_mib" -le 0 ]; then
    exit 0
fi
cap_bytes=$(( cap_mib * 1024 * 1024 ))
for container_id in $(docker ps -aq --filter "label={GEN2_HOST_ID_CONTAINER_LABEL}"); do
    current=$(docker inspect --format '{{{{.HostConfig.Memory}}}}' "$container_id")
    if [ "$current" != "$cap_bytes" ]; then
        docker update --memory "${{cap_mib}}m" --memory-swap "${{cap_mib}}m" "$container_id" \\
            || echo "WARNING: could not update the memory cap of $container_id" >&2
    fi
done
"""


@pure
def render_guest_container_memory_unit() -> str:
    """The systemd unit for the container-memory reconciler (needs a running dockerd)."""
    return f"""\
[Unit]
Description=mngr: reconcile the agent host container's memory cap with the VM's RAM
After=docker.service
Wants=docker.service

[Service]
Type=oneshot
ExecStart={_GUEST_CONTAINER_MEMORY_SCRIPT_PATH}

[Install]
WantedBy=multi-user.target
"""


@pure
def guest_sizing_oneshot_write_files() -> list[dict[str, str]]:
    """The cloud-init ``write_files`` entries installing the two sizing oneshots."""
    return [
        {
            "path": _GUEST_GROW_DATA_FS_SCRIPT_PATH,
            "permissions": "0755",
            "content": render_guest_grow_data_fs_script(),
        },
        {
            "path": f"/etc/systemd/system/{GUEST_GROW_DATA_FS_UNIT_NAME}",
            "permissions": "0644",
            "content": render_guest_grow_data_fs_unit(),
        },
        {
            "path": _GUEST_CONTAINER_MEMORY_SCRIPT_PATH,
            "permissions": "0755",
            "content": render_guest_container_memory_script(),
        },
        {
            "path": f"/etc/systemd/system/{GUEST_CONTAINER_MEMORY_UNIT_NAME}",
            "permissions": "0644",
            "content": render_guest_container_memory_unit(),
        },
    ]


@pure
def guest_sizing_oneshot_enable_command() -> list[str]:
    """The cloud-init ``runcmd`` entry that enables + first-runs the sizing oneshots."""
    return [
        "bash",
        "-c",
        "systemctl daemon-reload && "
        f"systemctl enable --now {GUEST_GROW_DATA_FS_UNIT_NAME} {GUEST_CONTAINER_MEMORY_UNIT_NAME}",
    ]


# Cloud-init material (generated once at carve; never replayed)


# The NoCloud files a gen-2 slice's cidata ISO is built from, in the order the
# reserve scripts write them. A gen-2 stop bundles them (alongside the env
# file) into the meta tar and a restore copies the three back verbatim.
GEN2_CIDATA_FILE_NAMES: Final[tuple[str, ...]] = ("user-data", "meta-data", "network-config")


class Gen2SliceCidata(FrozenModel):
    """The three NoCloud files of a gen-2 slice's cidata, supplied whole when no artifact meta tar carries them."""

    user_data: str = Field(description="The cloud-init user-data (root keys, pinned host key, first boot)")
    meta_data: str = Field(description="The meta-data with the slice's stable instance-id")
    network_config: str = Field(description="The placement-free DHCP network-config")

    def content_by_file_name(self) -> dict[str, str]:
        """The three files keyed by their NoCloud names, in ``GEN2_CIDATA_FILE_NAMES`` order."""
        return {"user-data": self.user_data, "meta-data": self.meta_data, "network-config": self.network_config}


@pure
def render_guest_data_disk_selection_lines() -> str:
    """The bash block that puts the data disk's device path in ``DATA_DEV`` (or exits 1).

    A formatted disk is found by its filesystem label (a restored or re-run
    boot). Otherwise the data disk is the blank one: the first ``disk``-type
    device that is not the root disk, is writable, and carries no filesystem.
    Both the read-only test and the filesystem test matter because the cidata
    ISO is also a virtio-blk disk (read-only, iso9660): "first non-root disk"
    would pick it and the mkfs would fail the boot.
    """
    return f"""\
DATA_DEV="$(blkid -L {GEN2_GUEST_DATA_FS_LABEL} 2>/dev/null || true)"
if [ -z "$DATA_DEV" ]; then
    DATA_ROOT_SRC="$(findmnt -no SOURCE /)"
    DATA_ROOT_DISK="$(lsblk -no PKNAME "$DATA_ROOT_SRC" | head -1)"
    if [ -z "$DATA_ROOT_DISK" ]; then
        echo "ERROR: could not determine root disk for $DATA_ROOT_SRC; refusing to format data disk" >&2
        exit 1
    fi
    for DATA_CANDIDATE in $(lsblk -dn -o NAME,TYPE | awk '$2=="disk"{{print $1}}'); do
        [ "$DATA_CANDIDATE" != "$DATA_ROOT_DISK" ] || continue
        [ "$(lsblk -dn -o RO "/dev/$DATA_CANDIDATE" | tr -d ' ')" = "0" ] || continue
        [ -z "$(lsblk -dn -o FSTYPE "/dev/$DATA_CANDIDATE" | tr -d ' ')" ] || continue
        DATA_DEV="/dev/$DATA_CANDIDATE"
        break
    done
fi
if [ -z "$DATA_DEV" ]; then
    echo "ERROR: no blank writable data disk (and no {GEN2_GUEST_DATA_FS_LABEL} filesystem) found to back {GEN2_GUEST_DATA_MOUNT}" >&2
    exit 1
fi
"""


@pure
def _build_firstboot_script(host_dir: str) -> str:
    """The one-time first-boot script: packages, /code, btrfs data disk, docker.

    The gen-2 base image pre-installs docker + the package set (via the prep
    stage's virt-customize), so the apt block is a presence-guarded fallback for
    images staged before a customization existed. The btrfs block mirrors the
    gen-1 lima provisioning: the data disk (selected by label, else the blank
    writable non-root disk) is formatted when not already btrfs (so re-runs and
    restored disks survive), mounted at a fixed path, and ``host_dir`` symlinked
    to it.
    """
    return f"""\
#!/bin/bash
set -eux -o pipefail

mkdir -p /run/sshd
mkdir -p /code && chmod 777 /code

# Fallback package install for base images staged without the preinstall
# customization; retried to ride out transient mirror failures.
apt_get_retry() {{
    local attempt
    for attempt in 1 2 3 4 5; do
        if apt-get "$@"; then
            return 0
        fi
        echo "apt-get $* failed (attempt $attempt/5); retrying in $((attempt * 5))s" >&2
        sleep "$((attempt * 5))"
    done
    return 1
}}
PKGS_TO_INSTALL=""
command -v tmux >/dev/null 2>&1 || PKGS_TO_INSTALL="$PKGS_TO_INSTALL tmux"
command -v git >/dev/null 2>&1 || PKGS_TO_INSTALL="$PKGS_TO_INSTALL git"
command -v jq >/dev/null 2>&1 || PKGS_TO_INSTALL="$PKGS_TO_INSTALL jq"
command -v rsync >/dev/null 2>&1 || PKGS_TO_INSTALL="$PKGS_TO_INSTALL rsync"
command -v curl >/dev/null 2>&1 || PKGS_TO_INSTALL="$PKGS_TO_INSTALL curl"
command -v xxd >/dev/null 2>&1 || PKGS_TO_INSTALL="$PKGS_TO_INSTALL xxd"
command -v flock >/dev/null 2>&1 || PKGS_TO_INSTALL="$PKGS_TO_INSTALL util-linux"
command -v mkfs.btrfs >/dev/null 2>&1 || PKGS_TO_INSTALL="$PKGS_TO_INSTALL btrfs-progs"
command -v inotifywait >/dev/null 2>&1 || PKGS_TO_INSTALL="$PKGS_TO_INSTALL inotify-tools"
test -x /usr/sbin/sshd || PKGS_TO_INSTALL="$PKGS_TO_INSTALL openssh-server"
test -f /etc/ssl/certs/ca-certificates.crt || PKGS_TO_INSTALL="$PKGS_TO_INSTALL ca-certificates"
if [ -n "$PKGS_TO_INSTALL" ]; then
    apt_get_retry update -qq
    apt_get_retry install -y -qq $PKGS_TO_INSTALL
fi

# Format + mount the btrfs data disk and symlink host_dir to it. Idempotent:
# mkfs only when the device is not already btrfs. Docker's data-root lives on
# this disk (as does containerd's root), so neither engine may run against the
# bare mountpoint: stop them (the image does not enable them at boot, but be
# safe) and drop anything they left on the root filesystem under the mountpoint.
if ! mountpoint -q {GEN2_GUEST_DATA_MOUNT}; then
    systemctl stop docker.socket docker.service containerd.service 2>/dev/null || true
    rm -rf {GEN2_GUEST_DOCKER_DATA_ROOT} {GEN2_GUEST_CONTAINERD_ROOT}
{textwrap.indent(render_guest_data_disk_selection_lines(), "    ")}\
    if ! blkid -t TYPE=btrfs "$DATA_DEV" >/dev/null 2>&1; then
        mkfs.btrfs -f -L {GEN2_GUEST_DATA_FS_LABEL} "$DATA_DEV"
    fi
    mkdir -p {GEN2_GUEST_DATA_MOUNT}
    mount "$DATA_DEV" {GEN2_GUEST_DATA_MOUNT}
fi
grep -q "^LABEL={GEN2_GUEST_DATA_FS_LABEL} " /etc/fstab || \\
    echo "LABEL={GEN2_GUEST_DATA_FS_LABEL} {GEN2_GUEST_DATA_MOUNT} btrfs defaults 0 0" >> /etc/fstab
chmod 0777 {GEN2_GUEST_DATA_MOUNT}
if [ -L {host_dir} ] || [ ! -e {host_dir} ]; then
    ln -sfn {GEN2_GUEST_DATA_MOUNT} {host_dir}
else
    rm -rf {host_dir}
    ln -sfn {GEN2_GUEST_DATA_MOUNT} {host_dir}
fi

# One quota over everything the agent host writes -- its home subvolume (joined
# at creation) and the image + container layers and content blobs in
# containerd's root -- limited by the grow oneshot to the disk minus the system
# reserve. The engines' own metadata, docker's build cache, the backup
# snapshots and btrfs metadata stay outside it, so a host that fills its quota
# never stops the engines or the snapshot helper. Simple quotas charge extents
# to their creating subvolume, so snapshots never leave the accounting
# inconsistent. The containerd dirs must exist as subvolumes before it first runs.
btrfs quota enable --simple {GEN2_GUEST_DATA_MOUNT}
btrfs qgroup create {GEN2_GUEST_HOST_QUOTA_QGROUP} {GEN2_GUEST_DATA_MOUNT} 2>/dev/null || true
[ -d {GEN2_GUEST_DOCKER_DATA_ROOT} ] || btrfs subvolume create {GEN2_GUEST_DOCKER_DATA_ROOT}
[ -d {GEN2_GUEST_CONTAINERD_ROOT} ] || btrfs subvolume create {GEN2_GUEST_CONTAINERD_ROOT}
for quota_subvolume in {GEN2_GUEST_CONTAINERD_SNAPSHOTS_SUBVOLUME} {GEN2_GUEST_CONTAINERD_CONTENT_SUBVOLUME}; do
    [ -d "$quota_subvolume" ] || btrfs subvolume create -i {GEN2_GUEST_HOST_QUOTA_QGROUP} "$quota_subvolume"
done
{_GUEST_GROW_DATA_FS_SCRIPT_PATH}

systemctl enable containerd docker 2>/dev/null || true
systemctl restart containerd docker
systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || true
"""


@pure
def build_qemu_slice_user_data(
    *,
    host_dir: str,
    root_authorized_public_keys: tuple[str, ...],
    host_private_key_pem: str,
    host_public_key_openssh: str,
    # The tier's SSH CA public key; when given, VM root trusts certificates
    # carrying the VM principal (the fleet's management access), and
    # ``root_authorized_public_keys`` need carry no management key at all.
    trusted_user_ca_public_key: str | None = None,
) -> str:
    """The NoCloud ``user-data``: root keys, the pinned sshd host key, sshd config, CA trust, first boot.

    ``ssh_keys`` installs exactly the pre-generated ed25519 host key mngr pins
    (strict host-key checking, no first-connect TOFU); ``ssh_genkeytypes:
    [ed25519]`` names only that type, so cloud-init generates nothing (the file
    already exists) and no other key type appears -- an empty list would mean
    the same but fails the cloud-config schema, leaving the guest "degraded";
    and ``ssh_deletekeys: false`` leaves the installed key alone. Because the instance-id is stable,
    none of this ever replays -- the key persists across every reboot.
    ``PerSourcePenalties no`` preserves gen-1 management-SSH semantics (all
    management connections share few source addresses; a boot-time auth timeout
    must not lock them all out); revisiting it is explicitly deferred by the
    gen-2 spec.
    """
    sshd_config = "\n".join(
        [
            "MaxSessions 100",
            "MaxStartups 100:30:200",
            "PermitRootLogin prohibit-password",
            "PerSourcePenalties no",
        ]
    )
    ca_trust_write_files = (
        [
            {"path": trust_file.path, "permissions": trust_file.mode, "content": trust_file.content}
            for trust_file in ssh_ca_trust_files(trusted_user_ca_public_key, {SSH_CA_ROOT_USER: SSH_CA_PRINCIPAL_VM})
        ]
        if trusted_user_ca_public_key is not None
        else []
    )
    # cloud-config's schema rejects an empty ssh_authorized_keys list, and a
    # gen-2 VM (CA trust only) authorizes no static key, so the key is omitted
    # rather than emitted empty.
    authorized_keys = [key.strip() for key in root_authorized_public_keys]
    root_user: dict[str, object] = {"name": "root"}
    if authorized_keys:
        root_user["ssh_authorized_keys"] = authorized_keys
    config = {
        "disable_root": False,
        "ssh_pwauth": False,
        "ssh_deletekeys": False,
        "ssh_genkeytypes": ["ed25519"],
        "ssh_keys": {
            "ed25519_private": host_private_key_pem,
            "ed25519_public": host_public_key_openssh.strip(),
        },
        "users": [root_user],
        "write_files": [
            {
                "path": "/etc/ssh/sshd_config.d/60-mngr.conf",
                "permissions": "0644",
                "content": sshd_config + "\n",
            },
            *ca_trust_write_files,
            {
                "path": "/usr/local/sbin/mngr-slice-firstboot.sh",
                "permissions": "0755",
                "content": _build_firstboot_script(host_dir),
            },
            *guest_sizing_oneshot_write_files(),
        ],
        "runcmd": [
            ["bash", "/usr/local/sbin/mngr-slice-firstboot.sh"],
            guest_sizing_oneshot_enable_command(),
        ],
    }
    return "#cloud-config\n" + yaml.safe_dump(config, default_flow_style=False, sort_keys=False)


@pure
def build_qemu_slice_meta_data(instance_name: str) -> str:
    """The NoCloud ``meta-data``: a STABLE instance-id, so cloud-init never replays.

    The stable id is the load-bearing difference from lima (which regenerated it
    every start, replaying every per-instance module each boot). Hostnames may
    not contain underscores (env names may), so those are mapped to hyphens.
    """
    hostname = instance_name.replace("_", "-")
    return f"instance-id: {instance_name}\nlocal-hostname: {hostname}\n"


# The guest's one NIC is matched by its driver, not its MAC: the MAC is
# ordinal-derived and changes when a restore lands on another ordinal, while
# this file is copied verbatim from the artifact and never re-rendered.
_GUEST_NIC_DRIVER: Final[str] = "virtio_net"
# The netplan id of that NIC. With a ``match`` block the id is only a label
# (it never names or renames the device), so it cannot go stale.
_GUEST_NIC_NETPLAN_ID: Final[str] = "slice-nic"


@pure
def build_qemu_slice_network_config() -> str:
    """The NoCloud ``network-config``: DHCP on the VM's single virtio NIC.

    Placement-free v2 config: the box-side DHCP server on the slice's tap hands
    out the ordinal's /30 address, the gateway (the tap's box-side address) and
    the public resolvers, so this file is identical for every slice and every
    placement, and a restore copies the artifact's own copy unchanged. The NIC
    keeps its kernel name (``enp0s2`` under the unit's fixed qemu argv): a
    ``set-name`` rename never took effect on the trixie guest (cloud-init hands
    the v2 config to netplan verbatim, and the rename is a udev-time action
    that has already passed by the time cloud-init renders the config), which
    left the driver-matched NIC unconfigured.
    """
    config = {
        "version": 2,
        "ethernets": {
            _GUEST_NIC_NETPLAN_ID: {
                "match": {"driver": _GUEST_NIC_DRIVER},
                "dhcp4": True,
            }
        },
    }
    return yaml.safe_dump(config, default_flow_style=False, sort_keys=False)
