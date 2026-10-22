"""The same-machine API socket: where it lives and who may connect to it.

The desktop client serves its Flask app on a second cheroot listener bound to a
Unix domain socket, so an agent running as the user can call the API with no
credential at all: the kernel reports the connecting process's uid, and a
connection from any other uid is closed before a byte is read. Browsers cannot
open Unix sockets, so nothing here is reachable from a web page. The design is
``specs/imbue-studio-agent-access/spec.md``, Part 2.

The canonical path is ``<state root>/api.sock``. Unix socket paths are capped
at 104 bytes on macOS and 108 on Linux (terminator included), and the macOS
state root is long, so a path over the cap falls back to the per-user runtime
directory; ``app.json`` beside the state root then records where the socket
really is. ``MINDS_API_SOCKET`` overrides both with an explicit path, for an
operator or an agent harness that wants the socket somewhere of its own.
"""

import os
import socket
import struct
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import Final

from cheroot import wsgi
from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.api_transport import mark_requests_local_trusted
from imbue.minds.desktop_client.ws_gateway import WebSocketAwareGateway
from imbue.minds.errors import MindError

API_SOCKET_ENV_VAR: Final[str] = "MINDS_API_SOCKET"
API_SOCKET_FILENAME: Final[str] = "api.sock"

_DARWIN_PLATFORM: Final[str] = "darwin"
# ``sun_path`` is 104 bytes on macOS and 108 on Linux; one byte is the terminator.
_MAX_SOCKET_PATH_BYTES_DARWIN: Final[int] = 103
_MAX_SOCKET_PATH_BYTES_OTHER: Final[int] = 107

_SOCKET_FILE_MODE: Final[int] = 0o600
_SOCKET_DIR_MODE: Final[int] = 0o700

# Linux: ``struct ucred { pid_t pid; uid_t uid; gid_t gid; }``.
_LINUX_PEERCRED_FORMAT: Final[str] = "iII"
# macOS: ``struct xucred { u_int cr_version; uid_t cr_uid; short cr_ngroups; gid_t cr_groups[16]; }``.
# Only the leading version and uid are read; the option is asked for with room
# for the whole struct (and its alignment padding) so the kernel fills it in.
_DARWIN_XUCRED_PREFIX_FORMAT: Final[str] = "II"
_DARWIN_XUCRED_BUFFER_BYTES: Final[int] = 128
# ``SOL_LOCAL`` and ``LOCAL_PEERCRED`` from ``<sys/un.h>``; Python's socket module does not export them.
_DARWIN_SOL_LOCAL: Final[int] = 0
_DARWIN_LOCAL_PEERCRED: Final[int] = 1

_PROBE_TIMEOUT_SECONDS: Final[float] = 1.0


class ApiSocketError(MindError):
    """Raised when the same-machine API socket cannot be set up."""


class ApiSocketInUseError(ApiSocketError):
    """Raised when another desktop client already answers on this tier's socket."""


class PeerCredentialsError(ApiSocketError, ValueError):
    """Raised when a connection's peer credentials cannot be read or parsed."""


class ApiSocketLocation(FrozenModel):
    """Where this tier's socket is bound, and whether that is the canonical place."""

    path: Path = Field(description="The socket path")
    is_fallback: bool = Field(description="True when the canonical path was too long and the runtime dir was used")


class PeerCredentials(FrozenModel):
    """Who is on the other end of a Unix socket connection, as the kernel reports it."""

    uid: int = Field(description="The connecting process's effective uid")
    pid: int | None = Field(default=None, description="The connecting process's pid, where the platform reports it")


def max_socket_path_bytes(platform: str) -> int:
    if platform == _DARWIN_PLATFORM:
        return _MAX_SOCKET_PATH_BYTES_DARWIN
    return _MAX_SOCKET_PATH_BYTES_OTHER


def canonical_api_socket_path(state_dir: Path) -> Path:
    return state_dir / API_SOCKET_FILENAME


def fallback_api_socket_path(tier: str, platform: str, environ: Mapping[str, str], uid: int) -> Path:
    """The per-user runtime location used when the canonical path is too long.

    macOS has a per-user temp dir in ``TMPDIR``; Linux has ``XDG_RUNTIME_DIR``
    when a session manager set it. Without either, the name under the shared
    ``/tmp`` carries the uid, so two users of one machine never meet at one path.
    """
    runtime_dir = environ.get("TMPDIR") if platform == _DARWIN_PLATFORM else environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        return Path(runtime_dir) / f"imbue-studio-{tier}.sock"
    return Path("/tmp") / f"imbue-studio-{uid}-{tier}.sock"


def resolve_api_socket_location(
    state_dir: Path,
    tier: str,
    platform: str = sys.platform,
    environ: Mapping[str, str] = os.environ,
    uid: int = os.getuid(),
) -> ApiSocketLocation:
    """Pick the socket path: the explicit override, else the canonical path, else the runtime-dir fallback."""
    explicit = environ.get(API_SOCKET_ENV_VAR)
    if explicit:
        return ApiSocketLocation(path=Path(explicit), is_fallback=False)
    canonical = canonical_api_socket_path(state_dir)
    if len(str(canonical).encode()) <= max_socket_path_bytes(platform):
        return ApiSocketLocation(path=canonical, is_fallback=False)
    return ApiSocketLocation(path=fallback_api_socket_path(tier, platform, environ, uid), is_fallback=True)


