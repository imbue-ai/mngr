import json
import os
import shutil
import tempfile
import time
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr
from pyinfra.api import Host as PyinfraHost

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import MngrError
from imbue.mngr.hosts.common import check_agent_type_known
from imbue.mngr.hosts.common import compute_idle_seconds
from imbue.mngr.hosts.common import determine_lifecycle_state
from imbue.mngr.hosts.common import resolve_expected_process_name
from imbue.mngr.hosts.common import timestamp_to_datetime
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.hosts.offline_host import derive_offline_host_state
from imbue.mngr.hosts.offline_host import validate_and_create_discovered_agent
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import CpuResources
from imbue.mngr.interfaces.data_types import HostDetails
from imbue.mngr.interfaces.data_types import HostLifecycleOptions
from imbue.mngr.interfaces.data_types import HostResources
from imbue.mngr.interfaces.data_types import PyinfraConnector
from imbue.mngr.primitives import SSHInfo
from imbue.mngr.interfaces.data_types import SnapshotInfo
from imbue.mngr.interfaces.data_types import SnapshotRecord
from imbue.mngr.interfaces.data_types import VolumeInfo
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import IdleMode
from imbue.mngr.primitives import ImageReference
from imbue.mngr.primitives import LogLevel
from imbue.mngr.primitives import SnapshotId
from imbue.mngr.primitives import SnapshotName
from imbue.mngr.primitives import VolumeId
from imbue.mngr.providers.base_provider import BaseProviderInstance
from imbue.mngr.providers.listing_utils import build_listing_collection_script
from imbue.mngr.providers.listing_utils import parse_listing_collection_output
from imbue.mngr.providers.ssh_host_setup import build_add_authorized_keys_command
from imbue.mngr.providers.ssh_host_setup import build_add_known_hosts_command
from imbue.mngr.providers.ssh_host_setup import build_check_and_install_packages_command
from imbue.mngr.providers.ssh_host_setup import build_configure_ssh_command
from imbue.mngr.providers.ssh_host_setup import build_start_activity_watcher_command
from imbue.mngr.providers.ssh_utils import add_host_to_known_hosts
from imbue.mngr.providers.ssh_utils import create_pyinfra_host
from imbue.mngr.providers.ssh_utils import load_or_create_host_keypair
from imbue.mngr.providers.ssh_utils import load_or_create_ssh_keypair
from imbue.mngr.providers.ssh_utils import wait_for_sshd
from imbue.mngr_vps_docker.cloud_init import generate_cloud_init_user_data
from imbue.mngr_vps_docker.config import VpsDockerProviderConfig
from imbue.mngr_vps_docker.docker_over_ssh import DockerOverSsh
from imbue.mngr_vps_docker.errors import ContainerSetupError
from imbue.mngr_vps_docker.errors import DockerNotReadyError
from imbue.mngr_vps_docker.errors import VpsConnectionError
from imbue.mngr_vps_docker.host_store import CONTAINER_ENTRYPOINT_CMD
from imbue.mngr_vps_docker.host_store import VpsDockerHostRecord
from imbue.mngr_vps_docker.host_store import VpsDockerHostStore
from imbue.mngr_vps_docker.host_store import VpsHostConfig
from imbue.mngr_vps_docker.host_store import ensure_state_container
from imbue.mngr_vps_docker.primitives import VpsInstanceId
from imbue.mngr_vps_docker.vps_client import VpsClientInterface


def _remove_host_from_known_hosts(known_hosts_path: Path, hostname: str, port: int) -> None:
    """Remove a host entry from the known_hosts file."""
    if not known_hosts_path.exists():
        return
    host_pattern = hostname if port == 22 else f"[{hostname}]:{port}"
    lines = known_hosts_path.read_text().splitlines(keepends=True)
    filtered = [line for line in lines if not line.startswith(f"{host_pattern} ")]
    known_hosts_path.write_text("".join(filtered))


class _ParsedVpsBuildOptions(FrozenModel):
    """Result of parsing VPS-specific build args from Docker build args."""

    region: str = Field(description="VPS region")
    plan: str = Field(description="VPS plan")
    os_id: int = Field(description="VPS OS image ID")
    git_depth: int | None = Field(default=None, description="Git clone depth for build context, or None for full clone")
    docker_build_args: tuple[str, ...] = Field(description="Remaining args passed to docker build")


def _parse_build_args(
    build_args: Sequence[str] | None,
    *,
    default_region: str,
    default_plan: str,
    default_os_id: int,
) -> _ParsedVpsBuildOptions:
    """Parse build args, separating VPS provisioning args from Docker build args.

    VPS-specific args use the --vps- prefix (e.g., --vps-region=ewr).
    ``--git-depth=N`` controls the git clone depth for the build context.
    Everything else is passed through to docker build on the VPS.
    """
    region = default_region
    plan = default_plan
    os_id = default_os_id
    git_depth: int | None = None
    docker_build_args: list[str] = []

    if build_args:
        for arg in build_args:
            if arg.startswith("--vps-region="):
                region = arg.split("=", 1)[1]
            elif arg.startswith("--vps-plan="):
                plan = arg.split("=", 1)[1]
            elif arg.startswith("--vps-os="):
                os_id = int(arg.split("=", 1)[1])
            elif arg.startswith("--git-depth="):
                git_depth = int(arg.split("=", 1)[1])
            elif arg.startswith("--vps-"):
                raise MngrError(f"Unknown VPS build arg: {arg}. Valid VPS args: --vps-region=, --vps-plan=, --vps-os=, --git-depth=")
            else:
                docker_build_args.append(arg)

    return _ParsedVpsBuildOptions(
        region=region,
        plan=plan,
        os_id=os_id,
        git_depth=git_depth,
        docker_build_args=tuple(docker_build_args),
    )


def _resolve_dockerfile_paths(
    docker_build_args: Sequence[str],
    remote_build_dir: str,
) -> tuple[str, ...]:
    """Rewrite relative --file/--dockerfile paths to absolute paths on the VPS.

    Docker resolves --file relative to the daemon's CWD, not the build context.
    Since the build context was uploaded to remote_build_dir on the VPS, any
    relative Dockerfile paths must be prefixed with that directory.

    Handles both ``--file=Dockerfile`` and ``-f Dockerfile`` forms.
    """
    resolved: list[str] = []
    is_next_arg_dockerfile = False
    for arg in docker_build_args:
        if is_next_arg_dockerfile:
            if not arg.startswith("/"):
                arg = f"{remote_build_dir}/{arg}"
            is_next_arg_dockerfile = False
        elif arg in ("-f", "--file", "--dockerfile"):
            is_next_arg_dockerfile = True
        else:
            for prefix in ("--file=", "-f=", "--dockerfile="):
                if arg.startswith(prefix):
                    dockerfile_path = arg[len(prefix):]
                    if not dockerfile_path.startswith("/"):
                        arg = f"{prefix}{remote_build_dir}/{dockerfile_path}"
                    break
        resolved.append(arg)
    return tuple(resolved)


def _emit_docker_build_output(line: str) -> None:
    """Log a line of docker build output at BUILD level."""
    stripped = line.strip()
    if stripped:
        logger.log(LogLevel.BUILD.value, "{}", stripped, source="docker")


# Label constants (same scheme as Docker provider)
LABEL_PREFIX: Final[str] = "com.imbue.mngr."
LABEL_PROVIDER: Final[str] = f"{LABEL_PREFIX}provider"
LABEL_HOST_ID: Final[str] = f"{LABEL_PREFIX}host-id"
LABEL_HOST_NAME: Final[str] = f"{LABEL_PREFIX}host-name"
LABEL_TAGS: Final[str] = f"{LABEL_PREFIX}tags"

