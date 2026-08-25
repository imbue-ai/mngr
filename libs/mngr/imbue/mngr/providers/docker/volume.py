import io
import tarfile
from typing import Final
from typing import Mapping
from uuid import uuid4

import docker
import docker.errors
import docker.models.containers
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.mngr.errors import MngrError
from imbue.mngr.errors import VolumeListingError
from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr.interfaces.data_types import VolumeFile
from imbue.mngr.interfaces.volume import BaseVolume
from imbue.mngr.primitives import HostName

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


def host_container_name(prefix: str, host_name: HostName) -> str:
    """Generate the name for the container backing host ``host_name``.

    Every creation path names a host's container this way, and lookups rely on
    it: the labels alone carry no environment discriminator (two mngr
    environments differing only in ``MNGR_PREFIX`` label their containers
    identically), so the prefixed container name is what scopes a host name to
    a single environment -- the same uniqueness scope Docker itself enforces.
    """
    return f"{prefix}{host_name}"


def state_container_name(prefix: str, user_id: str) -> str:
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
    container_name = state_container_name(prefix, user_id)
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
        """Execute a command in the state container.

        Forces ``workdir="/"`` for consistency with the per-host
        container's exec wrapper (see ``DockerProviderInstance._exec_in_container``).
        The state container doesn't currently race with any seed step, but
        the override is harmless (all volume paths are absolute) and
        keeps the exec pattern uniform across containers.
        """
        exit_code, output = self.container.exec_run(["sh", "-c", command], workdir="/")
        output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)
        return exit_code, output_str

    def listdir(self, path: str) -> list[VolumeFile]:
        resolved = self._resolve(path)
        # BusyBox-compatible: use ls -la and parse output
        exit_code, output = self._exec(f"ls -la '{resolved}'")
        if exit_code != 0:
            # Distinguish a genuinely-missing directory (normal: a fresh env's
            # host_state, a host with no persisted agents) from every other
            # failure -- a failed exec against the state container makes the
            # listing look empty upstream, which discovery reports as "these
            # hosts/agents do not exist", so it must not masquerade as the
            # missing-directory case.
            if "No such file or directory" in output:
                raise FileNotFoundError(f"Directory not found on volume: {path}")
            raise VolumeListingError(
                f"Failed to list '{path}' on volume (ls exited {exit_code}): {output.strip()[:200]}"
            )
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
            file_type = FileType.DIRECTORY if perms.startswith("d") else FileType.FILE
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

    def path_exists(self, path: str) -> bool:
        resolved = self._resolve(path)
        exit_code, _ = self._exec(f"test -e '{resolved}'")
        return exit_code == 0

    def read_file(self, path: str) -> bytes:
        resolved = self._resolve(path)
        exit_code, output = self.container.exec_run(["cat", resolved], workdir="/")
        if exit_code != 0:
            detail = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)
            raise FileNotFoundError(
                f"File not found on volume: {path} (cat exited {exit_code}: {detail.strip()[:200]})"
            )
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
        """Write files to the volume using docker put_archive for binary safety.

        Each file is extracted under a temporary dot-prefixed name and then
        renamed into place: put_archive streams the extraction, so a reader
        catting the final path mid-extraction would otherwise observe an
        empty or partial file (the torn host-record read behind the
        HostNotFoundError flake in test_create_snapshot). rename(2) within
        one directory is atomic, so readers see the old content or the new
        content, never a torn write. The dot prefix plus non-.json suffix
        keeps in-flight temp files out of the host store's listings.
        """
        # An empty batch has nothing to upload or rename (and the rename exec
        # would otherwise degenerate to an empty shell command).
        if not file_contents_by_path:
            return

        # Ensure parent directories exist
        for file_path in file_contents_by_path:
            resolved = self._resolve(file_path)
            parent = resolved.rsplit("/", 1)[0]
            if parent:
                self._exec(f"mkdir -p '{parent}'")

        # Build a tar archive of the files under their temporary names and
        # extract at /
        nonce = uuid4().hex
        temp_path_by_final_path: dict[str, str] = {}
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode="w") as tar:
            for file_path, data in file_contents_by_path.items():
                resolved = self._resolve(file_path)
                parent, _, basename = resolved.rpartition("/")
                temp_path = f"{parent}/.{basename}.tmp-{nonce}"
                temp_path_by_final_path[resolved] = temp_path
                info = tarfile.TarInfo(name=temp_path.lstrip("/"))
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

        tar_buffer.seek(0)
        success = self.container.put_archive("/", tar_buffer)
        if not success:
            raise MngrError("Failed to write files to Docker volume")

        # Atomically move every fully-extracted file into place
        move_command = " && ".join(
            f"mv -f '{temp_path}' '{final_path}'" for final_path, temp_path in temp_path_by_final_path.items()
        )
        exit_code, output = self._exec(move_command)
        if exit_code != 0:
            # Best-effort cleanup so a failed finalize does not strand temp
            # files on the volume (already-renamed temps no longer exist, so
            # rm -f is a no-op for them).
            self._exec(" ; ".join(f"rm -f '{temp_path}'" for temp_path in temp_path_by_final_path.values()))
            raise MngrError(f"Failed to finalize volume write (mv exited {exit_code}): {output.strip()[:200]}")
