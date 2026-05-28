from typing import Final

from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderBackendName

LIMA_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("lima")

LIMA_INSTANCE_PREFIX: Final[str] = "mngr-"

# Minimum supported Lima version (major, minor, patch)
MINIMUM_LIMA_VERSION: Final[tuple[int, int, int]] = (1, 0, 0)

# Default image URLs for Lima VMs (Ubuntu 24.04 LTS cloud images).
# The cloud-init provisioning script installs any missing mngr dependencies.
DEFAULT_IMAGE_URL_AARCH64: Final[str] = (
    "https://cloud-images.ubuntu.com/releases/24.04/release/ubuntu-24.04-server-cloudimg-arm64.img"
)
DEFAULT_IMAGE_URL_X86_64: Final[str] = (
    "https://cloud-images.ubuntu.com/releases/24.04/release/ubuntu-24.04-server-cloudimg-amd64.img"
)

# Default host directory inside the VM
DEFAULT_HOST_DIR: Final[str] = "/mngr"

# SSH connection timeout when waiting for Lima VM to become reachable
SSH_CONNECT_TIMEOUT_SECONDS: Final[float] = 120.0

# cloud-init completion timeout
CLOUD_INIT_TIMEOUT_SECONDS: Final[float] = 300.0

# In-VM mount point at which the optional btrfs host-data volume is bind-mounted.
# When `is_host_data_volume_exposed=False`, `host_dir` (e.g. /mngr) becomes a
# symlink to this path. Naming mirrors Modal's `/host_volume` for symmetry with
# the other providers' host_volume_mount_path pattern.
HOST_VOLUME_MOUNT_PATH: Final[str] = "/mnt/host-volume"

# Default logical size of the btrfs additional disk. qcow2 is sparse so this
# is a logical cap visible to the guest, not upfront host disk usage.
DEFAULT_HOST_DATA_DISK_SIZE: Final[str] = "100GiB"


def lima_host_data_disk_name(host_id: HostId) -> str:
    """Return the Lima `additionalDisks` name for a host's btrfs data volume.

    Embeds the mngr `HostId` so the disk under `~/.lima/_disks/` is unique
    across every mngr-managed Lima host and can be cross-referenced via
    `limactl disk list`.
    """
    return f"mngr-{host_id.get_uuid().hex}-data"
