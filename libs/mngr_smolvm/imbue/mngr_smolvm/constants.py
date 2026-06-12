from typing import Final

from imbue.mngr.primitives import ProviderBackendName

SMOLVM_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("smolvm")

# Minimum supported smolvm version (major, minor, patch).
MINIMUM_SMOLVM_VERSION: Final[tuple[int, int, int]] = (1, 0, 3)

# Default host directory inside the VM.
DEFAULT_HOST_DIR: Final[str] = "/mngr"

# SSH connection timeout when waiting for the VM's sshd to become reachable.
SSH_CONNECT_TIMEOUT_SECONDS: Final[float] = 120.0

# Default logical size (GiB) of the btrfs data disk backing host_dir when
# is_host_data_volume_exposed=False. The backing image is sparse, so this is
# a logical cap visible to the guest, not upfront host disk usage.
DEFAULT_HOST_DATA_DISK_SIZE_GB: Final[int] = 100

# Default VM resources, mirroring the lima provider's defaults.
DEFAULT_CPUS: Final[int] = 4
DEFAULT_MEMORY_MIB: Final[int] = 4096

# Guest path of the agent-watched sentinel file that triggers a clean VM
# shutdown (see smolvm's poweroff watcher).
POWEROFF_SENTINEL_PATH: Final[str] = "/run/smolvm/poweroff"
