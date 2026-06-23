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


# ---------------------------------------------------------------------------
# PROXIED-mode wire protocol
# ---------------------------------------------------------------------------
# Request/response bodies for the connector's ``/modal/sandboxes`` routes.
# Shared by ``RemoteModalInterface`` (client) and the connector handlers
# (server) so the wire contract has a single source of truth. The connector
# fulfils each request with a ``DirectModalInterface`` running inside Modal
# (ambient control-plane credentials), so the client never holds a Modal token.


class SandboxCreateRequest(FrozenModel):
    """Body for ``POST /modal/sandboxes`` -- create a sandbox server-side."""

    app_name: str = Field(description="Modal app name to create the sandbox under")
    environment_name: str = Field(description="Modal environment name scoping the app/sandbox")
    image_ref: str | None = Field(
        default=None,
        description="Registry image reference (e.g. 'python:3.12-slim'). None => debian_slim base.",
    )
    apt_packages: tuple[str, ...] = Field(
        default=(),
        description="apt packages to install on top of the base image (only used when image_ref is None)",
    )
    timeout: int = Field(description="Sandbox timeout in seconds")
    cpu: float = Field(description="CPU cores")
    memory: int = Field(description="Memory in MB")
    unencrypted_ports: tuple[int, ...] = Field(
        default=(), description="Ports exposed via a public TCP tunnel (e.g. the sshd port)"
    )
    gpu: str | None = Field(default=None, description="GPU type, or None for no GPU")
    region: str | None = Field(default=None, description="Region hint, or None to let Modal choose")
    cidr_allowlist: tuple[str, ...] | None = Field(
        default=None, description="Optional CIDR allowlist for the sandbox's inbound tunnel"
    )


class SandboxCreateResponse(FrozenModel):
    """Response for ``POST /modal/sandboxes``."""

    sandbox_id: str = Field(description="The created sandbox's object id")


class SandboxExecRequest(FrozenModel):
    """Body for ``POST /modal/sandboxes/{sandbox_id}/exec``."""

    args: tuple[str, ...] = Field(description="Command argv to run inside the sandbox")
    is_background: bool = Field(
        default=False,
        description=(
            "When True, start the process detached and return immediately (e.g. 'sshd -D'); the "
            "connector does not wait for completion. When False, run to completion and return exit "
            "code + stdout."
        ),
    )
    is_stdout_captured: bool = Field(
        default=True, description="When False, stdout is discarded (mirrors StreamType.DEVNULL)"
    )


class SandboxExecResponse(FrozenModel):
    """Response for ``POST /modal/sandboxes/{sandbox_id}/exec``."""

    exit_code: int = Field(description="Process exit code (0 for background starts)")
    stdout: str = Field(default="", description="Captured stdout (empty for background or discarded streams)")


class SandboxTunnelsResponse(FrozenModel):
    """Response for ``GET /modal/sandboxes/{sandbox_id}/tunnels``.

    Tunnel keys are the container port as a string (JSON object keys are
    strings); the client converts them back to ints.
    """

    tunnels: dict[str, TunnelInfo] = Field(description="Map of container port (as string) -> tunnel endpoint")


class SandboxTagsRequest(FrozenModel):
    """Body for ``PUT /modal/sandboxes/{sandbox_id}/tags``."""

    tags: dict[str, str] = Field(description="Tags to set on the sandbox (replaces all tags)")


class SandboxInfo(FrozenModel):
    """A single sandbox in a list response."""

    sandbox_id: str = Field(description="The sandbox's object id")
    tags: dict[str, str] = Field(default_factory=dict, description="The sandbox's tags")


class SandboxListResponse(FrozenModel):
    """Response for ``GET /modal/sandboxes``."""

    sandboxes: tuple[SandboxInfo, ...] = Field(default=(), description="Matching sandboxes")
