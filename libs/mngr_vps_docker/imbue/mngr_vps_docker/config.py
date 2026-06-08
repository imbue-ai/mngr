from pathlib import Path

from pydantic import Field

from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import DockerBuilder
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
    default_os_id: int | str = Field(
        default=2136,
        description=(
            "Default VPS OS image identifier. Integer image ids are typical "
            "(e.g. 2136 = Debian 12 x64 on Vultr); providers like OVH classic "
            "VPS use string image names instead."
        ),
    )
    default_start_args: tuple[str, ...] = Field(
        default=(),
        description="Default docker run arguments applied to all containers",
    )
    docker_runtime: str | None = Field(
        default=None,
        description=(
            "Container runtime to pass to `docker run --runtime` (e.g. 'runsc' for gVisor). "
            "When None (the default), no `--runtime` flag is added and the VPS Docker daemon uses "
            "its configured default. The named runtime must be installed and registered on the VPS "
            "(see `install_gvisor_runtime`), otherwise container creation fails with Docker's native "
            "'unknown runtime' error. Override via MNGR__PROVIDERS__<NAME>__DOCKER_RUNTIME."
        ),
    )
    install_gvisor_runtime: bool = Field(
        default=False,
        description=(
            "When True, VPS provisioning installs and registers the gVisor `runsc` runtime with the "
            "Docker daemon (idempotent; a no-op when runsc is already present, e.g. baked into the "
            "image). This only installs the runtime -- set `docker_runtime='runsc'` to actually run "
            "containers under it."
        ),
    )
    builder: DockerBuilder = Field(
        default=DockerBuilder.DOCKER,
        description=(
            "Image builder used on the VPS. DOCKER (default) runs native `docker build` over SSH. "
            "DEPOT runs `depot build --load` over SSH, auto-installs the depot CLI on the VPS the "
            "first time, and requires DEPOT_TOKEN in the agent's environment (DEPOT_PROJECT_ID "
            "optional, only forwarded when set)."
        ),
    )
    btrfs_mount_path: Path = Field(
        default=Path("/mngr-btrfs"),
        description=(
            "Path on the outer where the loop-mounted btrfs filesystem holding the per-host "
            "unified docker volume is mounted. The per-host subvolume lives at "
            "``<btrfs_mount_path>/<host_id_hex>`` and is bound into the agent container via "
            "``docker volume create --opt device=...``."
        ),
    )
    btrfs_loop_file_path: Path = Field(
        default=Path("/var/lib/mngr-btrfs.img"),
        description=(
            "Path on the outer's root filesystem where the loop-backed btrfs image file is "
            "stored. Allocated with ``fallocate`` and mounted via an ``/etc/fstab`` entry so "
            "it survives VPS reboots."
        ),
    )
    outer_disk_reserved_gb: int = Field(
        default=20,
        description=(
            "Gigabytes of free space on the outer's root filesystem to hold back from the "
            "btrfs loop file at provisioning time. Loop file size is computed as "
            "``free_gb - outer_disk_reserved_gb``; ``VpsProvisioningError`` is raised when "
            "the result is not positive."
        ),
    )
