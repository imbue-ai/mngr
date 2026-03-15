"""Abstract interface for all interactions with Modal.

This interface captures every call that mng_modal makes to the Modal SDK or CLI,
organized into logical groups. Three implementations are planned:

1. DirectModalInterface: calls the Modal Python SDK and CLI directly (current behavior)
2. TestingModalInterface: fakes Modal behavior locally for integration tests
3. RemoteModalInterface: proxies calls to a web server (for white-labeling / managed service)

Handle types (AppHandle, SandboxHandle, ImageHandle, VolumeHandle) are opaque -- each
implementation stores whatever internal state it needs. Callers should not inspect them.

Call sites in mng_modal that this interface abstracts:
- backend.py: app creation/lookup, app run context, environment creation, volume creation
- instance.py: sandbox create/list/terminate/snapshot, image building, volume lifecycle,
  sandbox exec/tunnels/tags, function deployment, secret creation
- volume.py: volume data operations (listdir, read_file, remove_file, write_files)
- routes/deployment.py: modal deploy CLI, function lookup
- routes/snapshot_and_shutdown.py: sandbox lookup by ID, snapshot, terminate
- log_utils.py: output capture
"""

from abc import ABC
from abc import abstractmethod
from pathlib import Path
from typing import Any

from imbue.modal_proxy.data_types import ExecStreamType
from imbue.modal_proxy.data_types import FileEntry
from imbue.modal_proxy.data_types import ImageBuildContext
from imbue.modal_proxy.data_types import TunnelInfo
from imbue.modal_proxy.data_types import VolumeRef

# ---------------------------------------------------------------------------
# Opaque handle types
#
# Each implementation stores whatever it needs inside these. Callers treat
# them as opaque tokens passed back into subsequent ModalInterface methods.
# ---------------------------------------------------------------------------


class AppHandle:
    """Opaque handle to a Modal app (or equivalent in testing/remote mode)."""

    def __init__(self, *, app_id: str, name: str, inner: Any = None) -> None:
        self.app_id = app_id
        self.name = name
        self._inner = inner

    def __repr__(self) -> str:
        return f"AppHandle(app_id={self.app_id!r}, name={self.name!r})"


class SandboxHandle:
    """Opaque handle to a Modal sandbox."""

    def __init__(self, *, sandbox_id: str, inner: Any = None) -> None:
        self.sandbox_id = sandbox_id
        self._inner = inner

    def __repr__(self) -> str:
        return f"SandboxHandle(sandbox_id={self.sandbox_id!r})"


class ImageHandle:
    """Opaque handle to a Modal image."""

    def __init__(self, *, image_id: str, inner: Any = None) -> None:
        self.image_id = image_id
        self._inner = inner

    def __repr__(self) -> str:
        return f"ImageHandle(image_id={self.image_id!r})"


class VolumeHandle:
    """Opaque handle to a Modal volume."""

    def __init__(self, *, name: str, inner: Any = None) -> None:
        self.name = name
        self._inner = inner

    def __repr__(self) -> str:
        return f"VolumeHandle(name={self.name!r})"


class SecretHandle:
    """Opaque handle to a Modal secret."""

    def __init__(self, *, inner: Any = None) -> None:
        self._inner = inner


class ExecHandle:
    """Handle to a running command inside a sandbox.

    Provides access to stdout and the ability to wait for completion.
    The implementation determines whether stdout is available (it is only
    available when ExecStreamType.PIPE is used or defaulted).
    """

    @abstractmethod
    def wait(self) -> None:
        """Block until the command completes."""
        ...

    @abstractmethod
    def read_stdout(self) -> str:
        """Read all stdout output from the command. Blocks until complete."""
        ...


# ---------------------------------------------------------------------------
# Core interface
# ---------------------------------------------------------------------------


