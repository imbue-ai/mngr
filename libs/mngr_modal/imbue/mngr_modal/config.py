import os
from enum import auto
from pathlib import Path

from pydantic import AnyUrl
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import IdleMode
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import UserId

# Env var that supplies the connector ("imbue_cloud gateway") base URL for
# PROXIED mode. Falls back to the imbue_cloud var since it is the same gateway.
MODAL_CONNECTOR_URL_ENV_VAR = "MNGR__PROVIDERS__MODAL__CONNECTOR_URL"
_IMBUE_CLOUD_CONNECTOR_URL_ENV_VAR = "MNGR__PROVIDERS__IMBUE_CLOUD__CONNECTOR_URL"


class MissingModalConnectorUrlError(MngrError):
    """Raised when PROXIED mode has no connector URL (no field, no env)."""


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
    connector_url: AnyUrl | None = Field(
        default=None,
        description=(
            "Connector ('imbue_cloud gateway') base URL for PROXIED mode. When None, reads "
            f"${MODAL_CONNECTOR_URL_ENV_VAR} then ${_IMBUE_CLOUD_CONNECTOR_URL_ENV_VAR} from the "
            "environment (same gateway as imbue_cloud). Unused in DIRECT mode."
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
        description="Default base image (e.g., 'python:3.12-slim'). None uses debian_slim.",
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

    def get_connector_url(self) -> str:
        """Resolve the connector base URL for PROXIED mode.

        Precedence: per-instance ``connector_url`` field >
        ``$MNGR__PROVIDERS__MODAL__CONNECTOR_URL`` > the imbue_cloud connector
        var (same gateway). Raises :class:`MissingModalConnectorUrlError` if
        none is set.
        """
        if self.connector_url is not None:
            return str(self.connector_url).rstrip("/")
        for env_var in (MODAL_CONNECTOR_URL_ENV_VAR, _IMBUE_CLOUD_CONNECTOR_URL_ENV_VAR):
            env_value = os.environ.get(env_var)
            if env_value:
                return env_value.rstrip("/")
        raise MissingModalConnectorUrlError(
            "No connector URL configured for PROXIED Modal: set `connector_url` on the modal "
            f"provider config or export ${MODAL_CONNECTOR_URL_ENV_VAR}."
        )
