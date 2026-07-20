"""Write and delete chat attachments in an agent's working directory.

The composer lets the user paste an image or attach a file. The client generates
the name ``<uuid>.<ext>`` up front and drops a ``[FILE: ./chat_uploads/<uuid>.<ext>]``
token into the message where the cursor sits; foreman stores the bytes at
``<agent work_dir>/chat_uploads/<uuid>.<ext>`` on the agent's host. Because the
path is relative to the agent's cwd (its work_dir), Claude Code reads it natively
where it appears in the text.

One shared place defines the directory name, the size cap, and the stored-name
sanitiser, so the write and delete paths cannot disagree.
"""

from __future__ import annotations

import shlex
import time
from pathlib import Path
from typing import Any

from loguru import logger

from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr_foreman.connection_pool import ConnectionPool

# Where uploads land under the agent's work_dir, and the token path prefix.
UPLOAD_DIR_NAME = "chat_uploads"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

# The stored name must be a bare ``<uuid>.<ext>`` basename: no path separators, no
# ``..``, and a conservative charset. An extension of at most a few alnum chars.
_MAX_EXT_LENGTH = 12
_MAX_STORED_NAME_LENGTH = 128


# Serve cache: reading a mirrored file back over SFTP for every <img> render is
# wasteful, so cache recently served bytes briefly, bounded by count and total
# size. Keyed by (agent_name, stored_name).
_SERVE_CACHE: dict[tuple[str, str], tuple[float, bytes]] = {}
_SERVE_CACHE_TTL_SECONDS = 30.0
_SERVE_CACHE_MAX_ENTRIES = 32
_SERVE_CACHE_MAX_BYTES = 64 * 1024 * 1024

# Content types we serve inline for common attachment kinds; anything else is a
# generic download.
_CONTENT_TYPE_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
    "svg": "image/svg+xml",
    "pdf": "application/pdf",
    "txt": "text/plain; charset=utf-8",
    "md": "text/plain; charset=utf-8",
    "json": "application/json",
    "csv": "text/csv; charset=utf-8",
}


class UploadError(Exception):
    """A rejected upload (bad name, oversize, or host-resolution failure)."""


class UploadNotFound(UploadError):
    """The requested upload does not exist (deleted, or never written)."""


def content_type_for_name(name: str) -> str:
    """Best-effort MIME type from a ``<uuid>.<ext>`` name (generic if unknown)."""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return _CONTENT_TYPE_BY_EXT.get(ext, "application/octet-stream")


def sanitize_stored_name(name: str) -> str:
    """Validate a client-provided ``<uuid>.<ext>`` basename, or raise UploadError.

    Rejects anything that is not a single safe path component: no ``/``, no ``..``,
    a bounded length, exactly one extension, and only ``[A-Za-z0-9._-]``.
    """
    if not name or len(name) > _MAX_STORED_NAME_LENGTH:
        raise UploadError("invalid file name")
    # Must be a bare basename (no directory parts, no traversal).
    if name != Path(name).name or ".." in name or name.startswith("."):
        raise UploadError("invalid file name")
    if not all(c.isalnum() or c in "._-" for c in name):
        raise UploadError("invalid file name")
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem or not ext or len(ext) > _MAX_EXT_LENGTH or not ext.isalnum():
        raise UploadError("invalid file name (need <name>.<ext>)")
    return name


def write_upload(pool: ConnectionPool, agent_name: str, stored_name: str, data: bytes) -> str:
    """Write ``data`` to ``<work_dir>/chat_uploads/<stored_name>`` on the agent host.

    Resolves through the warm connection pool. Returns the workdir-relative token
    path (``./chat_uploads/<stored_name>``) the client embedded in the message.
    Raises ``UploadError`` on a bad name, an oversize payload, or an unreachable host.
    """
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError(f"file too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)}MB)")
    safe = sanitize_stored_name(stored_name)

    def _write(agent: AgentInterface, host: OnlineHostInterface) -> None:
        # write_file writes bytes verbatim (binary-safe) and creates parent dirs.
        host.write_file(agent.work_dir / UPLOAD_DIR_NAME / safe, data)

    try:
        pool.run_on_host(agent_name, _write)
    except Exception as e:  # noqa: BLE001 - surface any resolution/write failure as a clean 4xx
        raise UploadError(f"could not write to agent host: {e}") from e
    logger.info("foreman: wrote upload chat_uploads/{} ({} bytes) for agent {}", safe, len(data), agent_name)
    return f"./{UPLOAD_DIR_NAME}/{safe}"


def _cache_put(key: tuple[str, str], data: bytes) -> None:
    if len(data) > _SERVE_CACHE_MAX_BYTES:
        return  # too big to be worth caching
    _SERVE_CACHE[key] = (time.monotonic(), data)
    # Evict oldest entries until back under both budgets.
    while _SERVE_CACHE and (
        len(_SERVE_CACHE) > _SERVE_CACHE_MAX_ENTRIES
        or sum(len(v[1]) for v in _SERVE_CACHE.values()) > _SERVE_CACHE_MAX_BYTES
    ):
        oldest = min(_SERVE_CACHE, key=lambda k: _SERVE_CACHE[k][0])
        if oldest == key:  # never evict the entry we just wrote
            break
        del _SERVE_CACHE[oldest]


def read_upload(pool: ConnectionPool, agent_name: str, stored_name: str) -> bytes:
    """Read a previously uploaded file's bytes from the agent host (briefly cached).

    Raises ``UploadNotFound`` (-> 404) if the file is gone or unreadable, and
    ``UploadError`` on a bad name.
    """
    safe = sanitize_stored_name(stored_name)
    key = (agent_name, safe)
    cached = _SERVE_CACHE.get(key)
    if cached is not None and time.monotonic() - cached[0] < _SERVE_CACHE_TTL_SECONDS:
        return cached[1]

    def _read(agent: AgentInterface, host: OnlineHostInterface) -> bytes:
        return host.read_file(agent.work_dir / UPLOAD_DIR_NAME / safe)

    try:
        data = pool.run_on_host(agent_name, _read)
    except Exception as e:  # noqa: BLE001 - missing/unreadable/unreachable -> a graceful 404
        raise UploadNotFound(f"not found: {safe}") from e
    _cache_put(key, data)
    return data


def delete_upload(pool: ConnectionPool, agent_name: str, stored_name: str) -> None:
    """Best-effort remove of a previously uploaded file from the agent's workdir."""
    safe = sanitize_stored_name(stored_name)
    _SERVE_CACHE.pop((agent_name, safe), None)

    def _rm(agent: AgentInterface, host: OnlineHostInterface) -> Any:
        return host.execute_stateful_command(f"rm -f {shlex.quote(str(agent.work_dir / UPLOAD_DIR_NAME / safe))}")

    result = pool.run_on_host(agent_name, _rm)
    if not result.success:
        raise UploadError(f"delete failed: {result.stderr or result.stdout}")
