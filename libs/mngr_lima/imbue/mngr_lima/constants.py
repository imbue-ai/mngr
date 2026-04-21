from typing import Final

from imbue.mngr.primitives import ProviderBackendName

LIMA_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("lima")

LIMA_INSTANCE_PREFIX: Final[str] = "mngr-"

# Minimum supported Lima version (major, minor, patch)
MINIMUM_LIMA_VERSION: Final[tuple[int, int, int]] = (1, 0, 0)

# Default image URLs for Lima VMs (Ubuntu 24.04 LTS cloud images).
# The cloud-init provisioning script installs any missing mngr dependencies.
#
# When we publish a fat, self-contained mngr-lima qcow2 (built by
# scripts/build-lima-image.sh), flip these to the GitHub release URLs and set
# the matching DEFAULT_IMAGE_SHA256_* values below so Lima verifies them.
DEFAULT_IMAGE_URL_AARCH64: Final[str] = (
    "https://cloud-images.ubuntu.com/releases/24.04/release/ubuntu-24.04-server-cloudimg-arm64.img"
)
DEFAULT_IMAGE_URL_X86_64: Final[str] = (
    "https://cloud-images.ubuntu.com/releases/24.04/release/ubuntu-24.04-server-cloudimg-amd64.img"
)

# Expected SHA256 digest of each default image. None means "don't verify" (Lima
# will download but not check). Always pair a published custom image URL with a
# digest so tampering or partial downloads are rejected.
DEFAULT_IMAGE_SHA256_AARCH64: Final[str | None] = None
DEFAULT_IMAGE_SHA256_X86_64: Final[str | None] = None

# Default host directory inside the VM
DEFAULT_HOST_DIR: Final[str] = "/mngr"

# SSH connection timeout when waiting for Lima VM to become reachable
SSH_CONNECT_TIMEOUT_SECONDS: Final[float] = 120.0

# cloud-init completion timeout
CLOUD_INIT_TIMEOUT_SECONDS: Final[float] = 300.0
