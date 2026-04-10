from typing import Final

from imbue.mngr.primitives import ProviderBackendName

LIMA_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("lima")

LIMA_INSTANCE_PREFIX: Final[str] = "mngr-"

# Minimum supported Lima version (major, minor, patch)
MINIMUM_LIMA_VERSION: Final[tuple[int, int, int]] = (1, 0, 0)

# Default image URLs for Lima VMs (Alpine 3.23 cloud images).
# Alpine is minimal and boots fast. The cloud-init provisioning script
# installs any missing mngr dependencies via apk.
DEFAULT_IMAGE_URL_AARCH64: Final[str] = (
    "https://dl-cdn.alpinelinux.org/alpine/v3.23/releases/cloud/nocloud_alpine-3.23.3-aarch64-uefi-cloudinit-r0.qcow2"
)
DEFAULT_IMAGE_URL_X86_64: Final[str] = (
    "https://dl-cdn.alpinelinux.org/alpine/v3.23/releases/cloud/nocloud_alpine-3.23.3-x86_64-uefi-cloudinit-r0.qcow2"
)

# Default host directory inside the VM
DEFAULT_HOST_DIR: Final[str] = "/mngr"

# SSH connection timeout when waiting for Lima VM to become reachable
SSH_CONNECT_TIMEOUT_SECONDS: Final[float] = 120.0

# cloud-init completion timeout
CLOUD_INIT_TIMEOUT_SECONDS: Final[float] = 300.0