class ModalInterface(ABC):
    """Abstraction over all interactions with Modal.

    Methods are grouped by the type of Modal resource they operate on.
    All methods are synchronous (matching the current mng_modal usage).
    """

    # =====================================================================
    # Environment
    # =====================================================================

    @abstractmethod
    def environment_create(self, name: str) -> None:
        """Create a Modal environment.

        Environments scope all resources (apps, volumes, sandboxes) for user isolation.
        No-op if the environment already exists.

        Call sites: backend.py _create_environment (via `modal environment create` CLI)
        """
        ...

    # =====================================================================
    # App lifecycle
    # =====================================================================

    @abstractmethod
    def app_lookup(
        self,
        app_name: str,
        environment_name: str,
        create_if_missing: bool = True,
    ) -> AppHandle:
        """Look up or create a persistent Modal app.

        Call sites: backend.py _lookup_persistent_app_with_env_retry
        """
        ...

    @abstractmethod
    def app_create_ephemeral(
        self,
        app_name: str,
        environment_name: str,
    ) -> AppHandle:
        """Create an ephemeral Modal app and enter its run context.

        The run context stays active until app_close() is called. While active,
        resources (sandboxes, etc.) can be created under this app.

        Call sites: backend.py _get_or_create_app (modal.App + app.run)
        """
        ...

    @abstractmethod
    def app_close(self, app: AppHandle) -> None:
        """Exit an ephemeral app's run context and release resources.

        Call sites: backend.py close_app / _exit_modal_app_context
        """
        ...

    # =====================================================================
    # Image building
    # =====================================================================

    @abstractmethod
    def image_from_registry(self, name: str) -> ImageHandle:
        """Create an image from a Docker registry reference (e.g. 'python:3.11-slim').

        Call sites: instance.py _build_modal_image (modal.Image.from_registry)
        """
        ...

    @abstractmethod
    def image_debian_slim(self) -> ImageHandle:
        """Create a base Debian slim image.

        Call sites: instance.py _build_modal_image (modal.Image.debian_slim),
                    routes/snapshot_and_shutdown.py (modal.Image.debian_slim)
        """
        ...

    @abstractmethod
    def image_from_id(self, image_id: str) -> ImageHandle:
        """Load an image by its ID (used for restoring from snapshots).

        Call sites: instance.py create_host (from snapshot), start_host
        """
        ...

    @abstractmethod
    def image_apt_install(self, image: ImageHandle, packages: tuple[str, ...]) -> ImageHandle:
        """Install apt packages on an image.

        Call sites: instance.py _build_modal_image (default image path)
        """
        ...

    @abstractmethod
    def image_dockerfile_commands(
        self,
        image: ImageHandle,
        build_context: ImageBuildContext,
    ) -> ImageHandle:
        """Apply Dockerfile commands to an image.

        Supports build context directory for COPY/ADD instructions,
        and secrets for --mount=type=secret usage in RUN commands.

        Call sites: instance.py _build_image_from_dockerfile_contents
        """
        ...

    # =====================================================================
    # Sandbox lifecycle
    # =====================================================================

    @abstractmethod
    def sandbox_create(
        self,
        image: ImageHandle,
        app: AppHandle,
        *,
        timeout: int,
        cpu: float,
        memory_mb: int,
        unencrypted_ports: tuple[int, ...] = (),
        gpu: str | None = None,
        region: str | None = None,
        cidr_allowlist: list[str] | None = None,
        volumes: dict[str, VolumeHandle] | None = None,
    ) -> SandboxHandle:
        """Create a new sandbox from an image.

        The sandbox runs until timeout or explicit termination.
        Unencrypted ports are exposed for SSH access.
        Volumes are mounted at the specified paths.

        Call sites: instance.py create_host, start_host
        """
        ...

    @abstractmethod
    def sandbox_list(self, app: AppHandle) -> list[SandboxHandle]:
        """List all sandboxes associated with an app.

        Call sites: instance.py _list_sandboxes, _lookup_sandbox_by_host_id_once,
                    _lookup_sandbox_by_name_once, _list_running_host_ids
        """
        ...

    @abstractmethod
    def sandbox_from_id(self, sandbox_id: str) -> SandboxHandle:
        """Look up a sandbox by its ID.

        Call sites: routes/snapshot_and_shutdown.py
        """
        ...

    @abstractmethod
    def sandbox_get_tags(self, sandbox: SandboxHandle) -> dict[str, str]:
        """Get all tags on a sandbox.

        Call sites: instance.py _create_host_from_sandbox, discover_hosts,
                    _list_running_host_ids, get_host_tags, set_host_tags, etc.
        """
        ...

    @abstractmethod
    def sandbox_set_tags(self, sandbox: SandboxHandle, tags: dict[str, str]) -> None:
        """Replace all tags on a sandbox.

        Call sites: instance.py _setup_sandbox_ssh_and_create_host, set_host_tags,
                    add_tags_to_host, remove_tags_from_host, rename_host
        """
        ...

    @abstractmethod
    def sandbox_exec(
        self,
        sandbox: SandboxHandle,
        command: tuple[str, ...],
        *,
        stdout: ExecStreamType = ExecStreamType.PIPE,
        stderr: ExecStreamType = ExecStreamType.PIPE,
    ) -> ExecHandle:
        """Execute a command inside a running sandbox.

        Returns an ExecHandle that can be waited on and whose stdout can be read.

        Call sites: instance.py _check_and_install_packages, _start_sshd_in_sandbox,
                    _collect_all_listing_data_via_ssh (via SSH, not sandbox.exec),
                    activity watcher start, volume sync start
        """
        ...

    @abstractmethod
    def sandbox_get_tunnels(self, sandbox: SandboxHandle) -> dict[int, TunnelInfo]:
        """Get tunnel connection info for a sandbox's exposed ports.

        Returns a mapping from container port to TunnelInfo (host, port).

        Call sites: instance.py _get_ssh_info_from_sandbox
        """
        ...

    @abstractmethod
    def sandbox_terminate(self, sandbox: SandboxHandle) -> None:
        """Terminate a running sandbox.

        Call sites: instance.py stop_host, routes/snapshot_and_shutdown.py
        """
        ...

    @abstractmethod
    def sandbox_snapshot(self, sandbox: SandboxHandle, timeout: int = 120) -> ImageHandle:
        """Snapshot the sandbox's filesystem, returning a handle to the resulting image.

        The image can later be used with sandbox_create to restore from this snapshot.

        Call sites: instance.py _record_snapshot, routes/snapshot_and_shutdown.py
        """
        ...

    # =====================================================================
    # Volume lifecycle
    # =====================================================================

    @abstractmethod
    def volume_create_or_get(
        self,
        name: str,
        environment_name: str,
        version: int | None = None,
    ) -> VolumeHandle:
        """Look up a volume by name, creating it if it does not exist.

        Call sites: backend.py get_volume_for_app (state volume),
                    instance.py _build_host_volume, _build_modal_volumes,
                    get_volume_for_host
        """
        ...

    @abstractmethod
    def volume_lookup(
        self,
        name: str,
        environment_name: str,
    ) -> VolumeHandle:
        """Look up a volume by name (without creating).

        Raises if the volume does not exist.

        Call sites: instance.py get_volume_for_host (probe path)
        """
        ...

    @abstractmethod
    def volume_list(self, environment_name: str) -> list[VolumeRef]:
        """List all volumes in the given environment.

        Call sites: instance.py list_volumes, delete_volume
        """
        ...

    @abstractmethod
    def volume_delete(self, name: str, environment_name: str) -> None:
        """Delete a volume by name.

        No-op if the volume does not exist.

        Call sites: instance.py _delete_host_volume, delete_volume
        """
        ...

    # =====================================================================
    # Volume data operations
    # =====================================================================

    @abstractmethod
    def volume_listdir(self, volume: VolumeHandle, path: str) -> list[FileEntry]:
        """List entries in a directory on the volume.

        Call sites: volume.py ModalVolume.listdir, instance.py get_volume_for_host (probe)
        """
        ...

    @abstractmethod
    def volume_read_file(self, volume: VolumeHandle, path: str) -> bytes:
        """Read a file from the volume and return its contents.

        Call sites: volume.py ModalVolume.read_file
        """
        ...

    @abstractmethod
    def volume_remove_file(self, volume: VolumeHandle, path: str, *, recursive: bool = False) -> None:
        """Remove a file or directory from the volume.

        Call sites: volume.py ModalVolume.remove_file / remove_directory
        """
        ...

    @abstractmethod
    def volume_write_files(self, volume: VolumeHandle, file_contents_by_path: dict[str, bytes]) -> None:
        """Write one or more files to the volume.

        Call sites: volume.py ModalVolume.write_files (batch upload)
        """
        ...

    # =====================================================================
    # Secrets
    # =====================================================================

    @abstractmethod
    def secret_from_env(self, env_var_names: tuple[str, ...]) -> SecretHandle:
        """Create a secret from environment variable values.

        Reads the named variables from the current process environment and
        bundles them into a secret that can be used during image builds.

        Call sites: instance.py _build_modal_secrets_from_env
        """
        ...

    # =====================================================================
    # Function deployment
    # =====================================================================

    @abstractmethod
    def deploy_function(
        self,
        script_path: Path,
        app_name: str,
        environment_name: str | None,
    ) -> str:
        """Deploy a Modal function from a script file and return its web URL.

        Call sites: routes/deployment.py deploy_function (via `modal deploy` CLI)
        """
        ...

    @abstractmethod
    def get_function_url(
        self,
        function_name: str,
        app_name: str,
        environment_name: str | None,
    ) -> str | None:
        """Get the web URL of a previously deployed function.

        Call sites: routes/deployment.py deploy_function (Function.from_name + get_web_url)
        """
        ...
