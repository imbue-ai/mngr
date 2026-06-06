from pathlib import Path

from pydantic import Field

from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import DockerBuilder
from imbue.mngr.primitives import IdleMode
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr_lima.constants import DEFAULT_CONTAINER_SSH_PORT
from imbue.mngr_lima.constants import DEFAULT_HOST_DATA_DISK_SIZE
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
            "intended for consistent btrfs snapshots of host_dir."
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
    is_host_in_docker: bool = Field(
        default=False,
        description=(
            "When False (default), mngr runs the agent directly inside the Lima "
            "VM (today's behavior). When True, the VM is provisioned only with "
            "Docker + btrfs + a snapshot helper, and the agent runs inside a "
            "Docker container in the VM (built from the project's Dockerfile, "
            "exactly like the docker/vps_docker providers). mngr treats the "
            "container as the host: ssh and all agent work happen inside it, and "
            "Lima forwards the container's sshd out to the host's localhost. "
            "This mode forces the btrfs additional-disk layout "
            "(is_host_data_volume_exposed must be False) so the per-host data "
            "lives on a snapshottable filesystem."
        ),
    )
    container_ssh_port: int = Field(
        default=DEFAULT_CONTAINER_SSH_PORT,
        description=(
            "Guest-internal TCP port the agent container publishes its sshd on "
            "(bound to the VM's loopback). Lima forwards this to a unique "
            "host-side port. Only used when is_host_in_docker=True."
        ),
    )
    default_image: str = Field(
        default="debian:bookworm-slim",
        description=(
            "Default container base image used when is_host_in_docker=True and "
            "no Dockerfile build args are supplied. Ignored in direct-in-VM mode."
        ),
    )
    builder: DockerBuilder = Field(
        default=DockerBuilder.DOCKER,
        description="Image builder to use when building the container image inside the VM (DOCKER or DEPOT).",
    )
    docker_install_timeout: float = Field(
        default=600.0,
        description=(
            "Timeout in seconds for pulling the agent container's base image inside the VM. Only used when "
            "is_host_in_docker=True and no Dockerfile build args are supplied (the no-build pull path)."
        ),
    )
    container_ssh_connect_timeout: float = Field(
        default=180.0,
        description="Timeout in seconds for waiting for the container's sshd to become reachable via the forwarded port.",
    )
    image_build_timeout_seconds: float = Field(
        default=1800.0,
        description=(
            "Timeout in seconds for building the container image inside the VM. The default (30 min) is generous "
            "because the project Dockerfile is built in-VM on a cold layer cache."
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
    docker_runtime: str | None = Field(
        default=None,
        description=(
            "Container runtime to pass to `docker run --runtime` for the agent container in "
            "is_host_in_docker mode (e.g. 'runsc' for gVisor). When None (the default), no "
            "`--runtime` flag is added and the in-VM Docker daemon uses its configured default. "
            "The named runtime must be installed and registered inside the VM (see "
            "`install_gvisor_runtime`), otherwise container creation fails with Docker's native "
            "'unknown runtime' error. Override via MNGR__PROVIDERS__<NAME>__DOCKER_RUNTIME. "
            "Ignored when is_host_in_docker is False (no container is run)."
        ),
    )
    install_gvisor_runtime: bool = Field(
        default=False,
        description=(
            "When True, the is_host_in_docker VM provisioning installs and registers the gVisor "
            "`runsc` runtime with the in-VM Docker daemon (idempotent; a no-op when runsc is already "
            "present). This only installs the runtime -- set `docker_runtime='runsc'` to actually run "
            "the agent container under it. Ignored when is_host_in_docker is False."
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
