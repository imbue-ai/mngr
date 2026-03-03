import json
import os
import subprocess
import tempfile
from datetime import datetime
from datetime import timezone
from functools import cached_property
from pathlib import Path
from typing import Any
from typing import Final
from typing import Mapping
from typing import Sequence
from urllib.parse import urlparse
from uuid import uuid4

import docker
import docker.errors
import docker.models.containers
import docker.models.images
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pyinfra.api import Host as PyinfraHost

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.model_update import to_update
from imbue.mng.errors import HostNotFoundError
from imbue.mng.errors import MngError
from imbue.mng.errors import SnapshotNotFoundError
from imbue.mng.hosts.host import Host
from imbue.mng.hosts.offline_host import OfflineHost
from imbue.mng.interfaces.data_types import CertifiedHostData
from imbue.mng.interfaces.data_types import CpuResources
from imbue.mng.interfaces.data_types import HostLifecycleOptions
from imbue.mng.interfaces.data_types import HostResources
from imbue.mng.interfaces.data_types import PyinfraConnector
from imbue.mng.interfaces.data_types import SnapshotInfo
from imbue.mng.interfaces.data_types import SnapshotRecord
from imbue.mng.interfaces.data_types import VolumeFileType
from imbue.mng.interfaces.data_types import VolumeInfo
from imbue.mng.interfaces.host import HostInterface
from imbue.mng.interfaces.volume import HostVolume
from imbue.mng.primitives import ActivitySource
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.primitives import HostState
from imbue.mng.primitives import ImageReference
from imbue.mng.primitives import SnapshotId
from imbue.mng.primitives import SnapshotName
from imbue.mng.primitives import VolumeId
from imbue.mng.providers.base_provider import BaseProviderInstance
from imbue.mng.providers.docker.config import DockerProviderConfig
from imbue.mng.providers.docker.host_store import ContainerConfig
from imbue.mng.providers.docker.host_store import DockerHostStore
from imbue.mng.providers.docker.host_store import HostRecord
from imbue.mng.providers.docker.volume import CONTAINER_ENTRYPOINT_CMD
from imbue.mng.providers.docker.volume import DockerVolume
from imbue.mng.providers.docker.volume import STATE_VOLUME_MOUNT_PATH
from imbue.mng.providers.docker.volume import ensure_state_container
from imbue.mng.providers.docker.volume import state_volume_name
from imbue.mng.providers.ssh_host_setup import REQUIRED_HOST_PACKAGES
from imbue.mng.providers.ssh_host_setup import build_add_authorized_keys_command
from imbue.mng.providers.ssh_host_setup import build_add_known_hosts_command
from imbue.mng.providers.ssh_host_setup import build_check_and_install_packages_command
from imbue.mng.providers.ssh_host_setup import build_configure_ssh_command
from imbue.mng.providers.ssh_host_setup import build_start_activity_watcher_command
from imbue.mng.providers.ssh_host_setup import parse_warnings_from_output
from imbue.mng.providers.ssh_utils import add_host_to_known_hosts
from imbue.mng.providers.ssh_utils import create_pyinfra_host
from imbue.mng.providers.ssh_utils import load_or_create_host_keypair
from imbue.mng.providers.ssh_utils import load_or_create_ssh_keypair
from imbue.mng.providers.ssh_utils import wait_for_sshd

# Container entrypoint as SDK-style command tuple (used by tests)
CONTAINER_ENTRYPOINT: Final[tuple[str, ...]] = ("sh", "-c", CONTAINER_ENTRYPOINT_CMD)

# Fallback base image when no image is specified by the user or provider config.
DEFAULT_IMAGE: Final[str] = "debian:bookworm-slim"


def _build_default_dockerfile() -> str:
    """Build the default Dockerfile contents from REQUIRED_HOST_PACKAGES."""
    packages = " \\\n    ".join(sorted(pkg.package for pkg in REQUIRED_HOST_PACKAGES))
    return f"""\
FROM {DEFAULT_IMAGE}

RUN apt-get update && apt-get install -y --no-install-recommends \\
    {packages} \\
    && rm -rf /var/lib/apt/lists/*
"""


# Minimal Dockerfile that pre-installs the packages mng requires at runtime.
# Using this as the default avoids slow runtime installs on every host create.
# Derived from REQUIRED_HOST_PACKAGES so the two stay in sync.
DEFAULT_DOCKERFILE_CONTENTS: Final[str] = _build_default_dockerfile()

# Docker label prefix
LABEL_PREFIX: Final[str] = "com.imbue.mng."
LABEL_HOST_ID: Final[str] = f"{LABEL_PREFIX}host-id"
LABEL_HOST_NAME: Final[str] = f"{LABEL_PREFIX}host-name"
LABEL_PROVIDER: Final[str] = f"{LABEL_PREFIX}provider"
LABEL_TAGS: Final[str] = f"{LABEL_PREFIX}tags"

# Path where the state volume is mounted inside host containers (when host volume is enabled).
# The host_dir (e.g. /mng) is symlinked to <this>/<host_id> so all data persists on the volume.
HOST_VOLUME_MOUNT_PATH: Final[str] = STATE_VOLUME_MOUNT_PATH

# SSH constants
CONTAINER_SSH_PORT: Final[int] = 22
SSH_CONNECT_TIMEOUT: Final[float] = 60


