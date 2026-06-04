from pathlib import Path

from pydantic import Field

from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import IdleMode
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr_sbx.constants import DEFAULT_SBX_AGENT_TYPE
from imbue.mngr_sbx.constants import SBX_BACKEND_NAME
from imbue.mngr_sbx.constants import SSH_CONNECT_TIMEOUT_SECONDS


class SbxProviderConfig(ProviderInstanceConfig):
    """Configuration for the Docker Sandboxes (sbx) provider backend."""

    backend: ProviderBackendName = Field(
        default=SBX_BACKEND_NAME,
        description="Provider backend (always 'sbx' for this type)",
    )
    host_dir: Path | None = Field(
        default=None,
        description="Base directory for mngr data inside sandboxes (defaults to /mngr)",
    )
    default_agent_type: str = Field(
        default=DEFAULT_SBX_AGENT_TYPE,
        description=(
            "The sbx agent type used when creating sandboxes. 'docker-agent' is the "
            "most generic option; mngr installs its own sshd inside and treats the "
            "sandbox as a regular SSH host."
        ),
    )
    default_template: str | None = Field(
        default=None,
        description=(
            "Optional sbx template (container image) to use for new sandboxes. "
            "When None, uses sbx's agent-default image."
        ),
    )
    default_cpus: int = Field(
        default=0,
        description="Default CPUs allocated to each sandbox. 0 means auto (N-1 host CPUs, min 1).",
    )
    default_memory: str | None = Field(
        default=None,
        description=("Default memory limit (e.g. '4g'). None lets sbx pick (50% of host memory, capped at 32 GiB)."),
    )
    default_start_args: tuple[str, ...] = Field(
        default=(),
        description="Default 'sbx create' arguments applied to all sandboxes.",
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
    ssh_connect_timeout: float = Field(
        default=SSH_CONNECT_TIMEOUT_SECONDS,
        description="Timeout in seconds for waiting for sshd to come up inside the sandbox",
    )
