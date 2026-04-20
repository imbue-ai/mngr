from pathlib import Path

from pydantic import Field

from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import IdleMode


class VpsDockerProviderConfig(ProviderInstanceConfig):
    """Base configuration for VPS Docker providers."""

    host_dir: Path = Field(
        default=Path("/mngr"),
        description="Base directory for mngr data inside containers",
    )
    default_image: str = Field(
        default="debian:bookworm-slim",
        description="Default Docker image for containers",
    )
    default_idle_timeout: int = Field(
        default=800,
        description="Default idle timeout in seconds",
    )
    default_idle_mode: IdleMode = Field(
        default=IdleMode.IO,
        description="Default idle detection mode",
    )
    default_activity_sources: tuple[ActivitySource, ...] = Field(
        default_factory=lambda: tuple(ActivitySource),
        description="Default activity sources",
    )
    ssh_connect_timeout: float = Field(
        default=60.0,
        description="Timeout for SSH connections in seconds",
    )
    vps_boot_timeout: float = Field(
        default=300.0,
        description="Timeout for VPS to become active after provisioning in seconds",
    )
    docker_install_timeout: float = Field(
        default=300.0,
        description="Timeout for Docker installation on the VPS in seconds",
    )
    container_ssh_port: int = Field(
        default=2222,
        description="Port for sshd inside the Docker container (mapped to VPS localhost only)",
    )
    default_region: str = Field(
        default="ewr",
        description="Default VPS region",
    )
    default_plan: str = Field(
        default="vc2-1c-1gb",
        description="Default VPS plan (CPU/RAM specification)",
    )
    default_os_id: int = Field(
        default=2136,
        description="Default VPS OS image ID (2136 = Debian 12 x64)",
    )
    default_start_args: tuple[str, ...] = Field(
        default=(),
        description="Default docker run arguments applied to all containers",
    )
