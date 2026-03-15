"""Data types for the ModalInterface abstraction.

These types are used as parameters and return values in ModalInterface methods,
providing a Modal-SDK-independent representation of the concepts needed by mng_modal.
"""

from enum import auto
from pathlib import Path

from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel


class ExecStreamType(UpperCaseStrEnum):
    """Controls how stdout/stderr are handled for sandbox exec commands."""

    PIPE = auto()
    DEVNULL = auto()


class TunnelInfo(FrozenModel):
    """Connection info for a sandbox port tunnel."""

    host: str = Field(description="Hostname for the tunnel endpoint")
    port: int = Field(description="Port number for the tunnel endpoint")


class FileEntryType(UpperCaseStrEnum):
    """Type of entry in a volume directory listing."""

    FILE = auto()
    DIRECTORY = auto()


class FileEntry(FrozenModel):
    """A single file or directory entry from a volume listing."""

    path: str = Field(description="Path of the entry relative to the listed directory")
    entry_type: FileEntryType = Field(description="Whether this is a file or directory")
    mtime: float = Field(default=0.0, description="Last modification time as a Unix timestamp")
    size: int = Field(default=0, description="Size of the entry in bytes")


class VolumeRef(FrozenModel):
    """Reference to a volume returned by list_volumes."""

    name: str | None = Field(description="Name of the volume (if named)")


class ImageBuildContext(FrozenModel):
    """Context for building an image from Dockerfile commands."""

    commands: tuple[str, ...] = Field(description="Dockerfile commands to apply")
    context_dir: Path | None = Field(default=None, description="Build context directory for COPY/ADD instructions")
    secret_env_vars: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Environment variable names whose values are passed as build secrets",
    )
