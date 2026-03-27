from abc import ABC
from abc import abstractmethod
from typing import Mapping

from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.interfaces.data_types import VolumeFile
from imbue.mngr.primitives import AgentId


class Volume(MutableModel, ABC):
    """Interface for accessing a volume's files.

    A volume is a persistent, file-system-like store that can be read from
    and written to. Implementations may scope operations to a path prefix
    within a backing store.

    This is the mngr-level volume abstraction. Multiple logical mngr Volumes
    may map to a single provider-level volume (e.g., a root host volume can
    provide scoped-down volumes for individual agents or subfolders).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @abstractmethod
    def listdir(self, path: str) -> list[VolumeFile]:
        """List file entries in the given directory path on the volume."""
        ...

    @abstractmethod
    def read_file(self, path: str) -> bytes:
        """Read a file from the volume and return its contents as bytes."""
        ...

    @abstractmethod
    def remove_file(self, path: str, *, recursive: bool = False) -> None:
        """Remove a file or directory from the volume.

        If recursive is True, removes the path and all its contents.
        """
        ...

    @abstractmethod
    def remove_directory(self, path: str) -> None:
        """Recursively remove a directory and all its contents."""
        ...

    @abstractmethod
    def write_files(self, file_contents_by_path: Mapping[str, bytes]) -> None:
        """Write one or more files to the volume."""
        ...

    @abstractmethod
    def scoped(self, prefix: str) -> "Volume":
        """Return a new Volume scoped to the given path prefix.

        All operations on the returned volume will be relative to the prefix.
        """
        ...


class BaseVolume(Volume):
    """Base implementation of Volume that provides scoping via ScopedVolume.

    Concrete volume implementations (ModalVolume, LocalVolume, etc.) should
    inherit from this class rather than from Volume directly.
    """

    def scoped(self, prefix: str) -> "Volume":
        """Return a ScopedVolume that prepends the given prefix to all operations."""
        return ScopedVolume(delegate=self, prefix=prefix)


def _scoped_path(base_prefix: str, path: str) -> str:
    """Prepend a base prefix to the given path."""
    base_prefix = base_prefix.rstrip("/")
    path = path.lstrip("/")
    return f"{base_prefix}/{path}" if path else base_prefix


class ScopedVolume(BaseVolume):
    """A volume that prepends a path prefix to all operations.

    Useful for giving an agent or subsystem a restricted view of a
    larger volume (e.g., a per-host volume scoped to a specific agent's
    subdirectory).
    """

    delegate: Volume = Field(frozen=True, description="The underlying volume to delegate to")
    prefix: str = Field(frozen=True, description="Path prefix prepended to all operations")

    def _strip_prefix(self, full_path: str) -> str:
        """Strip the scope prefix from a path returned by the delegate."""
        prefix = self.prefix.rstrip("/") + "/"
        stripped = full_path.lstrip("/")
        if stripped.startswith(prefix.lstrip("/")):
            return stripped[len(prefix.lstrip("/")) :]
        return stripped

    def listdir(self, path: str) -> list[VolumeFile]:
        entries = self.delegate.listdir(_scoped_path(self.prefix, path))
        return [
            VolumeFile(path=self._strip_prefix(e.path), file_type=e.file_type, mtime=e.mtime, size=e.size)
            for e in entries
        ]

    def read_file(self, path: str) -> bytes:
        return self.delegate.read_file(_scoped_path(self.prefix, path))

    def remove_file(self, path: str, *, recursive: bool = False) -> None:
        self.delegate.remove_file(_scoped_path(self.prefix, path), recursive=recursive)

    def remove_directory(self, path: str) -> None:
        self.delegate.remove_directory(_scoped_path(self.prefix, path))

    def write_files(self, file_contents_by_path: Mapping[str, bytes]) -> None:
        scoped = {_scoped_path(self.prefix, p): data for p, data in file_contents_by_path.items()}
        self.delegate.write_files(scoped)

    def scoped(self, prefix: str) -> "Volume":
        combined = f"{self.prefix}/{prefix.lstrip('/')}"
        return ScopedVolume(delegate=self.delegate, prefix=combined)


class HostVolume(FrozenModel):
    """A host-level volume with the ability to scope down to a specific agent."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    volume: Volume = Field(description="The underlying volume for the entire host")

    def get_agent_volume(self, agent_id: AgentId) -> Volume:
        """Return a Volume scoped to the given agent's subdirectory."""
        return self.volume.scoped(f"agents/{agent_id}")
