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
from pathlib import Path

from loguru import logger

from imbue.mngr.api.address_parsers import parse_agent_address
from imbue.mngr.api.find import find_one_agent
from imbue.mngr.api.find import resolve_to_started_host_and_agent
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface

# Where uploads land under the agent's work_dir, and the token path prefix.
UPLOAD_DIR_NAME = "chat_uploads"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

# The stored name must be a bare ``<uuid>.<ext>`` basename: no path separators, no
# ``..``, and a conservative charset. An extension of at most a few alnum chars.
_MAX_EXT_LENGTH = 12
_MAX_STORED_NAME_LENGTH = 128


class UploadError(Exception):
    """A rejected upload (bad name, oversize, or host-resolution failure)."""


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


def _resolve_started_agent_and_host(
    mngr_ctx: MngrContext, agent_name: str
) -> tuple[AgentInterface, OnlineHostInterface]:
    address = parse_agent_address(agent_name)
    host_ref, agent_ref = find_one_agent(address, mngr_ctx)
    return resolve_to_started_host_and_agent(
        host_ref=host_ref,
        agent_ref=agent_ref,
        allow_auto_start=False,
        mngr_ctx=mngr_ctx,
    )


def _upload_path(agent: AgentInterface, stored_name: str) -> Path:
    return agent.work_dir / UPLOAD_DIR_NAME / stored_name


def write_upload(mngr_ctx: MngrContext, agent_name: str, stored_name: str, data: bytes) -> str:
    """Write ``data`` to ``<work_dir>/chat_uploads/<stored_name>`` on the agent host.

    Returns the workdir-relative token path (``./chat_uploads/<stored_name>``) that
    the client embedded in the message. Raises ``UploadError`` on a bad name, an
    oversize payload, or if the agent host cannot be reached.
    """
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError(f"file too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)}MB)")
    safe = sanitize_stored_name(stored_name)
    try:
        agent, host = _resolve_started_agent_and_host(mngr_ctx, agent_name)
    except Exception as e:  # noqa: BLE001 - surface any resolution failure as a clean 4xx
        raise UploadError(f"could not resolve agent host: {e}") from e
    dest = _upload_path(agent, safe)
    # write_file writes bytes verbatim (binary-safe) and creates parent dirs.
    host.write_file(dest, data)
    logger.info("foreman: wrote upload {} ({} bytes) for agent {}", dest, len(data), agent_name)
    return f"./{UPLOAD_DIR_NAME}/{safe}"


def delete_upload(mngr_ctx: MngrContext, agent_name: str, stored_name: str) -> None:
    """Best-effort remove of a previously uploaded file from the agent's workdir."""
    safe = sanitize_stored_name(stored_name)
    agent, host = _resolve_started_agent_and_host(mngr_ctx, agent_name)
    dest = _upload_path(agent, safe)
    result = host.execute_stateful_command(f"rm -f {shlex.quote(str(dest))}")
    if not result.success:
        raise UploadError(f"delete failed: {result.stderr or result.stdout}")
