from typing import Final

from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderBackendName

LIMA_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("lima")

LIMA_INSTANCE_PREFIX: Final[str] = "mngr-"

# Minimum supported Lima version (major, minor, patch)
MINIMUM_LIMA_VERSION: Final[tuple[int, int, int]] = (1, 0, 0)

# Default image URLs for Lima VMs (Debian 12 "bookworm" genericcloud images).
# Debian's genericcloud variant is a minimal cloud image; the provisioning
# script is apt-based, so it works on Debian unchanged and installs any missing
# mngr dependencies. Pinned to a specific dated snapshot for reproducibility;
# bump the snapshot id (in both URLs) to pick up a newer point release.
DEFAULT_IMAGE_URL_AARCH64: Final[str] = (
    "https://cloud.debian.org/images/cloud/bookworm/20260601-2496/debian-12-genericcloud-arm64-20260601-2496.qcow2"
)
DEFAULT_IMAGE_URL_X86_64: Final[str] = (
    "https://cloud.debian.org/images/cloud/bookworm/20260601-2496/debian-12-genericcloud-amd64-20260601-2496.qcow2"
)

# Default host directory inside the VM
DEFAULT_HOST_DIR: Final[str] = "/mngr"

# SSH connection timeout when waiting for Lima VM to become reachable
SSH_CONNECT_TIMEOUT_SECONDS: Final[float] = 120.0

# cloud-init completion timeout
CLOUD_INIT_TIMEOUT_SECONDS: Final[float] = 300.0

# Default logical size of the btrfs additional disk. qcow2 is sparse so this
# is a logical cap visible to the guest, not upfront host disk usage.
DEFAULT_HOST_DATA_DISK_SIZE: Final[str] = "100GiB"


def lima_host_data_disk_mount_path(disk_name: str) -> str:
    """Return the in-VM path Lima auto-mounts an additional disk at.

    Lima's additionalDisks machinery generates a systemd mount unit that
    mounts each named disk under ``/mnt/lima-<disk_name>``. This is where
    host_dir symlinks in btrfs mode; no separate canonical bind-mount path
    is introduced, since adding one stacked an empty-ext4 layer underneath
    the btrfs mount when the bind unit fired before Lima's auto-mount.
    """
    return f"/mnt/lima-{disk_name}"


def lima_host_data_disk_name(host_id: HostId) -> str:
    """Return the Lima `additionalDisks` name for a host's btrfs data volume.

    Embeds the mngr `HostId` so the disk under `~/.lima/_disks/` is unique
    across every mngr-managed Lima host and can be cross-referenced via
    `limactl disk list`.
    """
    return f"mngr-{host_id.get_uuid().hex}-data"