def build_container_labels(
    host_id: HostId,
    name: HostName,
    provider_name: str,
    user_tags: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build Docker container labels for discovery and metadata."""
    labels: dict[str, str] = {
        LABEL_HOST_ID: str(host_id),
        LABEL_HOST_NAME: str(name),
        LABEL_PROVIDER: provider_name,
        LABEL_TAGS: json.dumps(dict(user_tags) if user_tags else {}),
    }
    return labels


def parse_container_labels(
    labels: dict[str, str],
) -> tuple[HostId, HostName, str, dict[str, str]]:
    """Parse Docker container labels into structured data.

    Returns (host_id, host_name, provider_name, user_tags).
    """
    host_id = HostId(labels[LABEL_HOST_ID])
    host_name = HostName(labels[LABEL_HOST_NAME])
    provider_name = labels[LABEL_PROVIDER]

    tags_json = labels.get(LABEL_TAGS, "{}")
    try:
        user_tags = json.loads(tags_json)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Invalid JSON in container tags label: {}", tags_json)
        user_tags = {}

    return host_id, host_name, provider_name, user_tags


def _get_ssh_host_from_docker_config(docker_host_url: str) -> str:
    """Extract the SSH-reachable hostname from a Docker host URL.

    For local Docker (empty string or unix socket), returns 127.0.0.1.
    For remote Docker (ssh:// or tcp://), returns the hostname from the URL.
    """
    if not docker_host_url or docker_host_url.startswith("unix://"):
        return "127.0.0.1"

    parsed = urlparse(docker_host_url)
    if parsed.hostname:
        return parsed.hostname

    return "127.0.0.1"


class DockerProviderInstance(BaseProviderInstance):
    """Provider instance for managing Docker containers as hosts.

    Each container runs sshd and is accessed via pyinfra's SSH connector.
    Containers have a long-running PID 1 process and can be stopped/started
    natively (unlike Modal which must terminate and recreate from snapshots).

    Host metadata (SSH info, config, snapshots) is stored on a Docker named
    volume via a singleton state container, allowing multiple mng clients
    to share state. Container labels are used for discovery and immutable tags.
    """

    config: DockerProviderConfig = Field(frozen=True, description="Docker provider configuration")

    # Instance-level caches
    _container_cache_by_id: dict[HostId, docker.models.containers.Container] = PrivateAttr(default_factory=dict)
    _host_by_id_cache: dict[HostId, HostInterface] = PrivateAttr(default_factory=dict)

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

    @cached_property
    def _docker_client(self) -> docker.DockerClient:
        """Lazily create a Docker client."""
        if self.config.host:
            return docker.DockerClient(base_url=self.config.host)
        return docker.from_env()

    @cached_property
    def _state_volume(self) -> DockerVolume:
        """Get the state volume backed by the singleton state container."""
        user_id = str(self.mng_ctx.get_profile_user_id())
        prefix = self.mng_ctx.config.prefix
        state_container = ensure_state_container(self._docker_client, prefix, user_id)
        return DockerVolume(container=state_container)

    @cached_property
    def _state_volume_name(self) -> str:
        """Get the Docker named volume name for the state volume."""
        user_id = str(self.mng_ctx.get_profile_user_id())
        prefix = self.mng_ctx.config.prefix
        return state_volume_name(prefix, user_id)

    @cached_property
    def _host_store(self) -> DockerHostStore:
        """Get the host record store backed by the state volume."""
        return DockerHostStore(volume=self._state_volume)

    @property
    def _keys_dir(self) -> Path:
        """Get the directory for SSH keys (profile-specific)."""
        return self.mng_ctx.profile_dir / "providers" / "docker" / str(self.name) / "keys"

    @property
    def _known_hosts_path(self) -> Path:
        """Get the path to the known_hosts file for this provider instance."""
        return self._keys_dir / "known_hosts"

    def _get_host_volume_mount(self) -> str | None:
        """Build a -v flag value to mount the state volume into host containers.

        Returns None when host volumes are disabled. When enabled, returns
        '<volume_name>:<mount_path>:rw' so the container can read/write the
        shared state volume (where per-host data lives under volumes/vol-<host_hex>/).
        """
        if not self.config.is_host_volume_created:
            return None
        return f"{self._state_volume_name}:{HOST_VOLUME_MOUNT_PATH}:rw"

    def _get_host_volume_symlink_target(self, host_id: HostId) -> str | None:
        """Get the path inside a container that host_dir should symlink to.

        Returns the per-host sub-folder of the volume mount, e.g.
        /mng-state/volumes/vol-<host_hex>. Returns None when host volumes are disabled.
        """
        if not self.config.is_host_volume_created:
            return None
        volume_id = self._volume_id_for_host(host_id)
        return f"{HOST_VOLUME_MOUNT_PATH}/volumes/{volume_id}"

    def _get_ssh_keypair(self) -> tuple[Path, str]:
        """Get or create the SSH keypair for this provider instance."""
        return load_or_create_ssh_keypair(self._keys_dir, key_name="docker_ssh_key")

    def _get_host_keypair(self) -> tuple[Path, str]:
        """Get or create the SSH host keypair for Docker containers."""
        return load_or_create_host_keypair(self._keys_dir)

    def _get_ssh_host(self) -> str:
        """Get the SSH-reachable hostname for containers."""
        return _get_ssh_host_from_docker_config(self.config.host)

    # =========================================================================
    # Docker Exec Helpers
    # =========================================================================

    def _exec_in_container(
        self,
        container: docker.models.containers.Container,
        command: str,
        detach: bool = False,
    ) -> tuple[int, str]:
        """Execute a command in a Docker container via docker exec.

        Returns (exit_code, output). For detached commands, returns (0, "").
        """
        if detach:
            container.exec_run(["sh", "-c", command], detach=True)
            return 0, ""

        exit_code, output = container.exec_run(["sh", "-c", command])
        output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)
        return exit_code, output_str

    def _check_and_install_packages(
        self,
        container: docker.models.containers.Container,
        host_volume_mount_path: str | None = None,
    ) -> None:
        """Check for required packages and install if missing, with warnings.

        When host_volume_mount_path is provided, host_dir is set up as a symlink
        to the volume path so data persists on the shared Docker volume.
        """
        check_install_cmd = build_check_and_install_packages_command(
            str(self.host_dir),
            host_volume_mount_path=host_volume_mount_path,
        )
        exit_code, output = self._exec_in_container(container, check_install_cmd)
        if exit_code != 0:
            raise MngError(f"Failed to install required packages (exit code {exit_code}): {output}")
        warnings = parse_warnings_from_output(output)
        for warning in warnings:
            logger.warning(warning)

    def _start_sshd_in_container(
        self,
        container: docker.models.containers.Container,
        client_public_key: str,
        host_private_key: str,
        host_public_key: str,
        ssh_user: str = "root",
        known_hosts: Sequence[str] | None = None,
        authorized_keys: Sequence[str] | None = None,
        host_volume_mount_path: str | None = None,
    ) -> None:
        """Set up SSH access and start sshd in the container."""
        self._check_and_install_packages(container, host_volume_mount_path=host_volume_mount_path)

        with log_span("Configuring SSH keys in container", ssh_user=ssh_user):
            configure_ssh_cmd = build_configure_ssh_command(
                user=ssh_user,
                client_public_key=client_public_key,
                host_private_key=host_private_key,
                host_public_key=host_public_key,
            )
            exit_code, output = self._exec_in_container(container, configure_ssh_cmd)
            if exit_code != 0:
                raise MngError(f"Failed to configure SSH (exit code {exit_code}): {output}")

        if known_hosts:
            add_known_hosts_cmd = build_add_known_hosts_command(ssh_user, tuple(known_hosts))
            if add_known_hosts_cmd is not None:
                with log_span("Adding {} known_hosts entries to container", len(known_hosts)):
                    self._exec_in_container(container, add_known_hosts_cmd)

        if authorized_keys:
            add_authorized_keys_cmd = build_add_authorized_keys_command(ssh_user, tuple(authorized_keys))
            if add_authorized_keys_cmd is not None:
                with log_span("Adding {} authorized_keys entries to container", len(authorized_keys)):
                    self._exec_in_container(container, add_authorized_keys_cmd)

        with log_span("Starting sshd in container"):
            self._exec_in_container(container, "/usr/sbin/sshd -D", detach=True)

    def _get_container_ssh_port(self, container: docker.models.containers.Container) -> int:
        """Get the host-mapped SSH port for a container."""
        container.reload()
        ports = container.ports
        ssh_bindings = ports.get("22/tcp")
        if not ssh_bindings:
            raise MngError(f"Container {container.id} has no SSH port mapping")
        return int(ssh_bindings[0]["HostPort"])

    def _wait_for_sshd(self, hostname: str, port: int, timeout_seconds: float = SSH_CONNECT_TIMEOUT) -> None:
        """Wait for sshd to be ready to accept connections."""
        wait_for_sshd(hostname, port, timeout_seconds)

    def _create_pyinfra_host(self, hostname: str, port: int, private_key_path: Path) -> PyinfraHost:
        """Create a pyinfra host with SSH connector."""
        return create_pyinfra_host(hostname, port, private_key_path, self._known_hosts_path)

    # =========================================================================
    # Container Setup and Host Creation Helpers
    # =========================================================================

    def _setup_container_ssh_and_create_host(
        self,
        container: docker.models.containers.Container,
        host_id: HostId,
        host_name: HostName,
        user_tags: Mapping[str, str] | None,
        config: ContainerConfig,
        host_data: CertifiedHostData,
        known_hosts: Sequence[str] | None = None,
        authorized_keys: Sequence[str] | None = None,
    ) -> tuple[Host, str, int, str]:
        """Set up SSH in a container and create a Host object.

        Returns (Host, ssh_host, ssh_port, host_public_key).
        """
        private_key_path, client_public_key = self._get_ssh_keypair()
        host_key_path, host_public_key = self._get_host_keypair()
        host_private_key = host_key_path.read_text()

        host_volume_symlink_target = self._get_host_volume_symlink_target(host_id)
        self._start_sshd_in_container(
            container,
            client_public_key,
            host_private_key,
            host_public_key,
            known_hosts=known_hosts,
            authorized_keys=authorized_keys,
            host_volume_mount_path=host_volume_symlink_target,
        )

        ssh_host = self._get_ssh_host()
        ssh_port = self._get_container_ssh_port(container)
        logger.trace("Found SSH endpoint available", ssh_host=ssh_host, ssh_port=ssh_port)

        with log_span("Adding host to known_hosts", ssh_host=ssh_host, ssh_port=ssh_port):
            add_host_to_known_hosts(self._known_hosts_path, ssh_host, ssh_port, host_public_key)

        with log_span("Waiting for sshd to be ready..."):
            self._wait_for_sshd(ssh_host, ssh_port)

        pyinfra_host = self._create_pyinfra_host(ssh_host, ssh_port, private_key_path)
        connector = PyinfraConnector(pyinfra_host)

        host_record = HostRecord(
            ssh_host=ssh_host,
            ssh_port=ssh_port,
            ssh_host_public_key=host_public_key,
            config=config,
            certified_host_data=host_data,
            container_id=container.id,
        )
        self._host_store.write_host_record(host_record)

        host = Host(
            id=host_id,
            connector=connector,
            provider_instance=self,
            mng_ctx=self.mng_ctx,
            on_updated_host_data=lambda callback_host_id, certified_data: self._on_certified_host_data_updated(
                callback_host_id, certified_data
            ),
        )

        host.record_activity(ActivitySource.BOOT)
        host.set_certified_data(host_data)

        self._create_shutdown_script(host)

        with log_span("Starting activity watcher in container"):
            start_activity_watcher_cmd = build_start_activity_watcher_command(str(self.host_dir))
            self._exec_in_container(container, start_activity_watcher_cmd)

        return host, ssh_host, ssh_port, host_public_key

    def _create_shutdown_script(self, host: Host) -> None:
        """Create the shutdown.sh script on the host.

        For Docker, the shutdown script kills PID 1 to stop the container.
        """
        host_dir_str = str(host.host_dir)

        script_content = f'''#!/bin/bash
# Auto-generated shutdown script for mng Docker host
# Kills PID 1 to stop the container

LOG_FILE="{host_dir_str}/logs/shutdown.log"
mkdir -p "$(dirname "$LOG_FILE")"

log() {{
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_FILE"
    echo "$*"
}}

log "=== Shutdown script started ==="
log "STOP_REASON: ${{1:-PAUSED}}"

# Kill PID 1 to stop the container
# The entrypoint traps SIGTERM and exits cleanly
kill -TERM 1
'''

        commands_dir = host.host_dir / "commands"
        script_path = commands_dir / "shutdown.sh"

        with log_span("Creating shutdown script at {}", script_path):
            host.write_text_file(script_path, script_content, mode="755")

    def _on_certified_host_data_updated(self, host_id: HostId, certified_data: CertifiedHostData) -> None:
        """Update the certified host data in the host record."""
        with log_span("Updating certified host data", host_id=str(host_id)):
            host_record = self._host_store.read_host_record(host_id, use_cache=False)
            if host_record is None:
                raise HostNotFoundError(host_id)
            updated_host_record = host_record.model_copy_update(
                to_update(host_record.field_ref().certified_host_data, certified_data),
            )
            self._host_store.write_host_record(updated_host_record)

    def _save_failed_host_record(
        self,
        host_id: HostId,
        host_name: HostName,
        tags: Mapping[str, str] | None,
        failure_reason: str,
        build_log: str,
    ) -> None:
        """Save a host record for a host that failed during creation."""
        now = datetime.now(timezone.utc)
        host_data = CertifiedHostData(
            host_id=str(host_id),
            host_name=str(host_name),
            user_tags=dict(tags) if tags else {},
            snapshots=[],
            failure_reason=failure_reason,
            build_log=build_log,
            created_at=now,
            updated_at=now,
        )
        host_record = HostRecord(certified_host_data=host_data)
        with log_span("Saving failed host record for host_id={}", host_id):
            self._host_store.write_host_record(host_record)

    # =========================================================================
    # Docker CLI Subprocess Helpers
    # =========================================================================

    def _docker_env(self) -> dict[str, str]:
        """Build environment variables for docker subprocess calls."""
        env = dict(os.environ)
        if self.config.host:
            env["DOCKER_HOST"] = self.config.host
        return env

    def _run_docker_command(self, args: list[str], timeout: float = 300) -> subprocess.CompletedProcess[str]:
        """Run a docker CLI command and return the result."""
        return subprocess.run(
            ["docker"] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=self._docker_env(),
        )

    def _build_image(self, build_args: Sequence[str], tag: str) -> str:
        """Build a Docker image using native docker build with passthrough args."""
        cmd = ["build", "-t", tag] + list(build_args)
        with log_span("Running docker build with {} args", len(build_args)):
            result = self._run_docker_command(cmd)
        if result.returncode != 0:
            raise MngError(f"docker build failed:\n{result.stderr}")
        return tag

    def _build_default_image(self, tag: str) -> str:
        """Build a Docker image from the mng default Dockerfile."""
        with tempfile.TemporaryDirectory() as tmpdir:
            dockerfile_path = Path(tmpdir) / "Dockerfile"
            dockerfile_path.write_text(DEFAULT_DOCKERFILE_CONTENTS)
            return self._build_image(["--file", str(dockerfile_path), tmpdir], tag)

    def _pull_image(self, image_name: str) -> str:
        """Pull a Docker image if not already present locally."""
        with log_span("Pulling Docker image: {}", image_name):
            try:
                self._docker_client.images.pull(image_name)
            except docker.errors.ImageNotFound as e:
                raise MngError(f"Docker image not found: {image_name}") from e
            except docker.errors.APIError as e:
                raise MngError(f"Docker API error pulling image: {e}") from e
        return image_name

    def _build_docker_run_command(
        self,
        *,
        image: str,
        container_name: str,
        labels: dict[str, str],
        start_args: Sequence[str],
        volume_mount: str | None = None,
    ) -> list[str]:
        """Build a docker run command with mandatory flags + user passthrough args.

        When volume_mount is provided, adds -v <volume_mount> to mount the
        state volume into the container for persistent host data.
        """
        cmd = ["run", "-d", "--name", container_name, "-p", f":{CONTAINER_SSH_PORT}"]

        for key, value in labels.items():
            cmd.extend(["--label", f"{key}={value}"])

        if volume_mount is not None:
            cmd.extend(["-v", volume_mount])

        cmd.extend(list(start_args))
        cmd.extend(["--entrypoint", "sh", image, "-c", CONTAINER_ENTRYPOINT_CMD])
        return cmd

    def _run_container(
        self,
        *,
        image: str,
        container_name: str,
        labels: dict[str, str],
        start_args: Sequence[str],
        volume_mount: str | None = None,
    ) -> docker.models.containers.Container:
        """Create and start a container via docker run subprocess.

        Returns the SDK container object for subsequent management.
        """
        cmd = self._build_docker_run_command(
            image=image,
            container_name=container_name,
            labels=labels,
            start_args=start_args,
            volume_mount=volume_mount,
        )
        result = self._run_docker_command(cmd)
        if result.returncode != 0:
            raise MngError(f"docker run failed:\n{result.stderr}")

        container_id = result.stdout.strip()
        return self._docker_client.containers.get(container_id)

    # =========================================================================
    # Container Discovery Helpers
    # =========================================================================

    def _find_container_by_host_id(self, host_id: HostId) -> docker.models.containers.Container | None:
        """Find a Docker container by host_id label."""
        if host_id in self._container_cache_by_id:
            container = self._container_cache_by_id[host_id]
            try:
                container.reload()
                return container
            except docker.errors.NotFound:
                self._container_cache_by_id.pop(host_id, None)

        try:
            containers = self._docker_client.containers.list(
                all=True,
                filters={"label": [f"{LABEL_HOST_ID}={host_id}", f"{LABEL_PROVIDER}={self.name}"]},
            )
        except docker.errors.DockerException as e:
            raise MngError(f"Cannot connect to Docker daemon: {e}") from e

        if containers:
            container = containers[0]
            self._container_cache_by_id[host_id] = container
            return container
        return None

    def _find_container_by_name(self, name: HostName) -> docker.models.containers.Container | None:
        """Find a Docker container by host_name label."""
        try:
            containers = self._docker_client.containers.list(
                all=True,
                filters={"label": [f"{LABEL_HOST_NAME}={name}", f"{LABEL_PROVIDER}={self.name}"]},
            )
        except docker.errors.DockerException as e:
            raise MngError(f"Cannot connect to Docker daemon: {e}") from e

        return containers[0] if containers else None

    def _list_containers(self) -> list[docker.models.containers.Container]:
        """List all Docker containers managed by this provider instance."""
        try:
            containers = self._docker_client.containers.list(
                all=True,
                filters={"label": [f"{LABEL_PROVIDER}={self.name}"]},
            )
        except docker.errors.DockerException as e:
            raise MngError(f"Cannot connect to Docker daemon: {e}") from e
        return containers

    def _is_container_running(self, container: docker.models.containers.Container) -> bool:
        """Check if a container is running."""
        container.reload()
        return container.status == "running"

    def _create_host_from_container(
        self,
        container: docker.models.containers.Container,
    ) -> Host | None:
        """Create a Host object from a running Docker container.

        Returns None if the host record doesn't exist.
        """
        labels = container.labels or {}
        host_id, name, provider_name, user_tags = parse_container_labels(labels)

        host_record = self._host_store.read_host_record(host_id, use_cache=False)
        if host_record is None:
            logger.warning("Skipped container {}: no host record", container.short_id)
            return None

        if host_record.ssh_host is None or host_record.ssh_port is None or host_record.ssh_host_public_key is None:
            logger.warning("Skipped container {}: missing SSH info (likely failed host)", container.short_id)
            return None

        add_host_to_known_hosts(
            self._known_hosts_path,
            host_record.ssh_host,
            host_record.ssh_port,
            host_record.ssh_host_public_key,
        )

        private_key_path, _ = self._get_ssh_keypair()
        pyinfra_host = self._create_pyinfra_host(
            host_record.ssh_host,
            host_record.ssh_port,
            private_key_path,
        )
        connector = PyinfraConnector(pyinfra_host)

        return Host(
            id=host_id,
            connector=connector,
            provider_instance=self,
            mng_ctx=self.mng_ctx,
            on_updated_host_data=lambda callback_host_id, certified_data: self._on_certified_host_data_updated(
                callback_host_id, certified_data
            ),
        )

    def _create_host_from_host_record(
        self,
        host_record: HostRecord,
    ) -> OfflineHost:
        """Create an OfflineHost from a host record (for stopped/destroyed hosts)."""
        host_id = HostId(host_record.certified_host_data.host_id)
        return OfflineHost(
            id=host_id,
            certified_host_data=host_record.certified_host_data,
            provider_instance=self,
            mng_ctx=self.mng_ctx,
            on_updated_host_data=lambda callback_host_id, certified_data: self._on_certified_host_data_updated(
                callback_host_id, certified_data
            ),
        )

    # =========================================================================
    # Core Lifecycle Methods
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
        """Create a new Docker container host.

        Build args are passed through to 'docker build' (if provided).
        Start args are passed through to 'docker run' for resource limits,
        volumes, ports, network, etc.
        """
        host_id = HostId.generate()
        logger.info("Creating host {} in {} ...", name, self.name)

        base_image = str(image) if image else (self.config.default_image or DEFAULT_IMAGE)
        effective_start_args = tuple(self.config.default_start_args) + tuple(start_args or ())

        # Detect whether we're falling through to the default with no user customization
        is_using_default = not image and not build_args and not self.config.default_image
        if is_using_default:
            logger.warning(
                "No image or Dockerfile specified -- building from mng default Dockerfile. "
                "Consider using your own Dockerfile (-b --file=<path>) to include "
                "your project's dependencies for faster startup.",
            )

        try:
            if build_args:
                # Build image from user-provided build args / Dockerfile
                build_tag = f"mng-build-{host_id}"
                image_name = self._build_image(build_args, build_tag)
            elif is_using_default:
                # Build from the mng default Dockerfile so packages are pre-installed
                build_tag = f"mng-build-{host_id}"
                image_name = self._build_default_image(build_tag)
            else:
                # User specified an image (via --image or config default_image); pull it
                image_name = self._pull_image(base_image)

            labels = build_container_labels(host_id, name, str(self.name), tags)
            container_name = f"{self.mng_ctx.config.prefix}{name}"

            # Create the per-host volume directory before starting the container
            # so the symlink target exists when the setup script runs.
            if self.config.is_host_volume_created:
                self._ensure_host_volume_dir(host_id)

            with log_span("Creating Docker container", container_name=container_name):
                container = self._run_container(
                    image=image_name,
                    container_name=container_name,
                    labels=labels,
                    start_args=effective_start_args,
                    volume_mount=self._get_host_volume_mount(),
                )

        except docker.errors.APIError as e:
            failure_reason = str(e)
            logger.error("Host creation failed: {}", failure_reason)
            self._save_failed_host_record(
                host_id=host_id,
                host_name=name,
                tags=tags,
                failure_reason=failure_reason,
                build_log="",
            )
            raise MngError(f"Failed to create Docker container: {e}") from e
        except MngError as e:
            self._save_failed_host_record(
                host_id=host_id,
                host_name=name,
                tags=tags,
                failure_reason=str(e),
                build_log="",
            )
            raise

        self._container_cache_by_id[host_id] = container
        config = ContainerConfig(start_args=effective_start_args, image=base_image)

        lifecycle_options = lifecycle if lifecycle is not None else HostLifecycleOptions()
        activity_config = lifecycle_options.to_activity_config(
            default_idle_timeout_seconds=self.config.default_idle_timeout,
            default_idle_mode=self.config.default_idle_mode,
            default_activity_sources=self.config.default_activity_sources,
        )

        now = datetime.now(timezone.utc)
        host_data = CertifiedHostData(
            idle_timeout_seconds=activity_config.idle_timeout_seconds,
            activity_sources=activity_config.activity_sources,
            host_id=str(host_id),
            host_name=str(name),
            user_tags=dict(tags) if tags else {},
            snapshots=[],
            tmux_session_prefix=self.mng_ctx.config.prefix,
            created_at=now,
            updated_at=now,
        )

        try:
            host, ssh_host, ssh_port, host_public_key = self._setup_container_ssh_and_create_host(
                container=container,
                host_id=host_id,
                host_name=name,
                user_tags=tags,
                config=config,
                host_data=host_data,
                known_hosts=known_hosts,
                authorized_keys=authorized_keys,
            )
        except (MngError, docker.errors.DockerException, OSError) as e:
            # Clean up the container on SSH setup failure to avoid orphans
            logger.warning("SSH setup failed, removing container: {}", e)
            try:
                container.remove(force=True)
            except docker.errors.DockerException:
                pass
            self._container_cache_by_id.pop(host_id, None)
            self._save_failed_host_record(
                host_id=host_id,
                host_name=name,
                tags=tags,
                failure_reason=str(e),
                build_log="",
            )
            raise MngError(f"SSH setup failed for container {host_id}: {e}") from e

        return host

    def stop_host(
        self,
        host: HostInterface | HostId,
        create_snapshot: bool = True,
        timeout_seconds: float = 60.0,
    ) -> None:
        """Stop a Docker container.

        Unlike Modal, Docker supports native stop/start, so the container is
        stopped (not removed) and can be started again.
        """
        host_id = host.id if isinstance(host, HostInterface) else host
        logger.info("Stopping Docker container: {}", host_id)

        # Disconnect SSH before stopping
        cached_host = self._host_by_id_cache.get(host_id)
        host_to_disconnect = cached_host if cached_host is not None else host
        if isinstance(host_to_disconnect, Host):
            host_to_disconnect.disconnect()

        container = self._find_container_by_host_id(host_id)
        if container is not None:
            if create_snapshot and self._is_container_running(container):
                try:
                    with log_span("Creating snapshot before stop", host_id=str(host_id)):
                        self.create_snapshot(host_id, SnapshotName(f"stop-{uuid4().hex}"))
                except (MngError, docker.errors.DockerException) as e:
                    logger.warning("Failed to create snapshot before stop: {}", e)

            try:
                container.stop(timeout=int(timeout_seconds))
            except docker.errors.DockerException as e:
                logger.warning("Error stopping container: {}", e)
        else:
            logger.debug("Container not found (may already be stopped)", host_id=str(host_id))

        # Update host record with stop reason
        host_record = self._host_store.read_host_record(host_id, use_cache=False)
        if host_record is not None:
            updated_certified_data = host_record.certified_host_data.model_copy_update(
                to_update(host_record.certified_host_data.field_ref().stop_reason, HostState.STOPPED.value),
            )
            self._host_store.write_host_record(
                host_record.model_copy_update(
                    to_update(host_record.field_ref().certified_host_data, updated_certified_data),
                )
            )

        self._container_cache_by_id.pop(host_id, None)
        self._host_by_id_cache.pop(host_id, None)

    def start_host(
        self,
        host: HostInterface | HostId,
        snapshot_id: SnapshotId | None = None,
    ) -> Host:
        """Start a stopped Docker container, optionally from a snapshot.

        If the container is already running, returns the existing host.
        If snapshot_id is provided, creates a new container from the snapshot image.
        Otherwise, restarts the stopped container (preserving filesystem state).
        """
        host_id = host.id if isinstance(host, HostInterface) else host

        container = self._find_container_by_host_id(host_id)

        # If container is running, return existing host
        if container is not None and self._is_container_running(container):
            host_obj = self._create_host_from_container(container)
            if host_obj is not None:
                if snapshot_id is not None:
                    logger.warning(
                        "Container {} is already running; ignoring snapshot_id. "
                        "Stop the host first to restore from a snapshot.",
                        host_id,
                    )
                return host_obj

        # Check for failed host
        host_record = self._host_store.read_host_record(host_id, use_cache=False)
        if host_record is not None and host_record.certified_host_data.failure_reason is not None:
            raise MngError(
                f"Host {host_id} failed during creation and cannot be started. "
                f"Reason: {host_record.certified_host_data.failure_reason}"
            )

        if snapshot_id is not None:
            # Create a new container from the snapshot image
            return self._start_from_snapshot(host_id, snapshot_id, host_record)

        # Native restart: just start the stopped container
        if container is not None:
            with log_span("Starting stopped container", host_id=str(host_id)):
                container.start()

            self._container_cache_by_id[host_id] = container
            self._host_by_id_cache.pop(host_id, None)

            if host_record is None:
                raise HostNotFoundError(host_id)

            config = host_record.config
            if config is None:
                raise MngError(f"Host {host_id} has no configuration and cannot be started.")

            host_name = HostName(host_record.certified_host_data.host_name)
            user_tags = host_record.certified_host_data.user_tags

            restored_host, _, _, _ = self._setup_container_ssh_and_create_host(
                container=container,
                host_id=host_id,
                host_name=host_name,
                user_tags=user_tags,
                config=config,
                host_data=host_record.certified_host_data,
            )

            self._host_by_id_cache[host_id] = restored_host
            return restored_host

        # No container found, try snapshot restore
        if host_record is None:
            raise HostNotFoundError(host_id)

        if not host_record.certified_host_data.snapshots:
            raise MngError(
                f"Docker container {host_id} is not found and has no snapshots. "
                "Cannot restart. Create a new host instead."
            )

        # Use most recent snapshot
        sorted_snapshots = sorted(host_record.certified_host_data.snapshots, key=lambda s: s.created_at, reverse=True)
        return self._start_from_snapshot(host_id, SnapshotId(sorted_snapshots[0].id), host_record)

    def _start_from_snapshot(
        self,
        host_id: HostId,
        snapshot_id: SnapshotId,
        host_record: HostRecord | None,
    ) -> Host:
        """Start a host from a snapshot image."""
        if host_record is None:
            host_record = self._host_store.read_host_record(host_id, use_cache=False)
        if host_record is None:
            raise HostNotFoundError(host_id)

        snapshot_data: SnapshotRecord | None = None
        for snap in host_record.certified_host_data.snapshots:
            if snap.id == str(snapshot_id):
                snapshot_data = snap
                break

        if snapshot_data is None:
            raise SnapshotNotFoundError(snapshot_id)

        config = host_record.config
        if config is None:
            raise MngError(f"Host {host_id} has no configuration.")

        host_name = HostName(host_record.certified_host_data.host_name)
        user_tags = host_record.certified_host_data.user_tags

        # Remove old container if it exists
        old_container = self._find_container_by_host_id(host_id)
        if old_container is not None:
            try:
                old_container.remove(force=True)
            except docker.errors.DockerException as e:
                logger.warning("Error removing old container before snapshot restore: {}", e)

        # Create new container from snapshot image
        image_id = snapshot_data.id
        logger.info("Restoring Docker container from snapshot", host_id=str(host_id), snapshot_id=str(snapshot_id))

        labels = build_container_labels(host_id, host_name, str(self.name), user_tags)
        container_name = f"{self.mng_ctx.config.prefix}{host_name}"

        effective_start_args = config.start_args

        try:
            new_container = self._run_container(
                image=image_id,
                container_name=container_name,
                labels=labels,
                start_args=effective_start_args,
                volume_mount=self._get_host_volume_mount(),
            )
        except (MngError, docker.errors.DockerException) as e:
            raise MngError(f"Failed to create container from snapshot: {e}") from e

        self._container_cache_by_id[host_id] = new_container
        self._host_by_id_cache.pop(host_id, None)

        restored_host, _, _, _ = self._setup_container_ssh_and_create_host(
            container=new_container,
            host_id=host_id,
            host_name=host_name,
            user_tags=user_tags,
            config=config,
            host_data=host_record.certified_host_data,
        )

        self._host_by_id_cache[host_id] = restored_host
        return restored_host

    def destroy_host(
        self,
        host: HostInterface | HostId,
        delete_snapshots: bool = True,
    ) -> None:
        """Destroy a Docker container permanently."""
        host_id = host.id if isinstance(host, HostInterface) else host

        # Stop the host first (without creating a snapshot since we're destroying)
        self.stop_host(host, create_snapshot=False)

        # Remove the container
        container = self._find_container_by_host_id(host_id)
        if container is not None:
            try:
                container.remove(force=True)
            except docker.errors.DockerException as e:
                logger.warning("Error removing container: {}", e)

        if delete_snapshots:
            # Delete snapshot images
            host_record = self._host_store.read_host_record(host_id)
            if host_record is not None:
                for snap in host_record.certified_host_data.snapshots:
                    try:
                        self._docker_client.images.remove(snap.id)
                    except docker.errors.DockerException as e:
                        logger.warning("Error removing snapshot image {}: {}", snap.id, e)

            self._host_store.delete_host_record(host_id)

        # Clean up the host volume directory
        if self.config.is_host_volume_created:
            volume_id = self._volume_id_for_host(host_id)
            try:
                self._state_volume.remove_directory(f"volumes/{volume_id}")
            except (FileNotFoundError, OSError, MngError) as e:
                logger.trace("No host volume to clean up for {}: {}", host_id, e)

        self._container_cache_by_id.pop(host_id, None)
        self._host_by_id_cache.pop(host_id, None)

    def delete_host(self, host: HostInterface) -> None:
        """Permanently delete all records associated with a (destroyed) host."""
        self._host_store.delete_host_record(host.id)
        self._container_cache_by_id.pop(host.id, None)
        self._host_by_id_cache.pop(host.id, None)

    def on_connection_error(self, host_id: HostId) -> None:
        """Clear all caches for a host on connection error."""
        self._container_cache_by_id.pop(host_id, None)
        self._host_by_id_cache.pop(host_id, None)
        self._host_store.clear_cache()

    # =========================================================================
    # Discovery Methods
    # =========================================================================

    def get_host(
        self,
        host: HostId | HostName,
    ) -> HostInterface:
        """Get a host by ID or name."""
        if isinstance(host, HostId) and host in self._host_by_id_cache:
            return self._host_by_id_cache[host]

        host_obj: HostInterface | None = None

        if isinstance(host, HostId):
            container = self._find_container_by_host_id(host)
            if container is not None and self._is_container_running(container):
                host_obj = self._create_host_from_container(container)

            if host_obj is None:
                host_record = self._host_store.read_host_record(host)
                if host_record is not None:
                    host_obj = self._create_host_from_host_record(host_record)
        else:
            # Try container label lookup first (fast path)
            container = self._find_container_by_name(host)
            if container is not None and self._is_container_running(container):
                host_obj = self._create_host_from_container(container)

            # Fall back to host records (handles renamed hosts where label has old name)
            if host_obj is None:
                for host_record in self._host_store.list_all_host_records():
                    if host_record.certified_host_data.host_name == str(host):
                        record_host_id = HostId(host_record.certified_host_data.host_id)
                        # Check if the container is running (rename only changes the record, not labels)
                        record_container = self._find_container_by_host_id(record_host_id)
                        if record_container is not None and self._is_container_running(record_container):
                            host_obj = self._create_host_from_container(record_container)
                        else:
                            host_obj = self._create_host_from_host_record(host_record)
                        break

        if host_obj is not None:
            self._host_by_id_cache[host_obj.id] = host_obj
            return host_obj

        raise HostNotFoundError(host)

    def list_hosts(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> list[HostInterface]:
        """List all Docker container hosts."""
        hosts: list[HostInterface] = []
        processed_host_ids: set[HostId] = set()

        try:
            containers = self._list_containers()
            all_host_records = self._host_store.list_all_host_records()
        except (MngError, docker.errors.DockerException) as e:
            logger.warning("Cannot list Docker hosts (Docker daemon unavailable?): {}", e)
            return []

        # Map running containers by host_id
        container_by_host_id: dict[HostId, docker.models.containers.Container] = {}
        for container in containers:
            labels = container.labels or {}
            if LABEL_HOST_ID in labels:
                try:
                    host_id = HostId(labels[LABEL_HOST_ID])
                    container_by_host_id[host_id] = container
                except (KeyError, ValueError) as e:
                    logger.warning("Skipped container with invalid labels: {}", e)

        # Process host records
        for host_record in all_host_records:
            host_id = HostId(host_record.certified_host_data.host_id)
            processed_host_ids.add(host_id)

            host_obj: HostInterface | None = None

            if host_id in container_by_host_id:
                container = container_by_host_id[host_id]
                if self._is_container_running(container):
                    try:
                        host_obj = self._create_host_from_container(container)
                        if host_obj is not None:
                            hosts.append(host_obj)
                            continue
                    except (KeyError, ValueError, MngError) as e:
                        logger.warning("Failed to create host from container {}: {}", host_id, e)

            # Not running or failed to create from container
            has_snapshots = len(host_record.certified_host_data.snapshots) > 0
            is_failed = host_record.certified_host_data.failure_reason is not None
            has_container = host_id in container_by_host_id

            should_include = is_failed or has_snapshots or has_container or include_destroyed
            if should_include:
                try:
                    host_obj = self._create_host_from_host_record(host_record)
                    hosts.append(host_obj)
                except (OSError, ValueError, KeyError) as e:
                    logger.warning("Failed to create host from record {}: {}", host_id, e)

        # Include running containers without host records
        for host_id, container in container_by_host_id.items():
            if host_id in processed_host_ids:
                continue
            if self._is_container_running(container):
                try:
                    host_obj = self._create_host_from_container(container)
                    if host_obj is not None:
                        hosts.append(host_obj)
                except (KeyError, ValueError, MngError) as e:
                    logger.warning("Failed to create host from container {}: {}", host_id, e)

        for h in hosts:
            self._host_by_id_cache[h.id] = h

        return hosts

    def get_host_resources(self, host: HostInterface) -> HostResources:
        """Get resource information for a Docker container.

        Resource limits are applied via docker run flags (start_args) and are
        managed by Docker directly. We return defaults here since we don't
        parse the raw CLI args.
        """
        return HostResources(
            cpu=CpuResources(count=1, frequency_ghz=None),
            memory_gb=1.0,
            disk_gb=None,
            gpu=None,
        )

    # =========================================================================
    # Snapshot Methods
    # =========================================================================

    def create_snapshot(
        self,
        host: HostInterface | HostId,
        name: SnapshotName | None = None,
    ) -> SnapshotId:
        """Create a snapshot of a Docker container via docker commit."""
        host_id = host.id if isinstance(host, HostInterface) else host

        container = self._find_container_by_host_id(host_id)
        if container is None:
            raise HostNotFoundError(host_id)

        if not self._is_container_running(container):
            raise MngError(f"Cannot snapshot stopped container {host_id}")

        if name is None:
            name = SnapshotName(f"snapshot-{uuid4().hex}")

        # Warn about volume mounts (they are not captured in snapshots)
        host_record = self._host_store.read_host_record(host_id)
        if host_record is not None and host_record.config is not None:
            volume_args = [a for a in host_record.config.start_args if a.startswith("-v") or a.startswith("--volume")]
            if volume_args:
                logger.warning(
                    "Container has volume mounts that will NOT be captured in the snapshot: {}",
                    volume_args,
                )

        with log_span("Committing Docker container", host_id=str(host_id)):
            committed_image = container.commit(
                repository="mng-snapshot",
                tag=f"{host_id}-{name}",
            )

        snapshot_id = SnapshotId(committed_image.id)
        created_at = datetime.now(timezone.utc)

        new_snapshot = SnapshotRecord(
            id=str(snapshot_id),
            name=str(name),
            created_at=created_at.isoformat(),
        )

        # Update host record with new snapshot
        host_record = self._host_store.read_host_record(host_id, use_cache=False)
        if host_record is None:
            raise HostNotFoundError(host_id)

        updated_certified_data = host_record.certified_host_data.model_copy_update(
            to_update(
                host_record.certified_host_data.field_ref().snapshots,
                list(host_record.certified_host_data.snapshots) + [new_snapshot],
            ),
        )
        self.get_host(host_id).set_certified_data(updated_certified_data)

        logger.info("Created snapshot: id={}, name={}", snapshot_id, name)
        return snapshot_id

    def list_snapshots(
        self,
        host: HostInterface | HostId,
    ) -> list[SnapshotInfo]:
        """List all snapshots for a Docker container."""
        host_id = host.id if isinstance(host, HostInterface) else host

        host_record = self._host_store.read_host_record(host_id)
        if host_record is None:
            return []

        snapshots: list[SnapshotInfo] = []
        sorted_snapshots = sorted(host_record.certified_host_data.snapshots, key=lambda s: s.created_at, reverse=True)
        for idx, snap_record in enumerate(sorted_snapshots):
            created_at_str = snap_record.created_at
            created_at = datetime.fromisoformat(created_at_str) if created_at_str else datetime.now(timezone.utc)
            snapshots.append(
                SnapshotInfo(
                    id=SnapshotId(snap_record.id),
                    name=SnapshotName(snap_record.name),
                    created_at=created_at,
                    size_bytes=None,
                    recency_idx=idx,
                )
            )

        return snapshots

    def delete_snapshot(
        self,
        host: HostInterface | HostId,
        snapshot_id: SnapshotId,
    ) -> None:
        """Delete a snapshot from a Docker container."""
        host_id = host.id if isinstance(host, HostInterface) else host

        with log_span("Deleting snapshot", snapshot_id=str(snapshot_id), host_id=str(host_id)):
            host_record = self._host_store.read_host_record(host_id, use_cache=False)
            if host_record is None:
                raise HostNotFoundError(host_id)

            snapshot_id_str = str(snapshot_id)
            updated_snapshots = [s for s in host_record.certified_host_data.snapshots if s.id != snapshot_id_str]

            if len(updated_snapshots) == len(host_record.certified_host_data.snapshots):
                raise SnapshotNotFoundError(snapshot_id)

            # Remove Docker image
            try:
                self._docker_client.images.remove(snapshot_id_str)
            except docker.errors.DockerException as e:
                logger.warning("Error removing snapshot image {}: {}", snapshot_id_str, e)

            # Update host record
            updated_certified_data = host_record.certified_host_data.model_copy_update(
                to_update(host_record.certified_host_data.field_ref().snapshots, updated_snapshots),
            )
            self.get_host(host_id).set_certified_data(updated_certified_data)

        logger.info("Deleted snapshot", snapshot_id=str(snapshot_id))

    # =========================================================================
    # Volume Methods
    # =========================================================================

    @staticmethod
    def _volume_id_for_host(host_id: HostId) -> VolumeId:
        """Derive a VolumeId from a HostId.

        Both IDs share the same 32-char hex suffix (``host-<hex>`` ->
        ``vol-<hex>``), so the mapping is a simple prefix swap.
        """
        return VolumeId(f"vol-{host_id.get_uuid().hex}")

    def list_volumes(self) -> list[VolumeInfo]:
        """List logical volumes stored on the state volume."""
        try:
            entries = self._state_volume.listdir("volumes")
        except (FileNotFoundError, OSError):
            return []

        volumes: list[VolumeInfo] = []
        for entry in entries:
            if entry.file_type == VolumeFileType.DIRECTORY:
                vol_name = entry.path.rsplit("/", 1)[-1]
                volume_id = VolumeId(vol_name)
                host_id = HostId(f"host-{volume_id.get_uuid().hex}")
                volumes.append(
                    VolumeInfo(
                        volume_id=volume_id,
                        name=vol_name,
                        size_bytes=0,
                        host_id=host_id,
                    )
                )
        return volumes

    def delete_volume(self, volume_id: VolumeId) -> None:
        """Delete a logical volume from the state volume."""
        self._state_volume.remove_directory(f"volumes/{volume_id}")

    def get_volume_for_host(self, host: HostInterface | HostId) -> HostVolume | None:
        """Get the host volume for a given host.

        Returns a HostVolume backed by a sub-folder of the state volume
        at volumes/vol-<host_hex>/. Returns None when host volumes are disabled.
        """
        if not self.config.is_host_volume_created:
            return None
        host_id = host.id if isinstance(host, HostInterface) else host
        volume_id = self._volume_id_for_host(host_id)
        scoped_volume = self._state_volume.scoped(f"volumes/{volume_id}")
        return HostVolume(volume=scoped_volume)

    def _ensure_host_volume_dir(self, host_id: HostId) -> None:
        """Ensure the volume directory for a host exists on the state volume."""
        volume_id = self._volume_id_for_host(host_id)
        self._state_volume.write_files({f"volumes/{volume_id}/.volume": b""})

    # =========================================================================
    # Tag Methods (immutable)
    # =========================================================================

    def get_host_tags(
        self,
        host: HostInterface | HostId,
    ) -> dict[str, str]:
        """Get user-defined tags for a host from container labels."""
        host_id = host.id if isinstance(host, HostInterface) else host

        container = self._find_container_by_host_id(host_id)
        if container is not None:
            labels = container.labels or {}
            tags_json = labels.get(LABEL_TAGS, "{}")
            try:
                return json.loads(tags_json)
            except (json.JSONDecodeError, TypeError):
                logger.warning("Invalid JSON in container tags label: {}", tags_json)
                return {}

        host_record = self._host_store.read_host_record(host_id)
        if host_record is not None:
            return dict(host_record.certified_host_data.user_tags)

        raise HostNotFoundError(host_id)

    def set_host_tags(
        self,
        host: HostInterface | HostId,
        tags: Mapping[str, str],
    ) -> None:
        raise MngError("Docker provider does not support mutable tags. Tags are set at host creation time.")

    def add_tags_to_host(
        self,
        host: HostInterface | HostId,
        tags: Mapping[str, str],
    ) -> None:
        raise MngError("Docker provider does not support mutable tags. Tags are set at host creation time.")

    def remove_tags_from_host(
        self,
        host: HostInterface | HostId,
        keys: Sequence[str],
    ) -> None:
        raise MngError("Docker provider does not support mutable tags. Tags are set at host creation time.")

    def rename_host(
        self,
        host: HostInterface | HostId,
        name: HostName,
    ) -> HostInterface:
        """Rename a host (logical name only, container name unchanged)."""
        host_id = host.id if isinstance(host, HostInterface) else host

        host_obj = self.get_host(host_id)
        certified_data = host_obj.get_certified_data()
        updated_certified_data = certified_data.model_copy_update(
            to_update(certified_data.field_ref().host_name, str(name)),
        )
        host_obj.set_certified_data(updated_certified_data)

        return host_obj

    # =========================================================================
    # Connector Method
    # =========================================================================

    def get_connector(
        self,
        host: HostInterface | HostId,
    ) -> PyinfraHost:
        """Get a pyinfra connector for the host."""
        host_id = host.id if isinstance(host, HostInterface) else host

        host_record = self._host_store.read_host_record(host_id)
        if host_record is None:
            raise HostNotFoundError(host_id)

        if host_record.ssh_host is None or host_record.ssh_port is None or host_record.ssh_host_public_key is None:
            raise MngError(f"Cannot get connector for host {host_id}: host has no SSH info (likely a failed host)")

        add_host_to_known_hosts(
            self._known_hosts_path,
            host_record.ssh_host,
            host_record.ssh_port,
            host_record.ssh_host_public_key,
        )

        private_key_path, _ = self._get_ssh_keypair()
        return self._create_pyinfra_host(
            host_record.ssh_host,
            host_record.ssh_port,
            private_key_path,
        )

    # =========================================================================
    # Agent Data Persistence
    # =========================================================================

    def list_persisted_agent_data_for_host(self, host_id: HostId) -> list[dict[str, Any]]:
        """List persisted agent data for a stopped host."""
        return self._host_store.list_persisted_agent_data_for_host(host_id)

    def persist_agent_data(self, host_id: HostId, agent_data: Mapping[str, object]) -> None:
        """Persist agent data to the local file store."""
        self._host_store.persist_agent_data(host_id, dict(agent_data))

    def remove_persisted_agent_data(self, host_id: HostId, agent_id: AgentId) -> None:
        """Remove persisted agent data."""
        self._host_store.remove_persisted_agent_data(host_id, agent_id)

    # =========================================================================
    # Lifecycle Methods
    # =========================================================================

    def close(self) -> None:
        """Clean up the Docker client connection."""
        if "_docker_client" in self.__dict__:
            try:
                self._docker_client.close()
            except (OSError, docker.errors.DockerException) as e:
                logger.warning("Ignored error closing Docker client: {}", e)
