from typing import Final

from imbue.mngr.primitives import ProviderBackendName

LIMA_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("lima")

LIMA_INSTANCE_PREFIX: Final[str] = "mngr-"

# Minimum supported Lima version (major, minor, patch)
MINIMUM_LIMA_VERSION: Final[tuple[int, int, int]] = (1, 0, 0)

# Default image URLs for the pre-built mngr Lima image (Ubuntu LTS with mngr deps)
# These are hosted on GitHub Releases and downloaded on first use.
DEFAULT_IMAGE_URL_AARCH64: Final[str] = (
    "https://github.com/imbue-ai/mngr/releases/download/lima-image-v0.1.0/mngr-lima-aarch64.qcow2"
)
DEFAULT_IMAGE_URL_X86_64: Final[str] = (
    "https://github.com/imbue-ai/mngr/releases/download/lima-image-v0.1.0/mngr-lima-x86_64.qcow2"
)

# Default host directory inside the VM
DEFAULT_HOST_DIR: Final[str] = "/mngr"

# SSH connection timeout when waiting for Lima VM to become reachable
SSH_CONNECT_TIMEOUT_SECONDS: Final[float] = 120.0

# cloud-init completion timeout
CLOUD_INIT_TIMEOUT_SECONDS: Final[float] = 300.0
