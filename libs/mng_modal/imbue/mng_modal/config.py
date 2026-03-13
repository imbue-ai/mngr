from pathlib import Path

from pydantic import Field

from imbue.mng.config.data_types import ProviderInstanceConfig
from imbue.mng.primitives import ActivitySource
from imbue.mng.primitives import IdleMode
from imbue.mng.primitives import ProviderBackendName
from imbue.mng.primitives import UserId


class ModalProviderConfig(ProviderInstanceConfig):
    """Configuration for the modal provider backend."""

    backend: ProviderBackendName = Field(
        default=ProviderBackendName("modal"),
        description="Provider backend (always 'modal' for this type)",
    )
    user_id: UserId | None = Field(
        default=None,
        description=(
            "Override the profile user_id for this provider instance. When set, this "
            "user_id is used instead of the profile's user_id for namespacing Modal "
            "resources (environments, apps, volumes). This allows sharing Modal resources "
            "across different mng profiles or installations."
        ),
    )
    environment: str = Field(
        default="main",
        description="Modal environment name",
    )
    app_name: str | None = Field(
        default=None,
        description="Modal app name (defaults to 'mng-{user_id}-{name}')",
    )
    host_dir: Path | None = Field(
        default=None,
        description="Base directory for mng data on the sandbox (defaults to /mng)",
    )
    default_sandbox_timeout: int = Field(
        default=900,
        description="Default sandbox timeout in seconds",
    )
    shutdown_buffer_seconds: int = Field(
        default=90,
        description=(
            "Buffer time added to the host shutdown timeout. This ensures the activity watcher can trigger a clean shutdown before a hard kill. The max_host_age in data.json is set to the original timeout (without buffer), so the host shuts down gracefully before the infrastructure-level timeout expires."
        ),
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
    default_cpu: float = Field(
        default=1.0,
        description="Default CPU cores",
    )
    default_memory: float = Field(
        default=1.0,
        description="Default memory in GB",
    )
    default_gpu: str | None = Field(
        default=None,
        description="Default GPU type (e.g., 'h100', 'a10g'). None means no GPU.",
    )
    default_image: str | None = Field(
        default=None,
        description="Default base image (e.g., 'python:3.11-slim'). None uses debian_slim.",
    )
    default_region: str | None = Field(
        default=None,
        description="Default region (e.g., 'us-east'). None lets Modal choose.",
    )
    is_persistent: bool = Field(
        default=True,
        description=(
            "Configuration marker for Modal app persistence. When True (default), indicates "
            "the app is intended for production use. When False (set in tests), indicates "
            "the app is for testing and should be cleaned up. This field enables tests to "
            "signal their intent for easier identification and cleanup of test resources."
        ),
    )
    is_snapshotted_after_create: bool = Field(
        default=True,
        description=(
            "Whether to create an initial snapshot immediately after host creation. "
            "When True (default), an 'initial' snapshot is created, allowing the host "
            "to be restarted even if it's hard-killed. When False, the host can only "
            "be restarted if it was stopped gracefully (which creates a snapshot)."
        ),
    )
    is_host_volume_created: bool = Field(
        default=True,
        description=(
            "Whether to create and mount a persistent Modal Volume for the host directory. "
            "When True (default), a volume is created and the host directory is symlinked to it, "
            "so data (including logs) persists across sandbox restarts and is accessible when the "
            "host is offline. When False, no host volume is created; the host directory is a regular "
            "directory on the sandbox filesystem. Logs and other host data will only be available "
            "while the host is online."
        ),
    )
