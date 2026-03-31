# Interfaces that mirror the Modal SDK object model.
#
# Each Modal object type (App, Sandbox, Image, Volume, etc.) gets its own
# abstract interface exposing only the methods and arguments that mngr_modal
# actually uses. The top-level ModalInterface provides all class-method and
# module-level operations (object creation, lookup, CLI commands).
#
# Three implementations are planned:
# 1. DirectModalInterface -- wraps the real Modal Python SDK
# 2. TestingModalInterface -- fakes Modal locally for integration tests
# 3. RemoteModalInterface -- proxies to a web server for managed service

from abc import ABC
from abc import abstractmethod
from collections.abc import Generator
from pathlib import Path
from typing import Mapping
from typing import Sequence

from imbue.imbue_common.mutable_model import MutableModel
from imbue.modal_proxy.data_types import FileEntry
from imbue.modal_proxy.data_types import StreamType
from imbue.modal_proxy.data_types import TunnelInfo

# ---------------------------------------------------------------------------
# Object interfaces -- mirror modal's instance-level APIs
# ---------------------------------------------------------------------------


class ExecOutput(MutableModel, ABC):
    """Readable stream from a sandbox exec command (mirrors process.stdout)."""

    @abstractmethod
    def read(self) -> str:
        """Read all output, blocking until the command completes."""
        ...


class ExecProcess(MutableModel, ABC):
    """Handle to a running command inside a sandbox (mirrors modal exec result)."""

    @abstractmethod
    def get_stdout(self) -> ExecOutput:
        """Get the stdout stream for this process."""
        ...

    @abstractmethod
    def wait(self) -> int:
        """Block until the command completes and return the exit code."""
        ...


class SecretInterface(MutableModel, ABC):
    """Opaque secret passed to image builds (mirrors modal.Secret)."""


class FunctionInterface(MutableModel, ABC):
    """A deployed Modal function (mirrors modal.Function)."""

    @abstractmethod
    def get_web_url(self) -> str | None:
        """Get the public web endpoint URL for this function."""
        ...


class ImageInterface(MutableModel, ABC):
    """A container image that can be used to create sandboxes (mirrors modal.Image)."""

    @abstractmethod
    def get_object_id(self) -> str:
        """Get the unique identifier for this image."""
        ...

    # Image building methods -- each returns a new ImageInterface (chainable)

    @abstractmethod
    def apt_install(self, *packages: str) -> "ImageInterface":
        """Install apt packages on this image."""
        ...

    @abstractmethod
    def dockerfile_commands(
        self,
        commands: Sequence[str],
        *,
        context_dir: Path | None = None,
        secrets: Sequence[SecretInterface] = (),
    ) -> "ImageInterface":
        """Apply Dockerfile commands to this image."""
        ...


class VolumeInterface(MutableModel, ABC):
    """A persistent volume for storing files (mirrors modal.Volume)."""

    @abstractmethod
    def get_name(self) -> str | None:
        """Get the volume name (if it has one)."""
        ...

    @abstractmethod
    def listdir(self, path: str) -> list[FileEntry]:
        """List entries in a directory on the volume."""
        ...

    @abstractmethod
    def read_file(self, path: str) -> bytes:
        """Read a file from the volume and return its contents."""
        ...

    @abstractmethod
    def remove_file(self, path: str, *, recursive: bool = False) -> None:
        """Remove a file or directory from the volume."""
        ...

    @abstractmethod
    def write_files(self, file_contents_by_path: Mapping[str, bytes]) -> None:
        """Write one or more files to the volume (wraps batch_upload)."""
        ...

    @abstractmethod
    def reload(self) -> None:
        """Refresh volume data to see external changes."""
        ...

    @abstractmethod
    def commit(self) -> None:
        """Commit pending writes to the volume."""
        ...


class SandboxInterface(MutableModel, ABC):
    """A running sandbox container (mirrors modal.Sandbox)."""

    @abstractmethod
    def get_object_id(self) -> str:
        """Get the unique identifier for this sandbox."""
        ...

    @abstractmethod
    def exec(
        self,
        *args: str,
        stdout: StreamType = StreamType.PIPE,
        stderr: StreamType = StreamType.PIPE,
    ) -> ExecProcess:
        """Execute a command inside this sandbox."""
        ...

    @abstractmethod
    def tunnels(self, *, timeout: int = 50) -> dict[int, TunnelInfo]:
        """Get tunnel connection info for exposed ports.

        Blocks until tunnel metadata is available or the timeout is reached.
        If the sandbox is not ready within ``timeout`` seconds, a
        ``SandboxTimeoutError`` is raised by the Modal backend.
        """
        ...

    @abstractmethod
    def get_tags(self) -> dict[str, str]:
        """Get all tags on this sandbox."""
        ...

    @abstractmethod
    def set_tags(self, tags: Mapping[str, str]) -> None:
        """Replace all tags on this sandbox."""
        ...

    @abstractmethod
    def snapshot_filesystem(self, timeout: int = 120) -> ImageInterface:
        """Snapshot this sandbox's filesystem, returning the resulting image."""
        ...

    @abstractmethod
    def terminate(self) -> None:
        """Terminate this sandbox."""
        ...