def parse_peer_credentials(raw: bytes, platform: str) -> PeerCredentials:
    """Decode the kernel's peer-credential option into the uid (and pid where present).

    Raises ``PeerCredentialsError`` when the bytes are shorter than the platform's struct.
    """
    if platform == _DARWIN_PLATFORM:
        prefix_size = struct.calcsize(_DARWIN_XUCRED_PREFIX_FORMAT)
        if len(raw) < prefix_size:
            raise PeerCredentialsError(f"LOCAL_PEERCRED returned {len(raw)} bytes, expected at least {prefix_size}")
        _version, uid = struct.unpack_from(_DARWIN_XUCRED_PREFIX_FORMAT, raw)
        return PeerCredentials(uid=uid, pid=None)
    size = struct.calcsize(_LINUX_PEERCRED_FORMAT)
    if len(raw) < size:
        raise PeerCredentialsError(f"SO_PEERCRED returned {len(raw)} bytes, expected {size}")
    pid, uid, _gid = struct.unpack_from(_LINUX_PEERCRED_FORMAT, raw)
    return PeerCredentials(uid=uid, pid=pid)


def read_peer_credentials(connection: socket.socket) -> PeerCredentials:
    """Ask the kernel who is on the other end of an accepted Unix socket connection.

    Raises ``PeerCredentialsError`` when the socket refuses the option, or on a
    platform with no peer-credential option this reads.
    """
    try:
        if sys.platform == "darwin":
            raw = connection.getsockopt(_DARWIN_SOL_LOCAL, _DARWIN_LOCAL_PEERCRED, _DARWIN_XUCRED_BUFFER_BYTES)
        elif sys.platform == "linux":
            raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize(_LINUX_PEERCRED_FORMAT))
        else:
            raise PeerCredentialsError(f"Reading peer credentials is not supported on {sys.platform}")
    except OSError as e:
        raise PeerCredentialsError(f"Could not read the peer credentials of a socket connection: {e}") from e
    return parse_peer_credentials(raw, sys.platform)


def is_peer_allowed(credentials: PeerCredentials, allowed_uid: int) -> bool:
    return credentials.uid == allowed_uid


class LocalSocketWsgiServer(wsgi.Server):
    """A cheroot server on a Unix socket that admits only this user's processes.

    The peer check runs in ``process_conn``, the first thing cheroot does with an
    accepted connection and before any worker reads from it; a connection from
    another uid is closed there and never queued. ``allowed_uid`` is a plain
    attribute :func:`create_local_socket_server` sets after construction, since
    cheroot's server is not a pydantic model.
    """

    # No uid matches until the factory sets the real one, so a server built any
    # other way admits nobody.
    allowed_uid: int = -1

    def process_conn(self, conn: Any) -> None:
        try:
            credentials = read_peer_credentials(conn.socket)
        except PeerCredentialsError as e:
            logger.warning("Refusing a local-socket connection whose peer could not be identified: {}", e)
            conn.close()
            return
        if not is_peer_allowed(credentials, self.allowed_uid):
            logger.warning(
                "Refusing a local-socket connection from uid {} (pid {}); only uid {} may connect",
                credentials.uid,
                credentials.pid,
                self.allowed_uid,
            )
            conn.close()
            return
        super().process_conn(conn)


def _is_socket_answering(path: Path) -> bool:
    """Whether a process is accepting connections on this socket path right now."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(_PROBE_TIMEOUT_SECONDS)
        try:
            probe.connect(str(path))
        except OSError:
            return False
    return True


def prepare_socket_path(path: Path, platform: str = sys.platform) -> None:
    """Make the path bindable: create its directory privately and clear a stale socket file.

    Raises ``ApiSocketInUseError`` when a live server answers there, since that
    is another desktop client for this tier and the two must not both run, and
    ``ApiSocketError`` when the path is longer than the platform's socket paths
    may be (an explicit ``MINDS_API_SOCKET``, or a runtime dir with a long
    name), when the directory cannot be made, when something that is not a
    socket already sits at the path (a foreign file at the shared ``/tmp``
    fallback path, say; it is refused, never removed), or when the stale socket
    cannot be removed.
    """
    path_bytes = len(str(path).encode())
    limit = max_socket_path_bytes(platform)
    if path_bytes > limit:
        raise ApiSocketError(f"The API socket path {path} is {path_bytes} bytes; this platform allows at most {limit}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=_SOCKET_DIR_MODE)
    except OSError as e:
        raise ApiSocketError(f"Could not create the API socket directory {path.parent}: {e}") from e
    if not path.exists():
        return
    if not path.is_socket():
        raise ApiSocketError(f"{path} exists and is not a socket; refusing to remove it to bind the API socket there")
    if _is_socket_answering(path):
        raise ApiSocketInUseError(
            f"Another Imbue Studio is already answering on {path}; refusing to start a second one"
        )
    logger.info("Removing the stale API socket left at {}", path)
    try:
        path.unlink()
    except OSError as e:
        raise ApiSocketError(f"Could not remove the stale API socket at {path}: {e}") from e


def create_local_socket_server(
    socket_path: Path,
    wsgi_app: Any,
    numthreads: int,
    allowed_uid: int = os.getuid(),
) -> LocalSocketWsgiServer:
    """Build (but do not bind) the local-socket listener serving ``wsgi_app`` as the local user."""
    server = LocalSocketWsgiServer(str(socket_path), mark_requests_local_trusted(wsgi_app), numthreads=numthreads)
    server.gateway = WebSocketAwareGateway
    server.allowed_uid = allowed_uid
    return server


def restrict_socket_file(path: Path) -> None:
    """Tighten the bound socket file to the owner only (cheroot binds it world-accessible)."""
    os.chmod(path, _SOCKET_FILE_MODE)
