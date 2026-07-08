from pathlib import Path
from typing import Self

from pydantic import Field
from pydantic import model_validator

from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import IdleMode
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr_lima.constants import DEFAULT_HOST_DATA_DISK_SIZE
from imbue.mngr_lima.constants import LIMA_BACKEND_NAME
from imbue.mngr_lima.constants import MINIMUM_LIMA_VERSION
from imbue.mngr_lima.errors import LimaConfigError


class LimaProviderConfig(ProviderInstanceConfig):
    """Configuration for the Lima provider backend."""

    backend: ProviderBackendName = Field(
        default=LIMA_BACKEND_NAME,
        description="Provider backend (always 'lima' for this type)",
    )
    host_dir: Path | None = Field(
        default=None,
        description="Base directory for mngr data inside VMs (defaults to /mngr)",
    )
    is_host_data_volume_exposed: bool = Field(
        default=True,
        description=(
            "Whether host_dir data is exposed back to the host machine via a 9p "
            "bind mount (today's default behavior). When True, the host process "
            "can read host_dir contents directly from "
            "~/.mngr/providers/lima/<name>/volumes/<host_id>/ even while the VM "
            "is stopped, and get_volume_for_host() returns a usable HostVolume. "
            "When False, mngr attaches a dedicated Lima-managed btrfs "
            "additionalDisk to the VM and symlinks host_dir into it; the host "
            "machine has no direct read path so get_volume_for_host() returns "
            "None and mngr event / mngr transcript against a stopped Lima host "
            "stops working until the host is started. The False mode is "
            "intended for consistent btrfs snapshots of host_dir, and is "
            "required when is_run_as_root=True."
        ),
    )
    host_data_disk_size: str = Field(
        default=DEFAULT_HOST_DATA_DISK_SIZE,
        description=(
            "Logical size of the btrfs additional disk used when "
            "is_host_data_volume_exposed=False. qcow2 is sparse, so this is "
            "a logical cap visible to the guest, not upfront host disk usage. "
            "Format follows Lima's size string (e.g. '100GiB')."
        ),
    )
    is_run_as_root: bool = Field(
        default=False,
        description=(
            "When True, mngr runs the agent in the VM as root (uid 0), matching "
            "the docker/vps_docker providers where the agent is root inside its "
            "container: the agent can apt-install and write anywhere with no "
            "sudo. mngr injects a root client key and SSHes in as root. This "
            "requires the btrfs additional-disk layout "
            "(is_host_data_volume_exposed must be False), because root cannot "
            "traverse the 9p/reverse-sshfs bind mount the exposed layout uses. "
            "When False (today's default), the agent runs as the Lima default "
            "user with passwordless sudo."
        ),
    )
    default_image_url_aarch64: str | None = Field(
        default=None,
        description="Default qcow2 image URL for aarch64. None uses the mngr default.",
    )
    default_image_url_x86_64: str | None = Field(
        default=None,
        description="Default qcow2 image URL for x86_64. None uses the mngr default.",
    )
    default_start_args: tuple[str, ...] = Field(
        default=(),
        description="Default limactl start arguments applied to all VMs",
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
    minimum_lima_version: tuple[int, int, int] = Field(
        default=MINIMUM_LIMA_VERSION,
        description="Minimum required Lima version as (major, minor, patch)",
    )
    ssh_connect_timeout: float = Field(
        default=120.0,
        description="Timeout in seconds for waiting for SSH to be ready on the VM",
    )
    vm_start_timeout_seconds: float = Field(
        default=600.0,
        description=(
            "Maximum time (in seconds) to wait for `limactl start` to finish a "
            "fresh VM bring-up before aborting. The default (10 min) is "
            "generous for KVM-accelerated boots; environments without KVM "
            "(e.g. modal sandboxes running qemu in TCG mode) often need 20+ "
            "minutes and should bump this. Independent of the lima-side "
            "`--timeout` start arg, which controls Lima's own internal "
            "abort -- both have to be wide enough."
        ),
    )

    @model_validator(mode="after")
    def _validate_run_as_root_requires_btrfs_layout(self) -> Self:
        # Root cannot traverse the 9p/reverse-sshfs bind mount the exposed
        # layout uses, so running the agent as root requires the btrfs
        # additional-disk layout. Fail fast at config construction.
        if self.is_run_as_root and self.is_host_data_volume_exposed:
            raise LimaConfigError(
                "providers.lima.is_run_as_root=True requires the btrfs additional-disk layout; "
                "set providers.lima.is_host_data_volume_exposed=false."
            )
        return self
