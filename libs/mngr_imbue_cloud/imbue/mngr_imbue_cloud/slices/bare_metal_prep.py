from typing import Final

from imbue.imbue_common.pure import pure
from imbue.mngr_imbue_cloud.slices.bare_metal import SLICE_LIMA_INSTANCE_PREFIX
from imbue.mngr_imbue_cloud.slices.bare_metal import box_default_workspace_template_cache_dir
from imbue.mngr_imbue_cloud.slices.bare_metal import slice_base_image_path
from imbue.mngr_vps.host_setup import PINNED_CONTAINERD_APT_VERSION
from imbue.mngr_vps.host_setup import PINNED_DOCKER_APT_VERSION

# Lima release to install on the box. Must stay >= 2.2.0: earlier guestagents leak
# one goroutine + one socket FD per forwarded connection (portfwdserver's blocking
# closeCh, fixed upstream in the 2.2.0 release), which slowly wedged production
# slices. Independent of the desktop app's own lima pin (apps/minds/scripts/
# build.js), which is held back by a macOS-only usernet regression the box's
# qemu path does not use.
DEFAULT_LIMA_VERSION: Final[str] = "2.2.0"

# Swapfile size (GiB) to provision on the box. Slice hosts run RAM near capacity, so a
# real swapfile is cheap OOM insurance against transient spikes (idle baked agents
# don't thrash steady-state). Replaces the OS-install default (two tiny ~0.5GiB swap
# partitions), which is too small to matter.
_SWAPFILE_SIZE_GIB: Final[int] = 32
_SWAPFILE_PATH: Final[str] = "/swapfile"

# Pinned transfer-tool releases installed on every box for workspace
# stop/start: age encrypts/decrypts the artifact streams, s5cmd moves them
# to/from the tier's S3 bucket with parallel multipart transfers.
_AGE_VERSION: Final[str] = "1.2.1"
_S5CMD_VERSION: Final[str] = "2.3.0"

# How many slice VMs the boot autostart brings up concurrently. A full box
# cold-booting 14 QEMU VMs at once is a boot storm (each start waits on guest
# boot + lima requirement checks); fully serial keeps users down for ~10 minutes.
_SLICE_AUTOSTART_PARALLELISM: Final[int] = 4

# Packages the box needs to run lima/QEMU VMs and the slice bake (Docker lives
# inside each VM, not on the box). ``libguestfs-tools`` provides ``virt-customize``,
# used to pre-install Docker + inotify-tools into the golden slice image so per-VM
# first-boot provisioning skips those downloads.
_BOX_APT_PACKAGES: Final[tuple[str, ...]] = (
    "qemu-system-x86",
    "qemu-utils",
    "btrfs-progs",
    "rsync",
    "git",
    "curl",
    "ca-certificates",
    "iproute2",
    "libguestfs-tools",
)