class AppInterface(MutableModel, ABC):
    """A Modal app that scopes sandboxes and resources (mirrors modal.App)."""

    @abstractmethod
    def get_app_id(self) -> str:
        """Get the unique identifier for this app."""
        ...

    @abstractmethod
    def get_name(self) -> str:
        """Get the human-readable name of this app."""
        ...

    @abstractmethod
    def run(self, *, environment_name: str) -> Generator["AppInterface", None, None]:
        """Enter this app's run context (for ephemeral apps)."""
        ...


# ---------------------------------------------------------------------------
# Top-level interface -- mirrors modal module-level and class-method APIs
# ---------------------------------------------------------------------------


class ModalInterface(MutableModel, ABC):
    """Abstraction over the Modal SDK module-level and class-method APIs."""

    # =====================================================================
    # Environment (CLI: `modal environment create`)
    # =====================================================================

    @abstractmethod
    def environment_create(self, name: str) -> None:
        """Create a Modal environment for resource isolation."""
        ...

    # =====================================================================
    # App (modal.App constructor, modal.App.lookup)
    # =====================================================================

    @abstractmethod
    def app_create(self, name: str) -> AppInterface:
        """Create a new Modal app (mirrors modal.App(name))."""
        ...

    @abstractmethod
    def app_lookup(
        self,
        name: str,
        *,
        create_if_missing: bool = True,
        environment_name: str,
    ) -> AppInterface:
        """Look up a persistent Modal app (mirrors modal.App.lookup)."""
        ...

    # =====================================================================
    # Image (modal.Image class methods)
    # =====================================================================

    @abstractmethod
    def image_debian_slim(self) -> ImageInterface:
        """Create a Debian slim base image (mirrors modal.Image.debian_slim)."""
        ...

    @abstractmethod
    def image_from_registry(self, name: str) -> ImageInterface:
        """Create an image from a registry reference (mirrors modal.Image.from_registry)."""
        ...

    @abstractmethod
    def image_from_id(self, image_id: str) -> ImageInterface:
        """Load an image by ID, e.g. from a snapshot (mirrors modal.Image.from_id)."""
        ...

    # =====================================================================
    # Sandbox (modal.Sandbox class methods)
    # =====================================================================

    @abstractmethod
    def sandbox_create(
        self,
        *,
        image: ImageInterface,
        app: AppInterface,
        timeout: int,
        cpu: float,
        memory: int,
        unencrypted_ports: Sequence[int] = (),
        gpu: str | None = None,
        region: str | None = None,
        cidr_allowlist: Sequence[str] | None = None,
        volumes: Mapping[str, VolumeInterface] | None = None,
    ) -> SandboxInterface:
        """Create a sandbox (mirrors modal.Sandbox.create)."""
        ...

    @abstractmethod
    def sandbox_list(self, *, app_id: str) -> list[SandboxInterface]:
        """List sandboxes for an app (mirrors modal.Sandbox.list)."""
        ...

    @abstractmethod
    def sandbox_from_id(self, sandbox_id: str) -> SandboxInterface:
        """Look up a sandbox by ID (mirrors modal.Sandbox.from_id)."""
        ...

    # =====================================================================
    # Volume (modal.Volume class methods and modal.Volume.objects)
    # =====================================================================

    @abstractmethod
    def volume_from_name(
        self,
        name: str,
        *,
        create_if_missing: bool = True,
        environment_name: str,
        version: int | None = None,
    ) -> VolumeInterface:
        """Get or create a volume by name (mirrors modal.Volume.from_name)."""
        ...

    @abstractmethod
    def volume_list(self, *, environment_name: str) -> list[VolumeInterface]:
        """List all volumes in an environment (mirrors modal.Volume.objects.list)."""
        ...

    @abstractmethod
    def volume_delete(self, name: str, *, environment_name: str) -> None:
        """Delete a volume by name (mirrors modal.Volume.objects.delete)."""
        ...

    # =====================================================================
    # Secret (modal.Secret class methods)
    # =====================================================================

    @abstractmethod
    def secret_from_dict(self, values: Mapping[str, str | None]) -> SecretInterface:
        """Create a secret from key-value pairs (mirrors modal.Secret.from_dict)."""
        ...

    # =====================================================================
    # Function (modal.Function class methods)
    # =====================================================================

    @abstractmethod
    def function_from_name(
        self,
        name: str,
        *,
        app_name: str,
        environment_name: str | None = None,
    ) -> FunctionInterface:
        """Look up a deployed function by name (mirrors modal.Function.from_name)."""
        ...

    # =====================================================================
    # CLI operations
    # =====================================================================

    @abstractmethod
    def deploy(
        self,
        script_path: Path,
        *,
        app_name: str,
        environment_name: str | None = None,
    ) -> None:
        """Deploy a script to Modal (mirrors `modal deploy` CLI)."""
        ...
