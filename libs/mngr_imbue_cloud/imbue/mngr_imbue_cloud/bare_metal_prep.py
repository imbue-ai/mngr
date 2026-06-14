from typing import Final

from imbue.imbue_common.pure import pure
from imbue.mngr_imbue_cloud.bare_metal import slice_base_image_path

# Lima release to install on the box (matches what the slice path is tested against).
DEFAULT_LIMA_VERSION: Final[str] = "2.1.2"

# Packages the box needs to run lima/QEMU VMs and the slice bake (Docker lives
# inside each VM, not on the box).
_BOX_APT_PACKAGES: Final[tuple[str, ...]] = (
    "qemu-system-x86",
    "qemu-utils",
    "btrfs-progs",
    "rsync",
    "git",
    "curl",
    "ca-certificates",
    "iproute2",
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
    Debian mirror. limactl is only ever installed here, never invoked as root (lima
    refuses to run as root). Intended to be piped to ``sudo bash`` on the box.
    """
    apt_packages = " ".join(_BOX_APT_PACKAGES)
    lima_tarball = f"lima-{lima_version}-Linux-x86_64.tar.gz"
    lima_url = f"https://github.com/lima-vm/lima/releases/download/v{lima_version}/{lima_tarball}"
    base_image_path = slice_base_image_path(lima_service_user)
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

# 6. Stage the slice guest OS image once (idempotent). The slice bake references it
#    via file:// so VM boots never hit the Debian mirror (lima otherwise does a
#    per-boot last-modified HEAD that fatally fails when the mirror is flaky).
#    Download to a temp file, validate it is a real qcow2, then atomically move it
#    into place so a partial/corrupt fetch never becomes the base. Retries hard so a
#    flaky mirror at prep time still succeeds (prep is a one-time, re-runnable step).
sudo -u {lima_service_user} bash -lc 'set -euo pipefail
img={base_image_path}
mkdir -p "$(dirname "$img")"
if [ ! -f "$img" ]; then
    curl -fsSL --retry 8 --retry-delay 15 --retry-all-errors --retry-connrefused -o "$img.tmp" {slice_base_image_url}
    qemu-img info "$img.tmp" >/dev/null
    mv "$img.tmp" "$img"
fi'

echo MNGR_BOX_PREP_DONE
"""