@pure
def build_box_prep_script(
    *,
    pool_public_key: str,
    lima_service_user: str,
    lima_version: str,
    slice_base_image_url: str,
) -> str:
    """Render the idempotent root bash script that prepares a fresh Debian box to host slices.

    Installs QEMU + lima + tooling, creates the non-root ``lima_service_user`` (in
    the ``kvm`` group, with the pool management key authorized so the admin CLI and
    the connector can reach it), installs ``uv`` for that user, and stages the slice
    guest OS image (``slice_base_image_url``) once so VM boots never depend on the
    Debian mirror. The staged image is additionally customized (via ``virt-customize``)
    to pre-install the pinned Docker Engine + inotify-tools, so each slice VM's
    first-boot provisioning finds them present and skips the per-VM download/install.
    Also hardens the box against reboots: pins unattended-upgrades to never auto-reboot,
    and installs (enable-only) the ``mngr-slices-autostart.service`` boot unit that
    starts every stopped ``mngr-slice-*`` VM as the lima user after a box reboot.
    limactl is never invoked as root (lima refuses to run as root): prep itself only
    installs it, and the autostart script's limactl calls run later as the lima user
    via the unit's ``User=`` directive. Intended to be piped to ``sudo bash`` on the box.
    """
    apt_packages = " ".join(_BOX_APT_PACKAGES)
    lima_tarball = f"lima-{lima_version}-Linux-x86_64.tar.gz"
    lima_url = f"https://github.com/lima-vm/lima/releases/download/v{lima_version}/{lima_tarball}"
    base_image_path = slice_base_image_path(lima_service_user)
    default_workspace_template_cache_dir = box_default_workspace_template_cache_dir(lima_service_user)
    swapfile_path = _SWAPFILE_PATH
    swapfile_size_gib = _SWAPFILE_SIZE_GIB
    slice_autostart_parallelism = _SLICE_AUTOSTART_PARALLELISM
    age_version = _AGE_VERSION
    s5cmd_version = _S5CMD_VERSION
    return f"""\
#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# 1. System packages for QEMU/lima + the bake tooling.
apt-get update -qq
apt-get install -y -qq {apt_packages}

# 2. Install limactl (extract as root; never run limactl as root -- lima refuses,
#    so the installed release is tracked via a marker file instead of `limactl
#    --version`). Version-aware: re-running prep on a box whose installed lima
#    does not match the pinned release (including boxes prepped before the marker
#    existed) re-extracts the tarball, so a version bump reaches existing boxes.
lima_version_marker=/usr/local/share/lima/.mngr-installed-lima-version
if [ "$(cat "$lima_version_marker" 2>/dev/null)" != "{lima_version}" ]; then
    curl -fsSL -o /tmp/{lima_tarball} {lima_url}
    tar -C /usr/local -xzf /tmp/{lima_tarball}
    rm -f /tmp/{lima_tarball}
    mkdir -p /usr/local/share/lima
    printf '%s\\n' "{lima_version}" > "$lima_version_marker"
fi

# 2b. Transfer tooling for workspace stop/start: age (encryption) and s5cmd
#     (parallel S3 client), both pinned static binaries installed like limactl.
transfer_tools_marker=/usr/local/share/lima/.mngr-installed-transfer-tools
if [ "$(cat "$transfer_tools_marker" 2>/dev/null)" != "{age_version}-{s5cmd_version}" ]; then
    curl -fsSL -o /tmp/age.tar.gz https://github.com/FiloSottile/age/releases/download/v{age_version}/age-v{age_version}-linux-amd64.tar.gz
    tar -C /tmp -xzf /tmp/age.tar.gz
    install -m 755 /tmp/age/age /usr/local/bin/age
    install -m 755 /tmp/age/age-keygen /usr/local/bin/age-keygen
    rm -rf /tmp/age /tmp/age.tar.gz
    curl -fsSL -o /tmp/s5cmd.tar.gz https://github.com/peak/s5cmd/releases/download/v{s5cmd_version}/s5cmd_{s5cmd_version}_Linux-64bit.tar.gz
    tar -C /tmp -xzf /tmp/s5cmd.tar.gz s5cmd
    install -m 755 /tmp/s5cmd /usr/local/bin/s5cmd
    rm -f /tmp/s5cmd /tmp/s5cmd.tar.gz
    mkdir -p /usr/local/share/lima
    printf '%s\\n' "{age_version}-{s5cmd_version}" > "$transfer_tools_marker"
fi

# 3. Dedicated non-root service user that owns the lima VMs (kvm group for /dev/kvm).
if ! id {lima_service_user} >/dev/null 2>&1; then
    useradd -m -s /bin/bash {lima_service_user}
fi
usermod -aG kvm {lima_service_user}

# 4. Authorize the pool management key so the admin CLI + connector can SSH in as
#    this user (to bake slices and to tear them down via limactl on release).
install -d -m 700 -o {lima_service_user} -g {lima_service_user} /home/{lima_service_user}/.ssh
cat > /home/{lima_service_user}/.ssh/authorized_keys <<'MNGR_POOL_KEY'
{pool_public_key.strip()}
MNGR_POOL_KEY
chown {lima_service_user}:{lima_service_user} /home/{lima_service_user}/.ssh/authorized_keys
chmod 600 /home/{lima_service_user}/.ssh/authorized_keys

# 5. Install uv for the service user (used to run the vendored mngr that drives the bake).
sudo -u {lima_service_user} bash -lc 'command -v uv >/dev/null 2>&1 || curl -fsSL https://astral.sh/uv/install.sh | sh'

# 6. Stage + customize the golden slice guest image once (idempotent). Download the
#    base Debian qcow2, then pre-install the pinned Docker Engine + inotify-tools INTO
#    the image with virt-customize, so each slice VM's first-boot provisioning finds
#    them already present and skips the per-VM download/install (those guards are
#    presence-only). Customize the temp copy and only atomically move it into place on
#    success, so a partial/failed download or customize never becomes the base. Runs
#    as root (virt-customize needs /dev/kvm); the finished image is chowned to the lima
#    user that limactl reads it as. Referenced via file:// so VM boots never hit the
#    Debian mirror. To re-stage with a new customization, delete the image and re-run.
img={base_image_path}
# Create the image dir AND its parent (the user's ~/.cache) owned by the lima user.
# This script runs as root, so a freshly-created ~/.cache would be root-owned --
# which blocks `limactl` (run as the lima user) from creating ~/.cache/lima and fails
# every VM start. `install -d` only sets ownership on the leaf it's given, so create
# the whole chain and chown it (chown also repairs a ~/.cache left root-owned by an
# earlier prep run, since mkdir -p won't change an existing dir's ownership).
image_dir="$(dirname "$img")"
cache_dir="$(dirname "$image_dir")"
mkdir -p "$image_dir"
chown {lima_service_user}:{lima_service_user} "$cache_dir" "$image_dir"
chmod 755 "$cache_dir" "$image_dir"
if [ ! -f "$img" ]; then
    curl -fsSL --retry 8 --retry-delay 15 --retry-all-errors --retry-connrefused -o "$img.tmp" {slice_base_image_url}
    qemu-img info "$img.tmp" >/dev/null
    # In-guest customization run offline by virt-customize (so cloud-init still runs
    # fresh per VM). Installs the SAME pinned Docker (apt repo + exact =version) the
    # OVH path pins, plus inotify-tools, then trims apt caches to keep the image lean.
    # No systemctl here (no init in the appliance); the per-VM boot script enables +
    # starts docker. `set -eu` only (no pipefail): the appliance shell may be dash.
    cat > /tmp/mngr-slice-image-customize.sh <<'MNGR_SLICE_CUSTOMIZE'
set -eux
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl gnupg
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y --allow-downgrades docker-ce="{PINNED_DOCKER_APT_VERSION}" docker-ce-cli="{PINNED_DOCKER_APT_VERSION}" containerd.io="{PINNED_CONTAINERD_APT_VERSION}" docker-buildx-plugin docker-compose-plugin inotify-tools
apt-get clean
rm -rf /var/lib/apt/lists/*
MNGR_SLICE_CUSTOMIZE
    virt-customize -a "$img.tmp" --network --run /tmp/mngr-slice-image-customize.sh
    rm -f /tmp/mngr-slice-image-customize.sh
    chown {lima_service_user}:{lima_service_user} "$img.tmp"
    mv "$img.tmp" "$img"
fi

# 6b. Create the per-box DEFAULT_WORKSPACE_TEMPLATE image cache dir (owned by the lima user) where the box
#     keeps the single ``docker save`` tar slices ``docker load`` instead of rebuilding.
install -d -m 755 -o {lima_service_user} -g {lima_service_user} {default_workspace_template_cache_dir}

# 7. Provision a real swapfile (idempotent). Slice hosts run RAM near capacity; the
#    OS-install default swap (two ~0.5GiB partitions) is too small to cushion spikes.
if ! swapon --show=NAME --noheadings 2>/dev/null | grep -qx {swapfile_path}; then
    if [ ! -f {swapfile_path} ]; then
        fallocate -l {swapfile_size_gib}G {swapfile_path} || dd if=/dev/zero of={swapfile_path} bs=1M count=$(({swapfile_size_gib} * 1024))
        chmod 600 {swapfile_path}
        mkswap {swapfile_path}
    fi
    swapon {swapfile_path}
fi
grep -q "^{swapfile_path} " /etc/fstab || echo "{swapfile_path} none swap sw 0 0" >> /etc/fstab

# 7b. Retire the OS-install per-disk swap partitions (idempotent). They sit on raw
#     partitions OUTSIDE the md RAID mirrors, so when a disk dies its swapped-out
#     pages are gone and every process touching one gets SIGBUS -- which killed 13
#     of 14 slice VMs over several days in the 2026-08-07 production nvme failure.
#     Worse, the kernel activates them at boot before the swapfile, so their
#     default priorities make them the PREFERRED swap. All swap belongs on the
#     mirrored swapfile: turn the partitions off (swapoff migrates their few pages;
#     a failure here must fail prep loudly, not leave unmirrored swap in use),
#     drop them from fstab, and wipe their signatures so nothing re-activates them.
for swap_partition in $(swapon --show=NAME --noheadings 2>/dev/null | grep '^/dev/' || true); do
    swapoff "$swap_partition"
done
awk '!($3 == "swap" && $1 != "{swapfile_path}")' /etc/fstab > /etc/fstab.mngr-tmp && mv /etc/fstab.mngr-tmp /etc/fstab
for swap_partition in $(blkid -t TYPE=swap -o device 2>/dev/null || true); do
    wipefs -a "$swap_partition"
done

# 8. Pin unattended-upgrades to never reboot the box on its own. The Debian
#    default is already "false", but an explicit pin survives config drift (an
#    image or package update flipping it). Slice boxes host user workspaces:
#    kernels stage and activate at the next operator-scheduled reboot instead.
cat > /etc/apt/apt.conf.d/99mngr-no-auto-reboot <<'MNGR_NO_AUTO_REBOOT'
// Managed by mngr (bare_metal_prep): never let unattended-upgrades reboot a
// slice box on its own. Kernels stage and activate at the next operator-
// scheduled reboot; slices are user workspaces and must not restart unannounced.
Unattended-Upgrade::Automatic-Reboot "false";
MNGR_NO_AUTO_REBOOT

# 9. Boot autostart for the slice VMs. After a box reboot nothing else restarts
#    the lima VMs ({lima_service_user} has no linger session and lima has no boot
#    integration), so every workspace on the box stays down until an operator
#    intervenes. This unit starts all stopped slice VMs at boot as the lima user
#    (lima refuses root), with bounded parallelism and one retry per instance;
#    a VM that still fails start fails the unit so the breakage is visible in
#    systemd. Enable only (no start): with no reboot pending it must not run
#    now, and starting stopped-on-purpose VMs is the boot path's job alone.
#    In-VM recovery (workspace container + services agent) is handled inside
#    each VM by its own minds-autostart units.
cat > /usr/local/sbin/mngr-slices-autostart.sh <<'MNGR_SLICES_AUTOSTART'
#!/bin/bash
# Start every stopped mngr slice VM on this box (bounded parallelism, one retry
# each). Runs as the lima service user via mngr-slices-autostart.service.
set -euo pipefail
export PATH=/usr/local/bin:$PATH

start_one_slice() {{
    if limactl start "$1"; then
        return 0
    fi
    echo "first start of slice $1 failed; retrying" >&2
    limactl start "$1"
}}
export -f start_one_slice

# No stderr suppression and pipefail above: a failing listing must fail the
# unit (visible in systemd), not read as "no VMs to start".
# Skip VMs carrying the stop-requested marker: they were deliberately halted
# by a workspace stop (mid-upload) or are mid-restore, and must only ever be
# started by the connector's transition supervisor.
stopped_instances=$(limactl list --format '{{{{.Name}}}} {{{{.Status}}}}' \\
    | awk -v prefix="{SLICE_LIMA_INSTANCE_PREFIX}" 'index($1, prefix) == 1 && $2 == "Stopped" {{print $1}}' \\
    | while read -r name; do
        [ -e "$HOME/.lima/$name/mngr-stop-requested" ] || echo "$name"
    done)
if [ -z "$stopped_instances" ]; then
    echo "no stopped slice VMs to start"
    exit 0
fi
printf '%s\\n' "$stopped_instances" | xargs -n1 -P {slice_autostart_parallelism} bash -c 'start_one_slice "$1"' _
MNGR_SLICES_AUTOSTART
chmod +x /usr/local/sbin/mngr-slices-autostart.sh
cat > /etc/systemd/system/mngr-slices-autostart.service <<'MNGR_SLICES_UNIT'
[Unit]
Description=Start all mngr slice VMs on box boot
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
User={lima_service_user}
WorkingDirectory=/home/{lima_service_user}
ExecStart=/usr/local/sbin/mngr-slices-autostart.sh
TimeoutStartSec=45min

[Install]
WantedBy=multi-user.target
MNGR_SLICES_UNIT
systemctl daemon-reload
systemctl enable mngr-slices-autostart.service

echo MNGR_BOX_PREP_DONE
"""
