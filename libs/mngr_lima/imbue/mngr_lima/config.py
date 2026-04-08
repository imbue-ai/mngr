from pathlib import Path

from pydantic import Field

from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import IdleMode
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr_lima.constants import LIMA_BACKEND_NAME
from imbue.mngr_lima.constants import MINIMUM_LIMA_VERSION


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
