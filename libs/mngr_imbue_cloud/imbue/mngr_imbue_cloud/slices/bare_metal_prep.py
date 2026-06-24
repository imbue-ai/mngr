from typing import Final

from imbue.imbue_common.pure import pure
from imbue.mngr_imbue_cloud.slices.bare_metal import slice_base_image_path
from imbue.mngr_vps.host_setup import PINNED_DOCKER_APT_VERSION

# Lima release to install on the box (matches what the slice path is tested against).
DEFAULT_LIMA_VERSION: Final[str] = "2.1.2"

# Swapfile size (GiB) to provision on the box. Slice hosts run RAM near capacity, so a
# real swapfile is cheap OOM insurance against transient spikes (idle baked agents
# don't thrash steady-state). Replaces the OS-install default (two tiny ~0.5GiB swap
# partitions), which is too small to matter.
_SWAPFILE_SIZE_GIB: Final[int] = 32
_SWAPFILE_PATH: Final[str] = "/swapfile"

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
    limactl is only ever installed here, never invoked as root (lima refuses to run as
    root). Intended to be piped to ``sudo bash`` on the box.
    """
    apt_packages = " ".join(_BOX_APT_PACKAGES)
    lima_tarball = f"lima-{lima_version}-Linux-x86_64.tar.gz"
    lima_url = f"https://github.com/lima-vm/lima/releases/download/v{lima_version}/{lima_tarball}"
    base_image_path = slice_base_image_path(lima_service_user)
    swapfile_path = _SWAPFILE_PATH
    swapfile_size_gib = _SWAPFILE_SIZE_GIB
    return f"""\
#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# 1. System packages for QEMU/lima + the bake tooling.
apt-get update -qq
apt-get install -y -qq {apt_packages}

# 2. Install limactl (extract as root; never run limactl as root -- lima refuses).
if ! command -v limactl >/dev/null 2>&1; then
    curl -fsSL -o /tmp/{lima_tarball} {lima_url}
    tar -C /usr/local -xzf /tmp/{lima_tarball}
    rm -f /tmp/{lima_tarball}
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
apt-get install -y --allow-downgrades docker-ce="{PINNED_DOCKER_APT_VERSION}" docker-ce-cli="{PINNED_DOCKER_APT_VERSION}" containerd.io docker-buildx-plugin docker-compose-plugin inotify-tools
apt-get clean
rm -rf /var/lib/apt/lists/*
MNGR_SLICE_CUSTOMIZE
    virt-customize -a "$img.tmp" --network --run /tmp/mngr-slice-image-customize.sh
    rm -f /tmp/mngr-slice-image-customize.sh
    chown {lima_service_user}:{lima_service_user} "$img.tmp"
    mv "$img.tmp" "$img"
fi

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

echo MNGR_BOX_PREP_DONE
"""
