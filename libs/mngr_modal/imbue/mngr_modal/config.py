from enum import auto
from pathlib import Path
from typing import Final

from pydantic import Field
from pydantic import model_validator

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import ConfigStructureError
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import IdleMode
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import UserId

# Modal's hard cap on a sandbox's total lifetime, in seconds (24h). The timeout sent
# to Modal is default_sandbox_timeout + shutdown_buffer_seconds, which must not exceed
# this ceiling or Modal rejects the sandbox.
MODAL_MAX_SANDBOX_TIMEOUT_SECONDS: Final[int] = 86_400


class ModalMode(UpperCaseStrEnum):
    """How the modal provider backend talks to Modal.

    ``DIRECT`` uses the Modal SDK against the user's account.
    ``PROXIED`` routes Modal traffic through the imbue_cloud gateway.
    """

    DIRECT = auto()
    PROXIED = auto()


class ModalProviderConfig(ProviderInstanceConfig):
    """Configuration for the modal provider backend."""

    backend: ProviderBackendName = Field(
        default=ProviderBackendName("modal"),
        description="Provider backend (always 'modal' for this type)",
    )
    mode: ModalMode = Field(
        default=ModalMode.DIRECT,
        description=(
            "How to reach Modal. ``DIRECT`` uses the Modal SDK against the "
            "user's Modal account. ``PROXIED`` routes Modal traffic through the imbue_cloud gateway."
        ),
    )
    user_id: UserId | None = Field(
        default=None,
        description=(
            "Override the profile user_id for this provider instance. When set, this "
            "user_id is used instead of the profile's user_id for namespacing Modal "
            "resources (environments, apps, volumes). This allows sharing Modal resources "
            "across different mngr profiles or installations."
        ),
    )
    environment: str = Field(
        default="main",
        description="Modal environment name",
    )
    app_name: str | None = Field(
        default=None,
        description="Modal app name (defaults to 'mngr-{user_id}-{name}')",
    )
    host_dir: Path | None = Field(
        default=None,
        description="Base directory for mngr data on the sandbox (defaults to /mngr)",
    )
    default_sandbox_timeout: int = Field(
        default=86_310,
        description=(
            "Default sandbox timeout (graceful max host age) in seconds. The Modal sandbox is "
            "created with this PLUS shutdown_buffer_seconds, and Modal's hard cap is 86400s (24h); "
            "86_310 + 90 = 86_400 lands exactly at that ceiling. Lower it to tear sandboxes down "
            "sooner to save cost."
        ),
    )
    shutdown_buffer_seconds: int = Field(
        default=90,
        description=(
            "Buffer time added to the host shutdown timeout. This ensures the activity watcher can trigger a clean shutdown before a hard kill. The max_host_age in data.json is set to the original timeout (without buffer), so the host shuts down gracefully before the infrastructure-level timeout expires."
        ),
    )
    default_idle_timeout: int = Field(
        default=86_400,
        description=(
            "Default host idle timeout in seconds. Raised to 24h so an idle sandbox is not reaped "
            "mid-session; the create template also sets idle_mode=disabled for Modal, so in practice "
            "the sandbox runs until the 24h sandbox timeout."
        ),
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
        default=2.0,
        description="Default CPU cores. Matches the lima/docker convention (--cpus=2) so a Modal sandbox is sized like the other compute providers rather than the old 1-core minimum.",
    )
    default_memory: float = Field(
        default=4.0,
        description="Default memory in GB. Matches the lima/docker convention (--memory=4).",
    )
    default_gpu: str | None = Field(
        default=None,
        description="Default GPU type (e.g., 'h100', 'a10g'). None means no GPU.",
    )
    default_image: str | None = Field(
        default=None,
        description="Default base image (e.g., 'python:3.12-slim'). None uses debian_slim.",
    )
    default_region: str | None = Field(
        default=None,
        description="Default region (e.g., 'us-east'). None lets Modal choose.",
    )
    is_persistent: bool = Field(
        default=True,
        description=(
            "Whether the Modal app backing this provider is persistent. This is load-bearing, "
            "not a mere test marker: when True (default) the app is deployed/persistent, so its "
            "sandboxes outlive the mngr-create subprocess that created them. When False the Modal "
            "app is ephemeral and tied to the creating process -- the sandbox dies when the "
            "mngr-create subprocess exits. It is set False in tests so test resources are "
            "automatically torn down, but the consequence is real, not cosmetic."
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
    ssh_connect_timeout: float = Field(
        default=60.0,
        description="Timeout in seconds for waiting for sshd to be ready on the sandbox",
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

    @model_validator(mode="after")
    def _validate_timeout_within_modal_cap(self) -> "ModalProviderConfig":
        # The actual sandbox timeout sent to Modal is default_sandbox_timeout +
        # shutdown_buffer_seconds. Modal hard-caps a sandbox's total lifetime at
        # MODAL_MAX_SANDBOX_TIMEOUT_SECONDS (24h), so reject configs that would exceed it.
        total_timeout = self.default_sandbox_timeout + self.shutdown_buffer_seconds
        if total_timeout > MODAL_MAX_SANDBOX_TIMEOUT_SECONDS:
            raise ConfigStructureError(
                f"default_sandbox_timeout ({self.default_sandbox_timeout}) + shutdown_buffer_seconds "
                f"({self.shutdown_buffer_seconds}) = {total_timeout}s exceeds Modal's hard cap of "
                f"{MODAL_MAX_SANDBOX_TIMEOUT_SECONDS}s. Lower default_sandbox_timeout or "
                "shutdown_buffer_seconds so their sum is at most the cap."
            )
        return self
