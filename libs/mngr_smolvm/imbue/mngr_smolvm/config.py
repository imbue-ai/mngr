from pathlib import Path

from pydantic import Field

from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import IdleMode
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr_smolvm.constants import DEFAULT_CPUS
from imbue.mngr_smolvm.constants import DEFAULT_HOST_DATA_DISK_SIZE_GB
from imbue.mngr_smolvm.constants import DEFAULT_MEMORY_MIB
from imbue.mngr_smolvm.constants import MINIMUM_SMOLVM_VERSION
from imbue.mngr_smolvm.constants import SMOLVM_BACKEND_NAME
from imbue.mngr_smolvm.constants import SSH_CONNECT_TIMEOUT_SECONDS


class SmolvmProviderConfig(ProviderInstanceConfig):
    """Configuration for the smolvm provider backend."""

    backend: ProviderBackendName = Field(
        default=SMOLVM_BACKEND_NAME,
        description="Provider backend (always 'smolvm' for this type)",
    )
    smolvm_command: str = Field(
        default="smolvm",
        description=(
            "Command used to invoke the smolvm CLI. Defaults to 'smolvm' on "
            "PATH; point this at a wrapper script or an absolute path to use "
            "a custom build (e.g. one with the btrfs-enabled guest kernel)."
        ),
    )
    host_dir: Path | None = Field(
        default=None,
        description="Base directory for mngr data inside VMs (defaults to /mngr)",
    )
    is_host_data_volume_exposed: bool = Field(
        default=True,
        description=(
            "Whether host_dir data is exposed back to the host machine via a "
            "virtiofs mount (the default). When True, the host process can "
            "read host_dir contents directly from "
            "~/.mngr/providers/smolvm/<name>/volumes/<host_id>/ even while "
            "the VM is stopped, and get_volume_for_host() returns a usable "
            "HostVolume. When False, mngr attaches a smolvm-managed btrfs "
            "data disk mounted at host_dir inside the VM; the host machine "
            "has no direct read path so get_volume_for_host() returns None. "
            "The False mode provides consistent btrfs snapshots of host_dir "
            "(required for agent-workspace backup flows) and requires a "
            "smolvm build with data-disk support."
        ),
    )
    host_data_disk_size_gb: int = Field(
        default=DEFAULT_HOST_DATA_DISK_SIZE_GB,
        description=(
            "Logical size (GiB) of the btrfs data disk used when "
            "is_host_data_volume_exposed=False. The backing image is sparse, "
            "so this is a logical cap visible to the guest, not upfront host "
            "disk usage."
        ),
    )
    default_cpus: int = Field(
        default=DEFAULT_CPUS,
        description="Default number of vCPUs for new machines",
    )
    default_memory_mib: int = Field(
        default=DEFAULT_MEMORY_MIB,
        description="Default memory (MiB) for new machines",
    )
    default_start_args: tuple[str, ...] = Field(
        default=(),
        description="Default extra arguments applied to every 'smolvm machine create'",
    )
    default_idle_timeout: int = Field(
        default=800,
        description="Default host idle timeout in seconds",
    )
    default_idle_mode: IdleMode = Field(
        default=IdleMode.IO,
        description="Default idle mode for hosts",
    )
    default_activity_sources: tuple[ActivitySource, ...] = Field(
        default_factory=lambda: tuple(ActivitySource),
        description="Default activity sources that count toward keeping host active",
    )
    minimum_smolvm_version: tuple[int, int, int] = Field(
        default=MINIMUM_SMOLVM_VERSION,
        description="Minimum required smolvm version as (major, minor, patch)",
    )
    ssh_connect_timeout: float = Field(
        default=SSH_CONNECT_TIMEOUT_SECONDS,
        description="Timeout in seconds for waiting for SSH to be ready on the VM",
    )
    vm_start_timeout_seconds: float = Field(
        default=120.0,
        description=(
            "Maximum time (in seconds) to wait for 'smolvm machine start' to "
            "bring a VM up. smolvm boots are sub-second once the image is "
            "local; the budget mostly covers first-boot image pulls."
        ),
    )
    provision_timeout_seconds: float = Field(
        default=300.0,
        description=(
            "Maximum time (in seconds) for the in-guest provisioning step "
            "(installing sshd and base packages via apk/apt on first boot)."
        ),
    )
