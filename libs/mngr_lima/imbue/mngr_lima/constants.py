from typing import Final

from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderBackendName

LIMA_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("lima")

LIMA_INSTANCE_PREFIX: Final[str] = "mngr-"

# Minimum supported Lima version (major, minor, patch)
MINIMUM_LIMA_VERSION: Final[tuple[int, int, int]] = (1, 0, 0)

# Default image URLs for Lima VMs (Ubuntu 24.04 LTS cloud images).
# wz/minds_onboard carries this Ubuntu pin: main switched to Debian 12
# bookworm assuming is_host_in_docker=True, but minds today runs with
# is_host_in_docker=False, and pilot_2's extra_provision_command (uv
# tool install / cpython 3.14.5) deterministically SIGILLs on the
# Debian 12 genericcloud base. Ubuntu 24.04 is the last known-good
# combo with pilot_2; revert when main lands a Debian-12-compatible
# pilot or when minds enables is_host_in_docker.
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

# Default logical size of the btrfs additional disk. qcow2 is sparse so this
# is a logical cap visible to the guest, not upfront host disk usage.
DEFAULT_HOST_DATA_DISK_SIZE: Final[str] = "100GiB"

# Default guest-internal port the agent container publishes its sshd on when
# is_host_in_docker=True. Mirrors the vps_docker provider's container_ssh_port.
DEFAULT_CONTAINER_SSH_PORT: Final[int] = 2222


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
