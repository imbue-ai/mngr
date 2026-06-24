# Simple data types used by the modal_proxy interfaces.

from enum import auto

from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel


class StreamType(UpperCaseStrEnum):
    """Controls how stdout/stderr are handled for sandbox exec commands.

    Mirrors modal.stream_type.StreamType for the values we use.
    """

    PIPE = auto()
    DEVNULL = auto()


class TunnelInfo(FrozenModel):
    """Connection info for a sandbox port tunnel.

    Mirrors the tunnel object returned by modal.Sandbox.tunnels().
    """

    tcp_socket: tuple[str, int] = Field(description="(host, port) tuple for the tunnel endpoint")


class FileEntryType(UpperCaseStrEnum):
    """Type of entry in a volume directory listing.

    Mirrors modal.volume.FileEntryType.
    """

    FILE = auto()
    DIRECTORY = auto()


class FileEntry(FrozenModel):
    """A single file or directory entry from a volume listing.

    Mirrors modal.volume.FileEntry for the fields we use.
    """

    path: str = Field(description="Path of the entry")
    type: FileEntryType = Field(description="Whether this is a file or directory")
    mtime: float = Field(default=0.0, description="Last modification time as a Unix timestamp")
    size: int = Field(default=0, description="Size in bytes")