# Default image when no user customization
DEFAULT_IMAGE: Final[str] = "debian:bookworm-slim"

# Host volume mount path inside the container
HOST_VOLUME_MOUNT_PATH: Final[str] = "/mngr-vol"


class VpsDockerProvider(BaseProviderInstance):
    """Provider that runs agents in Docker containers on VPS instances.

    Each host maps to exactly one VPS running exactly one Docker container.
    The VPS stays running at all times; stop/start operates on the container.
    Destroying the host destroys both the container and the VPS.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    config: VpsDockerProviderConfig = Field(frozen=True, description="VPS Docker provider configuration")
    vps_client: VpsClientInterface = Field(frozen=True, description="VPS provider API client")

    _host_by_id_cache: dict[HostId, HostInterface] = PrivateAttr(default_factory=dict)
    _host_record_cache: dict[HostId, VpsDockerHostRecord] = PrivateAttr(default_factory=dict)
    _container_running_cache: dict[str, bool] = PrivateAttr(default_factory=dict)

    @property
    def supports_snapshots(self) -> bool:
        return True

    @property
    def supports_shutdown_hosts(self) -> bool:
        return True

    @property
    def supports_volumes(self) -> bool:
        return True

    @property
    def supports_mutable_tags(self) -> bool:
        return False

    def reset_caches(self) -> None:
        self._host_by_id_cache.clear()
        self._host_record_cache.clear()
        self._container_running_cache.clear()

    # =========================================================================
    # Key Management
    # =========================================================================

    def _key_dir(self) -> Path:
        """Directory for SSH keys for this provider instance."""
        key_dir = self.mngr_ctx.profile_dir / "providers" / str(self.config.backend) / str(self.name) / "keys"
        key_dir.mkdir(parents=True, exist_ok=True)
        return key_dir

    def _get_vps_ssh_keypair(self) -> tuple[Path, str]:
        """Load or create the SSH keypair for authenticating to the VPS."""
        return load_or_create_ssh_keypair(self._key_dir(), "vps_ssh_key")

    def _get_container_ssh_keypair(self) -> tuple[Path, str]:
        """Load or create the SSH keypair for authenticating to the container."""
        return load_or_create_ssh_keypair(self._key_dir(), "container_ssh_key")

    def _get_vps_host_keypair(self) -> tuple[Path, str]:
        """Load or create the Ed25519 host keypair injected into VPS via cloud-init."""
        return load_or_create_host_keypair(self._key_dir(), "host_key")

    def _get_container_host_keypair(self) -> tuple[Path, str]:
        """Load or create the Ed25519 host keypair for the container's sshd."""
        return load_or_create_host_keypair(self._key_dir(), "container_host_key")

    def _vps_known_hosts_path(self) -> Path:
        return self._key_dir() / "vps_known_hosts"

    def _container_known_hosts_path(self) -> Path:
        return self._key_dir() / "container_known_hosts"

    # =========================================================================
    # Docker-over-SSH helper
    # =========================================================================

    def _make_docker_ssh(self, vps_ip: str) -> DockerOverSsh:
        """Create a DockerOverSsh instance for the given VPS IP."""
        vps_key_path, _pub = self._get_vps_ssh_keypair()
        return DockerOverSsh(
            vps_ip=vps_ip,
            ssh_user="root",
            ssh_key_path=vps_key_path,
            known_hosts_path=self._vps_known_hosts_path(),
        )

    # =========================================================================
    # Host Store
    # =========================================================================

    def _get_host_store(self, docker_ssh: DockerOverSsh) -> VpsDockerHostStore:
        """Get or create the host store on the VPS."""
        state_container_name = ensure_state_container(
            docker_ssh=docker_ssh,
            prefix=self.mngr_ctx.config.prefix,
            user_id=str(self.mngr_ctx.get_profile_user_id()),
            provider_name=str(self.name),
        )
        return VpsDockerHostStore(
            docker_ssh=docker_ssh,
            state_container_name=state_container_name,
        )

    # =========================================================================
    # Host Object Construction
    # =========================================================================

    def _create_host_object(
        self,
        host_id: HostId,
        vps_ip: str,
        docker_ssh: DockerOverSsh,
    ) -> Host:
        """Create a Host object with direct SSH to the container via the VPS's exposed port."""
        container_key_path, _container_pub = self._get_container_ssh_keypair()

        # Container sshd port is exposed on the VPS's public IP.
        # We connect directly to vps_ip:container_ssh_port.
        pyinfra_host = create_pyinfra_host(
            hostname=vps_ip,
            port=self.config.container_ssh_port,
            private_key_path=container_key_path,
            known_hosts_path=self._container_known_hosts_path(),
        )

        connector = PyinfraConnector(pyinfra_host)
        host = Host(
            id=host_id,
            connector=connector,
            provider_instance=self,
            mngr_ctx=self.mngr_ctx,
            on_updated_host_data=lambda callback_host_id, certified_data: self._on_certified_host_data_updated(
                callback_host_id, certified_data, vps_ip
            ),
        )
        self._host_by_id_cache[host_id] = host
        return host

    def _create_offline_host(
        self,
        host_record: VpsDockerHostRecord,
    ) -> OfflineHost:
        """Create an OfflineHost from a host record."""
        host_id = HostId(host_record.certified_host_data.host_id)
        vps_ip = host_record.vps_ip or ""
        offline = OfflineHost(
            id=host_id,
            certified_host_data=host_record.certified_host_data,
            provider_instance=self,
            mngr_ctx=self.mngr_ctx,
            on_updated_host_data=lambda callback_host_id, certified_data: self._on_certified_host_data_updated(
                callback_host_id, certified_data, vps_ip
            ),
        )
        self._host_by_id_cache[host_id] = offline
        return offline

    def _on_certified_host_data_updated(self, host_id: HostId, certified_data: CertifiedHostData, vps_ip: str) -> None:
        """Callback when host data.json is updated -- sync to state volume."""
        try:
            docker_ssh = self._make_docker_ssh(vps_ip)
            host_store = self._get_host_store(docker_ssh)
            existing = host_store.read_host_record(host_id)
            if existing is not None:
                updated = existing.model_copy(update={"certified_host_data": certified_data})
                host_store.write_host_record(updated)
        except (VpsConnectionError, ContainerSetupError) as e:
            logger.warning("Failed to sync certified data to VPS state volume: {}", e)

    # =========================================================================
    # VPS Provisioning
    # =========================================================================

    def _wait_for_cloud_init(self, docker_ssh: DockerOverSsh, timeout_seconds: float) -> None:
        """Wait for cloud-init to finish (Docker installed, marker file present)."""
        start = time.monotonic()
        while time.monotonic() - start < timeout_seconds:
            if docker_ssh.check_file_exists("/var/run/mngr-ready"):
                elapsed = time.monotonic() - start
                if elapsed > 30.0:
                    logger.warning("Cloud-init took {:.1f}s (threshold: 30s)", elapsed)
                return
            time.sleep(5.0)
        raise DockerNotReadyError(
            f"Cloud-init did not complete within {timeout_seconds}s. Docker may not be installed on the VPS."
        )

    def _wait_for_sshd_on_vps(self, vps_ip: str, timeout_seconds: float) -> None:
        """Wait for sshd on the VPS to be ready."""
        wait_for_sshd(hostname=vps_ip, port=22, timeout_seconds=timeout_seconds)

    # =========================================================================
    # Container Setup
    # =========================================================================

    def _setup_container_ssh(
        self,
        docker_ssh: DockerOverSsh,
        container_name: str,
        host_volume_mount_path: str | None,
        known_hosts_entries: tuple[str, ...],
        authorized_keys_entries: tuple[str, ...],
    ) -> None:
        """Set up SSH inside the container via docker exec."""
        container_key_path, container_public_key = self._get_container_ssh_keypair()
        container_host_key_path, container_host_public_key = self._get_container_host_keypair()
        container_host_private_key = container_host_key_path.read_text()

        # Install packages and set up host_dir
        with log_span("Installing packages in container"):
            install_cmd = build_check_and_install_packages_command(
                mngr_host_dir=str(self.host_dir),
                host_volume_mount_path=host_volume_mount_path,
            )
            docker_ssh.exec_in_container(container_name, install_cmd, timeout_seconds=300.0)

        # Configure SSH keys
        with log_span("Configuring SSH in container"):
            ssh_cmd = build_configure_ssh_command(
                user="root",
                client_public_key=container_public_key,
                host_private_key=container_host_private_key,
                host_public_key=container_host_public_key,
            )
            docker_ssh.exec_in_container(container_name, ssh_cmd)

        # Add known_hosts entries
        known_hosts_cmd = build_add_known_hosts_command("root", known_hosts_entries)
        if known_hosts_cmd is not None:
            docker_ssh.exec_in_container(container_name, known_hosts_cmd)

        # Add authorized_keys entries
        auth_keys_cmd = build_add_authorized_keys_command("root", authorized_keys_entries)
        if auth_keys_cmd is not None:
            docker_ssh.exec_in_container(container_name, auth_keys_cmd)

        # Start sshd
        with log_span("Starting sshd in container"):
            docker_ssh.exec_in_container(
                container_name,
                "/usr/sbin/sshd -D -o MaxSessions=100 &",
            )

        # Add container host key to local known_hosts.
        # The container is reached via <vps_ip>:<container_ssh_port> directly.
        # We need to add the key for that endpoint. Since we don't know the
        # VPS IP here, the caller is responsible for adding the known_hosts entry.

    # =========================================================================
    # Core Lifecycle: create_host
    # =========================================================================

    def create_host(
        self,
        name: HostName,
        image: ImageReference | None = None,
        tags: Mapping[str, str] | None = None,
        build_args: Sequence[str] | None = None,
        start_args: Sequence[str] | None = None,
        lifecycle: HostLifecycleOptions | None = None,
        known_hosts: Sequence[str] | None = None,
        authorized_keys: Sequence[str] | None = None,
        snapshot: SnapshotName | None = None,
    ) -> Host:
        host_id = HostId.generate()
        logger.info("Creating VPS Docker host {} ({}) ...", name, host_id)

        base_image = str(image) if image else self.config.default_image
        effective_start_args = tuple(self.config.default_start_args) + tuple(start_args or ())
        parsed = self._parse_build_args(build_args)
        region, plan, os_id = parsed.region, parsed.plan, parsed.os_id
        docker_build_args = parsed.docker_build_args

        _vps_key_path, vps_public_key = self._get_vps_ssh_keypair()
        vps_host_key_path, vps_host_public_key = self._get_vps_host_keypair()

        with log_span("Uploading SSH key to provider"):
            key_name = f"mngr-{self.name}-{host_id}"
            vps_ssh_key_id = self.vps_client.upload_ssh_key(key_name, vps_public_key)

        vps_instance_id: VpsInstanceId | None = None
        try:
            vps_instance_id, vps_ip, docker_ssh = self._provision_vps(
                host_id=host_id,
                name=name,
                region=region,
                plan=plan,
                os_id=os_id,
                vps_host_key_path=vps_host_key_path,
                vps_host_public_key=vps_host_public_key,
                vps_ssh_key_id=vps_ssh_key_id,
            )

            container_name, container_id, volume_name = self._setup_container_on_vps(
                docker_ssh=docker_ssh,
                host_id=host_id,
                name=name,
                vps_ip=vps_ip,
                base_image=base_image,
                effective_start_args=effective_start_args,
                docker_build_args=docker_build_args,
                git_depth=parsed.git_depth,
                tags=tags,
                known_hosts=known_hosts,
                authorized_keys=authorized_keys,
            )

            host = self._finalize_host_creation(
                host_id=host_id,
                name=name,
                vps_ip=vps_ip,
                docker_ssh=docker_ssh,
                container_name=container_name,
                container_id=container_id,
                volume_name=volume_name,
                base_image=base_image,
                effective_start_args=effective_start_args,
                tags=tags,
                lifecycle=lifecycle,
                region=region,
                plan=plan,
                os_id=os_id,
                vps_instance_id=vps_instance_id,
                vps_ssh_key_id=vps_ssh_key_id,
                vps_host_public_key=vps_host_public_key,
            )

            logger.info("VPS Docker host {} created successfully (VPS: {}, IP: {})", name, vps_instance_id, vps_ip)
            return host

        except Exception:
            keep_failed = os.environ.get("MNGR_KEEP_FAILED_HOSTS", "0") == "1"
            if keep_failed:
                logger.error(
                    "Host creation failed. MNGR_KEEP_FAILED_HOSTS=1 is set, "
                    "skipping cleanup so you can debug. VPS instance: {}, IP: {}",
                    vps_instance_id,
                    vps_ip if "vps_ip" in dir() else "unknown",
                )
            else:
                logger.error("Host creation failed, attempting cleanup...")
                try:
                    if vps_instance_id is not None:
                        self.vps_client.destroy_instance(vps_instance_id)
                except Exception as cleanup_err:
                    logger.warning("Failed to clean up VPS instance: {}", cleanup_err)
                try:
                    self.vps_client.delete_ssh_key(vps_ssh_key_id)
                except Exception as cleanup_err:
                    logger.warning("Failed to clean up SSH key: {}", cleanup_err)
            raise

    def _provision_vps(
        self,
        host_id: HostId,
        name: HostName,
        region: str,
        plan: str,
        os_id: int,
        vps_host_key_path: Path,
        vps_host_public_key: str,
        vps_ssh_key_id: str,
    ) -> tuple[VpsInstanceId, str, DockerOverSsh]:
        """Provision a VPS, wait for it to boot, and wait for Docker to install.

        Returns (vps_instance_id, vps_ip, docker_ssh).
        """
        vps_host_private_key = vps_host_key_path.read_text()
        user_data = generate_cloud_init_user_data(
            host_private_key=vps_host_private_key,
            host_public_key=vps_host_public_key,
        )

        logger.log(LogLevel.BUILD.value, "Creating VPS instance (region: {}, plan: {})...", region, plan, source="vps")
        with log_span("Creating VPS instance"):
            vps_tags = [f"mngr-host-id={host_id}", f"mngr-provider={self.name}"]
            vps_instance_id = self.vps_client.create_instance(
                label=f"mngr-{name}",
                region=region,
                plan=plan,
                os_id=os_id,
                user_data=user_data,
                ssh_key_ids=[vps_ssh_key_id],
                tags=vps_tags,
            )

        logger.log(LogLevel.BUILD.value, "Waiting for VPS to become active...", source="vps")
        with log_span("Waiting for VPS to become active"):
            vps_ip = self.vps_client.wait_for_instance_active(
                vps_instance_id,
                timeout_seconds=self.config.vps_boot_timeout,
            )
        logger.log(LogLevel.BUILD.value, "VPS active (IP: {})", vps_ip, source="vps")

        add_host_to_known_hosts(
            known_hosts_path=self._vps_known_hosts_path(),
            hostname=vps_ip,
            port=22,
            public_key=vps_host_public_key,
        )

        logger.log(LogLevel.BUILD.value, "Waiting for SSH to be ready on VPS...", source="vps")
        with log_span("Waiting for VPS SSH"):
            self._wait_for_sshd_on_vps(vps_ip, timeout_seconds=self.config.ssh_connect_timeout)

        docker_ssh = self._make_docker_ssh(vps_ip)

        logger.log(LogLevel.BUILD.value, "Waiting for cloud-init to complete (Docker installation)...", source="vps")
        with log_span("Waiting for cloud-init (Docker install)"):
            self._wait_for_cloud_init(docker_ssh, timeout_seconds=self.config.docker_install_timeout)
        logger.log(LogLevel.BUILD.value, "Cloud-init complete, Docker is ready", source="vps")

        return vps_instance_id, vps_ip, docker_ssh

    def _setup_container_on_vps(
        self,
        docker_ssh: DockerOverSsh,
        host_id: HostId,
        name: HostName,
        vps_ip: str,
        base_image: str,
        effective_start_args: tuple[str, ...],
        docker_build_args: tuple[str, ...],
        git_depth: int | None,
        tags: Mapping[str, str] | None,
        known_hosts: Sequence[str] | None,
        authorized_keys: Sequence[str] | None,
    ) -> tuple[str, str, str]:
        """Create the Docker container and configure SSH inside it.

        If docker_build_args are provided, uploads the build context to the VPS
        and runs docker build there. Otherwise pulls the base image directly.

        Returns (container_name, container_id, volume_name).
        """
        with log_span("Setting up state container on VPS"):
            self._get_host_store(docker_ssh)

        volume_name = f"mngr-host-vol-{host_id.get_uuid().hex}"
        with log_span("Creating host volume"):
            docker_ssh.create_volume(volume_name)

        if docker_build_args:
            base_image = self._build_image_on_vps(docker_ssh, host_id, base_image, docker_build_args, git_depth)
        else:
            logger.log(LogLevel.BUILD.value, "Pulling Docker image {} on VPS...", base_image, source="vps")
            with log_span("Pulling Docker image on VPS"):
                docker_ssh.pull_image(base_image, timeout_seconds=300.0)

        container_name = f"{self.mngr_ctx.config.prefix}{name}"
        labels = {
            LABEL_HOST_ID: str(host_id),
            LABEL_HOST_NAME: str(name),
            LABEL_PROVIDER: str(self.name),
            LABEL_TAGS: json.dumps(dict(tags) if tags else {}),
        }
        logger.log(LogLevel.BUILD.value, "Starting Docker container on VPS...", source="vps")
        with log_span("Starting Docker container"):
            container_id = docker_ssh.run_container(
                image=base_image,
                name=container_name,
                port_mappings={f"0.0.0.0:{self.config.container_ssh_port}": "22"},
                volumes=[f"{volume_name}:{HOST_VOLUME_MOUNT_PATH}:rw"],
                labels=labels,
                extra_args=list(effective_start_args),
                entrypoint_cmd=CONTAINER_ENTRYPOINT_CMD,
            )

        logger.log(LogLevel.BUILD.value, "Setting up SSH in container...", source="vps")
        with log_span("Setting up SSH in container"):
            self._setup_container_ssh(
                docker_ssh=docker_ssh,
                container_name=container_name,
                host_volume_mount_path=HOST_VOLUME_MOUNT_PATH,
                known_hosts_entries=tuple(known_hosts or ()),
                authorized_keys_entries=tuple(authorized_keys or ()),
            )

        _container_host_key_path, container_host_public_key = self._get_container_host_keypair()
        add_host_to_known_hosts(
            known_hosts_path=self._container_known_hosts_path(),
            hostname=vps_ip,
            port=self.config.container_ssh_port,
            public_key=container_host_public_key,
        )
        logger.log(LogLevel.BUILD.value, "Waiting for container SSH to be ready...", source="vps")
        with log_span("Waiting for container SSH"):
            self._wait_for_container_sshd(vps_ip)
        logger.log(LogLevel.BUILD.value, "Container SSH ready", source="vps")

        return container_name, container_id, volume_name

    def _finalize_host_creation(
        self,
        host_id: HostId,
        name: HostName,
        vps_ip: str,
        docker_ssh: DockerOverSsh,
        container_name: str,
        container_id: str,
        volume_name: str,
        base_image: str,
        effective_start_args: tuple[str, ...],
        tags: Mapping[str, str] | None,
        lifecycle: HostLifecycleOptions | None,
        region: str,
        plan: str,
        os_id: int,
        vps_instance_id: VpsInstanceId,
        vps_ssh_key_id: str,
        vps_host_public_key: str,
    ) -> Host:
        """Create the Host object, configure activity watching, and persist state."""
        host = self._create_host_object(host_id, vps_ip, docker_ssh)

        idle_timeout = self.config.default_idle_timeout
        activity_sources = self.config.default_activity_sources
        if lifecycle is not None:
            if lifecycle.idle_timeout_seconds is not None:
                idle_timeout = lifecycle.idle_timeout_seconds
            if lifecycle.activity_sources is not None:
                activity_sources = lifecycle.activity_sources

        now = datetime.now(timezone.utc)
        host_data = CertifiedHostData(
            host_id=str(host_id),
            host_name=str(name),
            idle_timeout_seconds=idle_timeout,
            activity_sources=activity_sources,
            image=base_image,
            user_tags=dict(tags) if tags else {},
            created_at=now,
            updated_at=now,
        )
        host.record_activity(ActivitySource.BOOT)
        host.set_certified_data(host_data)

        self._create_shutdown_script(host)
        with log_span("Starting activity watcher"):
            start_watcher_cmd = build_start_activity_watcher_command(str(self.host_dir))
            docker_ssh.exec_in_container(container_name, start_watcher_cmd)

        host_record = VpsDockerHostRecord(
            certified_host_data=host_data,
            vps_ip=vps_ip,
            ssh_host_public_key=vps_host_public_key,
            container_ssh_host_public_key=self._get_container_host_keypair()[1],
            config=VpsHostConfig(
                vps_instance_id=vps_instance_id,
                region=region,
                plan=plan,
                os_id=os_id,
                start_args=effective_start_args,
                image=base_image,
                container_name=container_name,
                volume_name=volume_name,
                vps_ssh_key_id=vps_ssh_key_id,
            ),
            container_id=container_id,
        )
        host_store = self._get_host_store(docker_ssh)
        host_store.write_host_record(host_record)

        return host

    def _wait_for_container_sshd(self, vps_ip: str) -> None:
        """Wait for sshd in the container to be reachable via the VPS's exposed port."""
        wait_for_sshd(
            hostname=vps_ip,
            port=self.config.container_ssh_port,
            timeout_seconds=self.config.ssh_connect_timeout,
        )

    def _build_image_on_vps(
        self,
        docker_ssh: DockerOverSsh,
        host_id: HostId,
        base_image: str,
        docker_build_args: tuple[str, ...],
        git_depth: int | None,
    ) -> str:
        """Build a Docker image on the VPS from the provided build args.

        Uploads the build context (if a local path is referenced) to the VPS
        and runs docker build there. Returns the image tag to use.

        If the local build context is a git worktree, clones it into a temp
        directory first so the .git directory is self-contained. If git_depth
        is specified, the clone uses --depth and always creates a temp clone
        (even for non-worktree repos).
        """
        build_tag = f"mngr-build-{host_id}"
        remote_build_dir = f"/tmp/mngr-build-{host_id.get_uuid().hex}"

        # Separate the build context path from other docker build args.
        # Docker build expects the last positional arg to be the context path.
        # We scan for args that look like local paths (not starting with --)
        # and upload them as the build context.
        context_args: list[str] = []
        non_context_args: list[str] = []
        for arg in docker_build_args:
            if not arg.startswith("-") and Path(arg).exists():
                context_args.append(arg)
            else:
                non_context_args.append(arg)

        # If the build context is a git worktree or --git-depth is set,
        # clone into a temp directory to get a standalone .git directory.
        local_clone_dir: Path | None = None
        if context_args:
            local_context = Path(context_args[-1]).resolve()
            is_worktree = (local_context / ".git").is_file()
            if is_worktree or git_depth is not None:
                local_clone_dir = Path(tempfile.mkdtemp(prefix="mngr-vps-build-"))
                clone_reason = "worktree" if is_worktree else f"--git-depth={git_depth}"
                logger.log(
                    LogLevel.BUILD.value,
                    "Cloning build context locally ({})...",
                    clone_reason,
                    source="vps",
                )
                clone_cmd = ["git", "clone"]
                if git_depth is not None:
                    clone_cmd.extend(["--depth", str(git_depth)])
                # Use file:// so --depth is honored for local repos
                clone_cmd.extend([f"file://{local_context}", str(local_clone_dir / "repo")])
                cg = ConcurrencyGroup(name="git-clone-build-context")
                with cg:
                    clone_result = cg.run_process_to_completion(
                        command=clone_cmd,
                        is_checked_after=False,
                        timeout=120.0,
                    )
                if clone_result.returncode != 0:
                    raise ContainerSetupError(
                        f"Failed to clone build context: {clone_result.stderr.strip()}"
                    )
                context_args[-1] = str(local_clone_dir / "repo")

        try:
            logger.log(LogLevel.BUILD.value, "Building Docker image on VPS (this may take several minutes)...", source="docker")
            if context_args:
                upload_context = Path(context_args[-1])
                logger.log(LogLevel.BUILD.value, "Uploading build context to VPS...", source="vps")
                with log_span("Uploading build context to VPS"):
                    docker_ssh.run_ssh(f"mkdir -p {remote_build_dir}")
                    docker_ssh.upload_directory(upload_context, remote_build_dir)

                # Rewrite --file/--dockerfile paths to absolute paths on the VPS.
                # These are relative to the local build context, but on the VPS
                # the context lives at remote_build_dir.
                resolved_build_args = _resolve_dockerfile_paths(non_context_args, remote_build_dir)

                with log_span("Building Docker image on VPS"):
                    docker_ssh.build_image(
                        tag=build_tag,
                        build_context_path=remote_build_dir,
                        docker_build_args=tuple(resolved_build_args),
                        timeout_seconds=600.0,
                        on_output=_emit_docker_build_output,
                    )
            else:
                # No local context -- pass all args to docker build with a minimal context
                docker_ssh.run_ssh(f"mkdir -p {remote_build_dir}")
                with log_span("Building Docker image on VPS"):
                    docker_ssh.build_image(
                        tag=build_tag,
                        build_context_path=remote_build_dir,
                        docker_build_args=tuple(docker_build_args),
                        timeout_seconds=600.0,
                        on_output=_emit_docker_build_output,
                    )
            logger.log(LogLevel.BUILD.value, "Docker image built successfully", source="docker")
        finally:
            if local_clone_dir is not None:
                shutil.rmtree(local_clone_dir, ignore_errors=True)

        # Clean up remote build directory
        try:
            docker_ssh.run_ssh(f"rm -rf {remote_build_dir}")
        except (VpsConnectionError, ContainerSetupError) as e:
            logger.debug("Failed to clean up remote build dir: {}", e)

        return build_tag

    def _create_shutdown_script(self, host: Host) -> None:
        """Create the shutdown script that stops the container on idle."""
        shutdown_script = "#!/bin/bash\nkill -TERM 1\n"
        commands_dir = host.host_dir / "commands"
        host.execute_idempotent_command(f"mkdir -p {commands_dir}")
        host.write_file(commands_dir / "shutdown.sh", shutdown_script.encode())
        host.execute_idempotent_command(f"chmod +x {commands_dir / 'shutdown.sh'}")

    def _parse_build_args(self, build_args: Sequence[str] | None) -> _ParsedVpsBuildOptions:
        """Parse build args, separating VPS provisioning args from Docker build args."""
        return _parse_build_args(
            build_args,
            default_region=self.config.default_region,
            default_plan=self.config.default_plan,
            default_os_id=self.config.default_os_id,
        )

    # =========================================================================
    # Core Lifecycle: stop_host
    # =========================================================================

    def stop_host(
        self,
        host: HostInterface | HostId,
        create_snapshot: bool = True,
        timeout_seconds: float = 60.0,
    ) -> None:
        host_id = host.id if isinstance(host, HostInterface) else host
        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.config is None or host_record.vps_ip is None:
            raise HostNotFoundError(host_id)

        docker_ssh = self._make_docker_ssh(host_record.vps_ip)

        if create_snapshot:
            try:
                self.create_snapshot(host_id)
            except MngrError as e:
                logger.warning("Failed to create snapshot before stop: {}", e)

        with log_span("Stopping container on VPS"):
            docker_ssh.stop_container(host_record.config.container_name, timeout_seconds=int(timeout_seconds))

        # Update host record
        host_store = self._get_host_store(docker_ssh)
        now = datetime.now(timezone.utc)
        updated_data = host_record.certified_host_data.model_copy(update={"updated_at": now})
        updated_record = host_record.model_copy(update={"certified_host_data": updated_data})
        host_store.write_host_record(updated_record)

        self._host_by_id_cache.pop(host_id, None)
        logger.info("Host {} stopped", host_id)

    # =========================================================================
    # Core Lifecycle: start_host
    # =========================================================================

    def start_host(
        self,
        host: HostInterface | HostId,
        snapshot_id: SnapshotId | None = None,
    ) -> Host:
        host_id = host.id if isinstance(host, HostInterface) else host
        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.config is None or host_record.vps_ip is None:
            raise HostNotFoundError(host_id)

        docker_ssh = self._make_docker_ssh(host_record.vps_ip)

        with log_span("Starting container on VPS"):
            docker_ssh.start_container(host_record.config.container_name)

        # Wait for sshd in container
        with log_span("Waiting for container SSH"):
            self._wait_for_container_sshd(host_record.vps_ip)

        host_obj = self._create_host_object(host_id, host_record.vps_ip, docker_ssh)
        logger.info("Host {} started", host_id)
        return host_obj

    # =========================================================================
    # Core Lifecycle: destroy_host
    # =========================================================================

    def destroy_host(self, host: HostInterface | HostId) -> None:
        host_id = host.id if isinstance(host, HostInterface) else host
        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.config is None:
            raise HostNotFoundError(host_id)

        vps_config = host_record.config
        vps_ip = host_record.vps_ip

        if vps_ip is not None:
            docker_ssh = self._make_docker_ssh(vps_ip)

            # Stop and remove container
            try:
                docker_ssh.remove_container(vps_config.container_name, force=True)
            except (VpsConnectionError, ContainerSetupError) as e:
                logger.warning("Failed to remove container: {}", e)

            # Remove host volume
            try:
                docker_ssh.remove_volume(vps_config.volume_name)
            except (VpsConnectionError, ContainerSetupError) as e:
                logger.warning("Failed to remove host volume: {}", e)

            # Delete host record from state volume
            try:
                host_store = self._get_host_store(docker_ssh)
                host_store.delete_host_record(host_id)
            except (VpsConnectionError, ContainerSetupError) as e:
                logger.warning("Failed to delete host record from state volume: {}", e)

        # Destroy the VPS instance
        with log_span("Destroying VPS instance"):
            try:
                self.vps_client.destroy_instance(vps_config.vps_instance_id)
            except Exception as e:
                logger.warning("Failed to destroy VPS: {}", e)

        # Clean up SSH key from provider
        if vps_config.vps_ssh_key_id is not None:
            try:
                self.vps_client.delete_ssh_key(vps_config.vps_ssh_key_id)
            except Exception as e:
                logger.warning("Failed to delete SSH key from provider: {}", e)

        # Clean up local known_hosts
        if vps_ip is not None:
            try:
                _remove_host_from_known_hosts(self._vps_known_hosts_path(), vps_ip, 22)
            except Exception as e:
                logger.trace("Failed to clean up VPS known_hosts: {}", e)
            try:
                _remove_host_from_known_hosts(
                    self._container_known_hosts_path(), vps_ip, self.config.container_ssh_port
                )
            except Exception as e:
                logger.trace("Failed to clean up container known_hosts: {}", e)

        self._host_by_id_cache.pop(host_id, None)
        logger.info("Host {} destroyed (VPS {})", host_id, vps_config.vps_instance_id)

    def delete_host(self, host: HostInterface) -> None:
        """Delete all local records for a destroyed host (does not destroy VPS)."""
        host_id = host.id
        self._host_by_id_cache.pop(host_id, None)

    def on_connection_error(self, host_id: HostId) -> None:
        self._host_by_id_cache.pop(host_id, None)

    # =========================================================================
    # Discovery
    # =========================================================================

    def get_host(self, host: HostId | HostName) -> HostInterface:
        if isinstance(host, HostId) and host in self._host_by_id_cache:
            return self._host_by_id_cache[host]

        # Try to find via host records on all known VPSes
        # For now, we iterate all host records
        host_record = self._find_host_record(host)
        if host_record is None:
            raise HostNotFoundError(host)

        host_id = HostId(host_record.certified_host_data.host_id)
        vps_ip = host_record.vps_ip

        if vps_ip is not None and host_record.config is not None:
            docker_ssh = self._make_docker_ssh(vps_ip)
            # Check if container is running
            if docker_ssh.container_is_running(host_record.config.container_name):
                return self._create_host_object(host_id, vps_ip, docker_ssh)

        return self._create_offline_host(host_record)

    def to_offline_host(self, host_id: HostId) -> OfflineHost:
        host_record = self._find_host_record(host_id)
        if host_record is None:
            raise HostNotFoundError(host_id)
        return self._create_offline_host(host_record)

    def discover_hosts(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> list[DiscoveredHost]:
        """Discover all hosts managed by this provider."""
        discovered: list[DiscoveredHost] = []

        # Query all VPS instances from the provider API that have our tags
        # then SSH to each VPS to read host records from the state volume.

        # First, try to find any VPS instances for this provider
        # We'll need the host records from each VPS
        try:
            all_records = self._discover_host_records()
        except Exception as e:
            logger.warning("Failed to discover hosts: {}", e)
            return []

        for record in all_records:
            host_id = HostId(record.certified_host_data.host_id)
            host_name = HostName(record.certified_host_data.host_name)
            discovered.append(
                DiscoveredHost(
                    host_id=host_id,
                    host_name=host_name,
                    provider_name=self.name,
                )
            )
            # Cache the host object
            if record.vps_ip is not None and record.config is not None:
                docker_ssh = self._make_docker_ssh(record.vps_ip)
                if docker_ssh.container_is_running(record.config.container_name):
                    self._create_host_object(host_id, record.vps_ip, docker_ssh)
                else:
                    self._create_offline_host(record)
            else:
                self._create_offline_host(record)

        return discovered

    def discover_hosts_and_agents(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> dict[DiscoveredHost, list[DiscoveredAgent]]:
        """Load hosts and agent references from state volumes in batched SSH calls.

        Reads all host records and agent data from each VPS in a single SSH command,
        then determines container running status. Avoids the default implementation's
        per-host SSH calls into containers for agent discovery.
        """
        with log_span("VPS Docker discover_hosts_and_agents for provider={}", self.name):
            try:
                all_records, agent_data_by_host_id = self._discover_host_records_with_agents()
            except Exception as e:
                logger.warning("Failed to discover VPS Docker hosts: {}", e)
                return {}

        result: dict[DiscoveredHost, list[DiscoveredAgent]] = {}
        for record in all_records:
            host_id = HostId(record.certified_host_data.host_id)
            host_name = HostName(record.certified_host_data.host_name)

            # Cache the host record for later use by get_host_and_agent_details
            self._host_record_cache[host_id] = record

            # Determine host state from container running status
            is_running = False
            docker_ssh = None
            if record.vps_ip is not None and record.config is not None:
                docker_ssh = self._make_docker_ssh(record.vps_ip)
                container_name = record.config.container_name
                if container_name not in self._container_running_cache:
                    self._container_running_cache[container_name] = docker_ssh.container_is_running(container_name)
                is_running = self._container_running_cache[container_name]

            has_snapshots = len(record.certified_host_data.snapshots) > 0
            is_failed = record.certified_host_data.failure_reason is not None

            if not is_running and not is_failed and not has_snapshots and not include_destroyed:
                continue

            if is_running and docker_ssh is not None and record.vps_ip is not None:
                host_state = HostState.RUNNING
                self._create_host_object(host_id, record.vps_ip, docker_ssh)
            else:
                host_state = derive_offline_host_state(
                    certified_data=record.certified_host_data,
                    supports_shutdown_hosts=self.supports_shutdown_hosts,
                    supports_snapshots=self.supports_snapshots,
                    has_snapshots=has_snapshots,
                )
                self._create_offline_host(record)

            host_ref = DiscoveredHost(
                host_id=host_id,
                host_name=host_name,
                provider_name=self.name,
                host_state=host_state,
            )

            # Build agent refs from persisted agent data
            agent_refs: list[DiscoveredAgent] = []
            for agent_data in agent_data_by_host_id.get(host_id, []):
                ref = validate_and_create_discovered_agent(agent_data, host_id, self.name)
                if ref is not None:
                    agent_refs.append(ref)

            result[host_ref] = agent_refs

        return result

    def _discover_host_records_with_agents(
        self,
    ) -> tuple[list[VpsDockerHostRecord], dict[HostId, list[dict[str, Any]]]]:
        """Discover host records and agent data from state volumes.

        Calls _discover_host_records() for host records, and reads agent data
        from the state volume in the same batched SSH call. Concrete subclasses
        override this to include API-based discovery.
        """
        return [], {}

    def _discover_host_records(self) -> list[VpsDockerHostRecord]:
        """Discover host records by iterating known VPS instances."""
        # For each VPS instance that has our provider tag, SSH in and read
        # the state volume for host records
        all_records: list[VpsDockerHostRecord] = []

        # VpsClientInterface doesn't expose list_instances, so this base
        # implementation returns empty. Concrete subclasses override this
        # to query their provider API for tagged instances.

        # Since we can't easily list all VPS instances from the abstract interface,
        # we'll iterate host records from the state volumes of known VPSes.
        # This requires us to know at least one VPS IP to read from.

        # Approach: use the vps_client to list instances if it supports it,
        # otherwise return empty. Concrete implementations will override discover_hosts.
        return all_records

    def _find_host_record(self, host: HostId | HostName) -> VpsDockerHostRecord | None:
        """Find a host record by ID or name across all known VPSes."""
        # For now, we need to iterate through VPS instances
        # This is a placeholder that concrete subclasses should improve
        return None

    # =========================================================================
    # Optimized Listing
    # =========================================================================

    def get_host_and_agent_details(
        self,
        host_ref: DiscoveredHost,
        agent_refs: Sequence[DiscoveredAgent],
        field_generators: Mapping[str, Mapping[str, Callable[[AgentInterface, OnlineHostInterface], Any]]]
        | None = None,
        on_error: Callable[[DiscoveredAgent | DiscoveredHost, BaseException], None] | None = None,
    ) -> tuple[HostDetails, list[AgentDetails]]:
        """Build HostDetails and AgentDetails via a single SSH command."""
        # Look up cached host record (populated during discover_hosts_and_agents)
        host_record = self._host_record_cache.get(host_ref.host_id)
        if host_record is None:
            host_record = self._find_host_record(host_ref.host_id)

        # For offline hosts or hosts without a record, fall back to default
        if host_record is None or host_record.vps_ip is None or host_record.config is None:
            return super().get_host_and_agent_details(host_ref, agent_refs, field_generators, on_error)

        try:
            host = self.get_host(host_ref.host_id)

            if not isinstance(host, Host):
                return super().get_host_and_agent_details(host_ref, agent_refs, field_generators, on_error)

            # Collect all data in one SSH command
            docker_ssh = self._make_docker_ssh(host_record.vps_ip)
            script = build_listing_collection_script(str(self.host_dir), self.mngr_ctx.config.prefix)

            with log_span("Collecting listing data via single SSH command"):
                raw_output = docker_ssh.exec_in_container(
                    host_record.config.container_name,
                    script,
                    timeout_seconds=30.0,
                )

            raw = parse_listing_collection_output(raw_output)

        except HostConnectionError as e:
            self.on_connection_error(host_ref.host_id)
            logger.debug(
                "Host {} unreachable during optimized listing, falling back to default: {}",
                host_ref.host_id,
                e,
            )
            return super().get_host_and_agent_details(host_ref, agent_refs, field_generators, on_error)
        except MngrError as e:
            if on_error:
                on_error(host_ref, e)
                return HostDetails(
                    id=host_ref.host_id,
                    name=str(host_ref.host_name),
                    provider_name=host_ref.provider_name,
                    state=HostState.RUNNING,
                ), []
            else:
                raise

        host_details = self._build_host_details_from_raw(host, host_ref, host_record, raw)
        agent_details_list = self._build_agent_details_from_raw(host_details, host_record.certified_host_data, raw)
        return host_details, agent_details_list

    def _build_host_details_from_raw(
        self,
        host: Host,
        host_ref: DiscoveredHost,
        host_record: VpsDockerHostRecord,
        raw: dict[str, Any],
    ) -> HostDetails:
        """Construct HostDetails from cached host record and SSH-collected data."""
        ssh_info: SSHInfo | None = None
        ssh_connection = host.get_ssh_connection_info()
        if ssh_connection is not None:
            user, hostname, port, key_path = ssh_connection
            ssh_info = SSHInfo(
                user=user,
                host=hostname,
                port=port,
                key_path=key_path,
                command=f"ssh -i {key_path} -p {port} {user}@{hostname}",
            )

        boot_time = timestamp_to_datetime(raw.get("btime"))
        uptime_seconds = raw.get("uptime_seconds")
        resource = self.get_host_resources(host)

        lock_mtime = raw.get("lock_mtime")
        is_locked = lock_mtime is not None
        locked_time = datetime.fromtimestamp(lock_mtime, tz=timezone.utc) if lock_mtime is not None else None

        certified_data: CertifiedHostData | None = None
        certified_data_dict = raw.get("certified_data")
        if certified_data_dict is not None:
            try:
                certified_data = CertifiedHostData.model_validate(certified_data_dict)
            except (ValueError, KeyError) as e:
                logger.warning("Failed to validate host data.json from SSH output: {}", e)
        if certified_data is None:
            certified_data = host_record.certified_host_data

        tags = dict(certified_data.user_tags)

        ssh_activity_mtime = raw.get("ssh_activity_mtime")
        ssh_activity = (
            datetime.fromtimestamp(ssh_activity_mtime, tz=timezone.utc) if ssh_activity_mtime is not None else None
        )

        snapshots = self.list_snapshots(host)

        return HostDetails(
            id=host.id,
            name=certified_data.host_name,
            provider_name=host_ref.provider_name,
            state=HostState.RUNNING,
            image=certified_data.image,
            tags=tags,
            boot_time=boot_time,
            uptime_seconds=uptime_seconds,
            resource=resource,
            ssh=ssh_info,
            snapshots=snapshots,
            is_locked=is_locked,
            locked_time=locked_time,
            plugin=certified_data.plugin,
            ssh_activity_time=ssh_activity,
            failure_reason=certified_data.failure_reason,
        )

    def _build_agent_details_from_raw(
        self,
        host_details: HostDetails,
        certified_host_data: CertifiedHostData,
        raw: dict[str, Any],
    ) -> list[AgentDetails]:
        """Build AgentDetails objects from SSH-collected agent data."""
        idle_timeout_seconds = certified_host_data.idle_timeout_seconds
        activity_sources = certified_host_data.activity_sources
        idle_mode = certified_host_data.idle_mode

        ssh_activity = timestamp_to_datetime(raw.get("ssh_activity_mtime"))
        ps_output = raw.get("ps_output", "")

        agent_details_list: list[AgentDetails] = []
        for agent_raw in raw.get("agents", []):
            try:
                agent_details = self._build_single_agent_details(
                    agent_raw=agent_raw,
                    host_details=host_details,
                    ssh_activity=ssh_activity,
                    ps_output=ps_output,
                    idle_timeout_seconds=idle_timeout_seconds,
                    activity_sources=activity_sources,
                    idle_mode=idle_mode,
                )
                if agent_details is not None:
                    agent_details_list.append(agent_details)
            except (ValueError, KeyError, TypeError) as e:
                agent_id = agent_raw.get("data", {}).get("id", "unknown")
                logger.warning("Failed to build listing info for agent {}: {}", agent_id, e)

        return agent_details_list

    def _build_single_agent_details(
        self,
        agent_raw: dict[str, Any],
        host_details: HostDetails,
        ssh_activity: datetime | None,
        ps_output: str,
        idle_timeout_seconds: int,
        activity_sources: tuple[ActivitySource, ...],
        idle_mode: IdleMode,
    ) -> AgentDetails | None:
        """Build a single AgentDetails from raw SSH-collected data."""
        agent_data = agent_raw.get("data", {})
        agent_id_str = agent_data.get("id")
        agent_name_str = agent_data.get("name")
        if not agent_id_str or not agent_name_str:
            logger.warning("Skipped agent with missing id or name in listing data: {}", agent_data)
            return None

        agent_type = str(agent_data.get("type", "unknown"))
        command = CommandString(agent_data.get("command", "bash"))
        create_time_str = agent_data.get("create_time")
        try:
            create_time = (
                datetime.fromisoformat(create_time_str)
                if create_time_str
                else datetime(1970, 1, 1, tzinfo=timezone.utc)
            )
        except (ValueError, TypeError) as e:
            logger.warning("Failed to parse create_time for agent {}: {}", agent_id_str, e)
            create_time = datetime(1970, 1, 1, tzinfo=timezone.utc)

        user_activity = timestamp_to_datetime(agent_raw.get("user_activity_mtime"))
        agent_activity = timestamp_to_datetime(agent_raw.get("agent_activity_mtime"))
        start_time = timestamp_to_datetime(agent_raw.get("start_activity_mtime"))
        now = datetime.now(timezone.utc)
        runtime_seconds = (now - start_time).total_seconds() if start_time else None
        idle_seconds = compute_idle_seconds(user_activity, agent_activity, ssh_activity) or 0.0

        expected_process_name = resolve_expected_process_name(agent_type, command, self.mngr_ctx.config)
        is_type_known = check_agent_type_known(agent_type, self.mngr_ctx.config)
        state = determine_lifecycle_state(
            tmux_info=agent_raw.get("tmux_info"),
            is_active=agent_raw.get("is_active", False),
            expected_process_name=expected_process_name,
            ps_output=ps_output,
            is_agent_type_known=is_type_known,
        )

        return AgentDetails(
            id=AgentId(agent_id_str),
            name=AgentName(agent_name_str),
            type=agent_type,
            command=command,
            work_dir=Path(agent_data.get("work_dir", "/")),
            initial_branch=agent_data.get("created_branch_name"),
            create_time=create_time,
            start_on_boot=agent_data.get("start_on_boot", False),
            state=state,
            url=agent_raw.get("url"),
            start_time=start_time,
            runtime_seconds=runtime_seconds,
            user_activity_time=user_activity,
            agent_activity_time=agent_activity,
            idle_seconds=idle_seconds,
            idle_mode=idle_mode.value,
            idle_timeout_seconds=idle_timeout_seconds,
            activity_sources=tuple(s.value for s in activity_sources),
            labels=agent_data.get("labels", {}),
            host=host_details,
            plugin={},
        )

    # =========================================================================
    # Snapshots
    # =========================================================================

    def create_snapshot(
        self,
        host: HostInterface | HostId,
        name: SnapshotName | None = None,
    ) -> SnapshotId:
        host_id = host.id if isinstance(host, HostInterface) else host
        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.config is None or host_record.vps_ip is None:
            raise HostNotFoundError(host_id)

        docker_ssh = self._make_docker_ssh(host_record.vps_ip)
        snapshot_name = name or SnapshotName(f"mngr-snapshot-{host_id}-{int(time.time())}")
        image_tag = f"mngr-snapshot-{host_id.get_uuid().hex}-{int(time.time())}"

        with log_span("Creating Docker snapshot"):
            image_id = docker_ssh.commit_container(host_record.config.container_name, image_tag)

        # Store snapshot record in host data
        snapshot_record = SnapshotRecord(
            id=image_id,
            name=str(snapshot_name),
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        # Update certified data with new snapshot
        existing_snapshots = host_record.certified_host_data.snapshots
        updated_snapshots = list(existing_snapshots) + [snapshot_record]
        updated_data = host_record.certified_host_data.model_copy(
            update={"snapshots": updated_snapshots, "updated_at": datetime.now(timezone.utc)}
        )
        updated_record = host_record.model_copy(update={"certified_host_data": updated_data})

        host_store = self._get_host_store(docker_ssh)
        host_store.write_host_record(updated_record)

        logger.info("Created snapshot {} for host {}", snapshot_name, host_id)
        return SnapshotId(image_id)

    def list_snapshots(self, host: HostInterface | HostId) -> list[SnapshotInfo]:
        host_id = host.id if isinstance(host, HostInterface) else host
        host_record = self._find_host_record(host_id)
        if host_record is None:
            return []

        snapshots = host_record.certified_host_data.snapshots
        return [
            SnapshotInfo(
                id=SnapshotId(s.id),
                name=SnapshotName(s.name),
                created_at=datetime.fromisoformat(s.created_at),
            )
            for s in snapshots
        ]

    def delete_snapshot(self, host: HostInterface | HostId, snapshot_id: SnapshotId) -> None:
        host_id = host.id if isinstance(host, HostInterface) else host
        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.vps_ip is None:
            raise HostNotFoundError(host_id)

        docker_ssh = self._make_docker_ssh(host_record.vps_ip)
        try:
            docker_ssh.run_docker(["rmi", str(snapshot_id)])
        except ContainerSetupError as e:
            logger.warning("Failed to delete snapshot image: {}", e)

    # =========================================================================
    # Tags
    # =========================================================================

    def get_host_tags(self, host: HostInterface | HostId) -> dict[str, str]:
        host_id = host.id if isinstance(host, HostInterface) else host
        host_record = self._find_host_record(host_id)
        if host_record is None:
            return {}
        return dict(host_record.certified_host_data.user_tags)

    def set_host_tags(self, host: HostInterface | HostId, tags: Mapping[str, str]) -> None:
        raise MngrError("VPS Docker provider does not support mutable tags")

    def add_tags_to_host(self, host: HostInterface | HostId, tags: Mapping[str, str]) -> None:
        raise MngrError("VPS Docker provider does not support mutable tags")

    def remove_tags_from_host(self, host: HostInterface | HostId, keys: Sequence[str]) -> None:
        raise MngrError("VPS Docker provider does not support mutable tags")

    def rename_host(self, host: HostInterface | HostId, name: HostName) -> HostInterface:
        host_id = host.id if isinstance(host, HostInterface) else host
        host_record = self._find_host_record(host_id)
        if host_record is None:
            raise HostNotFoundError(host_id)

        updated_data = host_record.certified_host_data.model_copy(
            update={"host_name": str(name), "updated_at": datetime.now(timezone.utc)}
        )
        updated_record = host_record.model_copy(update={"certified_host_data": updated_data})

        if host_record.vps_ip is not None:
            docker_ssh = self._make_docker_ssh(host_record.vps_ip)
            host_store = self._get_host_store(docker_ssh)
            host_store.write_host_record(updated_record)

        return self.get_host(host_id)

    # =========================================================================
    # Volumes
    # =========================================================================

    def list_volumes(self) -> list[VolumeInfo]:
        return []

    def delete_volume(self, volume_id: VolumeId) -> None:
        pass

    # =========================================================================
    # Resources
    # =========================================================================

    def get_host_resources(self, host: HostInterface) -> HostResources:
        return HostResources(
            cpu=CpuResources(count=1, frequency_ghz=None),
            memory_gb=1.0,
            disk_gb=None,
            gpu=None,
        )

    # =========================================================================
    # Connector
    # =========================================================================

    def get_connector(self, host: HostInterface | HostId) -> PyinfraHost:
        resolved = self.get_host(host.id if isinstance(host, HostInterface) else host)
        if isinstance(resolved, Host):
            return resolved.connector.host
        raise MngrError("Cannot get connector for offline host")

    # =========================================================================
    # Agent Data Persistence
    # =========================================================================

    def list_persisted_agent_data_for_host(self, host_id: HostId) -> list[dict]:
        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.vps_ip is None:
            return []

        try:
            docker_ssh = self._make_docker_ssh(host_record.vps_ip)
            host_store = self._get_host_store(docker_ssh)
            return host_store.list_persisted_agent_data_for_host(host_id)
        except (VpsConnectionError, ContainerSetupError) as e:
            logger.warning("Failed to read persisted agent data: {}", e)
            return []

    def persist_agent_data(self, host_id: HostId, agent_data: Mapping[str, object]) -> None:
        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.vps_ip is None:
            return

        try:
            docker_ssh = self._make_docker_ssh(host_record.vps_ip)
            host_store = self._get_host_store(docker_ssh)
            host_store.persist_agent_data(host_id, agent_data)
        except (VpsConnectionError, ContainerSetupError) as e:
            logger.warning("Failed to persist agent data: {}", e)

    def remove_persisted_agent_data(self, host_id: HostId, agent_id: AgentId) -> None:
        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.vps_ip is None:
            return

        try:
            docker_ssh = self._make_docker_ssh(host_record.vps_ip)
            host_store = self._get_host_store(docker_ssh)
            host_store.remove_persisted_agent_data(host_id, agent_id)
        except (VpsConnectionError, ContainerSetupError) as e:
            logger.warning("Failed to remove agent data: {}", e)
