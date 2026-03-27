from pathlib import Path

from pydantic import Field

from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import IdleMode
from imbue.mngr.primitives import ProviderBackendName


class DockerProviderConfig(ProviderInstanceConfig):
    """Configuration for the docker provider backend."""

    backend: ProviderBackendName = Field(
        default=ProviderBackendName("docker"),
        description="Provider backend (always 'docker' for this type)",
    )
    host: str = Field(
        default="",
        description=(
            "Docker host URL (e.g., 'ssh://user@server', 'tcp://host:2376'). Empty string means local Docker daemon."
        ),
    )
    host_dir: Path | None = Field(
        default=None,
        description="Base directory for mngr data inside containers (defaults to /mngr)",
    )
    default_image: str | None = Field(
        default=None,
        description="Default base image. None uses debian:bookworm-slim.",
    )
    default_start_args: tuple[str, ...] = Field(
        default=(),
        description="Default docker run arguments applied to all containers (e.g., '--cpus=2', '--memory=4g')",
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
    is_host_volume_created: bool = Field(
        default=True,
        description=(
            "Whether to mount a persistent volume for the host directory. "
            "When True, the host_dir inside each container is backed by a "
            "sub-folder of the shared Docker named volume, making data "
            "accessible even when the container is stopped."
        ),
    )
