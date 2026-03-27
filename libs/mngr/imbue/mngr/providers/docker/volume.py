import io
import tarfile
from typing import Final
from typing import Mapping

import docker
import docker.errors
import docker.models.containers
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import VolumeFile
from imbue.mngr.interfaces.data_types import VolumeFileType
from imbue.mngr.interfaces.volume import BaseVolume

# Docker label constants shared between volume.py and instance.py.
# Defined here (the lower-level module) to avoid circular imports.
LABEL_PREFIX: Final[str] = "com.imbue.mngr."
LABEL_PROVIDER: Final[str] = f"{LABEL_PREFIX}provider"
STATE_CONTAINER_TYPE_LABEL: Final[str] = f"{LABEL_PREFIX}type"
STATE_CONTAINER_TYPE_VALUE: Final[str] = "state-container"

# Shell command that keeps PID 1 alive and responds to SIGTERM.
# Shared between host containers and the state container.
CONTAINER_ENTRYPOINT_CMD: Final[str] = "trap 'exit 0' TERM; tail -f /dev/null & wait"

# Name and configuration for the singleton state container
STATE_CONTAINER_IMAGE: Final[str] = "alpine:latest"
STATE_VOLUME_MOUNT_PATH: Final[str] = "/mngr-state"


def _state_container_name(prefix: str, user_id: str) -> str:
    """Generate the name for the singleton state container."""
    return f"{prefix}docker-state-{user_id}"


def state_volume_name(prefix: str, user_id: str) -> str:
    """Generate the name for the Docker volume backing the state container."""
    return f"{prefix}docker-state-{user_id}"


def ensure_state_container(
    client: docker.DockerClient,
    prefix: str,
    user_id: str,
    provider_name: str = "",
) -> docker.models.containers.Container:
    """Ensure the singleton state container exists and is running.

    Creates a Docker named volume and a small Alpine container that mounts it.
    The container is used as a file server: we exec into it to read/write
    state files (host records, agent data, etc.).

    The provider_name label is added so that the container is discoverable by
    the same label filter used for host containers (LABEL_PROVIDER).

    Returns the container (created or existing).
    """
    container_name = _state_container_name(prefix, user_id)
    volume_name = state_volume_name(prefix, user_id)

    # Check if container already exists
    try:
        container = client.containers.get(container_name)
        if container.status != "running":
            container.start()
        return container
    except docker.errors.NotFound:
        pass

    # Build labels -- always include the type label, and include the provider
    # label so the container is discoverable by _list_containers().
    labels: dict[str, str] = {STATE_CONTAINER_TYPE_LABEL: STATE_CONTAINER_TYPE_VALUE}
    if provider_name:
        labels[LABEL_PROVIDER] = provider_name

    # Create the container with a named volume
    logger.debug("Creating Docker state container: {}", container_name)
    container = client.containers.run(
        image=STATE_CONTAINER_IMAGE,
        name=container_name,
        command=["sh", "-c", CONTAINER_ENTRYPOINT_CMD],
        detach=True,
        volumes={volume_name: {"bind": STATE_VOLUME_MOUNT_PATH, "mode": "rw"}},
        labels=labels,
        restart_policy={"Name": "unless-stopped"},
    )
    return container


class DockerVolume(BaseVolume):
    """Volume implementation backed by exec into a Docker state container.

    All file operations are performed against the state container, which has
    the Docker named volume mounted at STATE_VOLUME_MOUNT_PATH.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    container: docker.models.containers.Container = Field(frozen=True, description="The state container to exec into")
    root_path: str = Field(
        default=STATE_VOLUME_MOUNT_PATH,
        frozen=True,
        description="Root path inside the container",
    )

    def _resolve(self, path: str) -> str:
        """Resolve a relative path to an absolute path inside the container."""
        path = path.lstrip("/")
        root = self.root_path.rstrip("/")
        return f"{root}/{path}" if path else root

    def _exec(self, command: str) -> tuple[int, str]:
        """Execute a command in the state container."""
        exit_code, output = self.container.exec_run(["sh", "-c", command])
        output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)
        return exit_code, output_str

    def listdir(self, path: str) -> list[VolumeFile]:
        resolved = self._resolve(path)
        # BusyBox-compatible: use ls -la and parse output
        exit_code, output = self._exec(f"ls -la '{resolved}'")
        if exit_code != 0:
            raise FileNotFoundError(f"Directory not found on volume: {path}")
        if not output.strip():
            return []

        entries: list[VolumeFile] = []
        for line in output.strip().split("\n"):
            # Skip total line and . / .. entries
            if line.startswith("total ") or not line.strip():
                continue
            parts = line.split()
            if len(parts) < 9:
                continue
            name = " ".join(parts[8:])
            if name in (".", ".."):
                continue

            perms = parts[0]
            size = int(parts[4]) if parts[4].isdigit() else 0
            file_type = VolumeFileType.DIRECTORY if perms.startswith("d") else VolumeFileType.FILE
            path_str = path.rstrip("/") + "/" + name if path.strip("/") else name

            entries.append(
                VolumeFile(
                    path=path_str,
                    file_type=file_type,
                    mtime=0,
                    size=size,
                )
            )
        return sorted(entries, key=lambda e: e.path)

    def read_file(self, path: str) -> bytes:
        resolved = self._resolve(path)
        exit_code, output = self.container.exec_run(["cat", resolved])
        if exit_code != 0:
            raise FileNotFoundError(f"File not found on volume: {path}")
        return output if isinstance(output, bytes) else output.encode("utf-8")

    def remove_file(self, path: str, *, recursive: bool = False) -> None:
        resolved = self._resolve(path)
        rm_flag = "-rf" if recursive else "-f"
        exit_code, output = self._exec(f"rm {rm_flag} '{resolved}'")
        if exit_code != 0:
            raise MngrError(f"Failed to remove '{path}' from volume: {output}")

    def remove_directory(self, path: str) -> None:
        """Recursively remove a directory and all its contents."""
        resolved = self._resolve(path)
        exit_code, output = self._exec(f"rm -rf '{resolved}'")
        if exit_code != 0:
            raise MngrError(f"Failed to remove directory '{path}' from volume: {output}")

    def write_files(self, file_contents_by_path: Mapping[str, bytes]) -> None:
        """Write files to the volume using docker put_archive for binary safety."""
        # Ensure parent directories exist
        for file_path in file_contents_by_path:
            resolved = self._resolve(file_path)
            parent = resolved.rsplit("/", 1)[0]
            if parent:
                self._exec(f"mkdir -p '{parent}'")

        # Build a tar archive containing all files and extract at /
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode="w") as tar:
            for file_path, data in file_contents_by_path.items():
                resolved = self._resolve(file_path)
                info = tarfile.TarInfo(name=resolved.lstrip("/"))
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

        tar_buffer.seek(0)
        success = self.container.put_archive("/", tar_buffer)
        if not success:
            raise MngrError("Failed to write files to Docker volume")
