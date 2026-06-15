"""Preserve files from an agent's state directory to local storage on destroy.

When an agent (or its whole host) is destroyed, the agent's state directory is
deleted. Some files in it are worth keeping -- session transcripts, logs, etc.
This module provides a single, source-agnostic way to copy a declared set of
those files to a stable local location *before* the state directory disappears.

The set of files to keep is declared once by the caller as a list of
:class:`PreservedItem` (paths relative to the agent state directory). The same
declaration is executed against either:

- an online host (:class:`~imbue.mngr.interfaces.host.OnlineHostInterface`),
  reading over SSH / locally and using rsync for directories, or
- a stopped-but-volume-backed host
  (:class:`~imbue.mngr.hosts.offline_host.OfflineHostWithVolume`), reading from
  the host's persisted volume.

Both are :class:`~imbue.mngr.interfaces.host.HostFileReadInterface`, so callers
do not branch on online-vs-offline: they pass whichever host they hold and the
single :func:`preserve_agent_data` call does the right thing. Preserved files
mirror the agent-state-dir layout verbatim under the destination root.
"""

from collections.abc import Sequence
from pathlib import Path

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr.interfaces.host import HostFileReadInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME


class PreservedItem(FrozenModel):
    """One file or directory to preserve, addressed relative to the agent state dir."""

    rel_path: str = Field(description="Path relative to the agent state directory")
    kind: FileType = Field(description="Whether rel_path is a FILE or a DIRECTORY")


def get_preserved_agents_root_dir(host_dir: Path) -> Path:
    """Return the directory under which all agents' preserved files are stored.

    This is the single source of truth for where preserved agent data lives on
    disk, so code that needs to enumerate preserved agents (rather than address
    a single one) can do so without duplicating the path structure.

    ``host_dir`` should be the *local* host directory: preserved files always
    live on the local machine so they survive remote host destruction.
    """
    return host_dir / "preserved"


def get_preserved_agent_dir(host_dir: Path, agent_name: AgentName, agent_id: AgentId) -> Path:
    """Return the directory under which an agent's preserved files are stored.

    This is the single source of truth for the on-disk layout of preserved
    agent data, so other code (and other plugins) can read those files without
    duplicating the path structure. Preserved files mirror the agent's state
    directory layout underneath this directory.

    ``host_dir`` should be the *local* host directory: preserved files always
    live on the local machine so they survive remote host destruction.
    """
    return get_preserved_agents_root_dir(host_dir) / f"{agent_name}--{agent_id}"


def get_local_preserved_agent_dir(mngr_ctx: MngrContext, agent_name: AgentName, agent_id: AgentId) -> Path:
    """Return the local preserved-files directory for an agent."""
    local_host_dir = Path(mngr_ctx.config.default_host_dir).expanduser()
    return get_preserved_agent_dir(local_host_dir, agent_name, agent_id)


def preserve_agent_data(
    items: Sequence[PreservedItem],
    source: HostFileReadInterface,
    agent_state_dir: Path,
    dest_root: Path,
    mngr_ctx: MngrContext,
) -> None:
    """Copy the declared items from ``source`` to ``dest_root``, mirroring layout.

    Each item is read from ``agent_state_dir / item.rel_path`` on ``source`` and
    written to ``dest_root / item.rel_path`` locally. Items that do not exist on
    the source are skipped. Failures for any single item are logged as warnings
    and do not abort the others (or the destruction that triggered this).

    For directories, an online source uses rsync (efficient over SSH); a
    volume-backed offline source walks and copies file-by-file. For single
    files both sources read bytes directly. ``agent_state_dir`` is the absolute
    path of the agent's state directory *as addressed on the source host*.
    """
    local_host: OnlineHostInterface | None = None
    with log_span("Preserving agent data to {}", dest_root):
        for item in items:
            src = agent_state_dir / item.rel_path
            dest = dest_root / item.rel_path
            try:
                if not source.path_exists(src):
                    # Items are usually expected to be present; a debug line helps
                    # diagnose why something did not get preserved.
                    logger.debug("Skipping preservation of {}: not present on source at {}", item.rel_path, src)
                    continue
                if item.kind == FileType.FILE:
                    _write_local_file(dest, source.read_file(src))
                elif isinstance(source, OnlineHostInterface):
                    if local_host is None:
                        local_host = _get_local_online_host(mngr_ctx)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    local_host.copy_directory(source, src, dest)
                else:
                    _copy_tree_via_reader(source, src, dest)
                logger.debug("Preserved {} -> {}", src, dest)
            except (MngrError, OSError) as e:
                logger.warning("Failed to preserve {}: {}", item.rel_path, e)


def _write_local_file(dest: Path, content: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)


def _copy_tree_via_reader(source: HostFileReadInterface, src_dir: Path, dest_dir: Path) -> None:
    """Recursively copy a directory tree from a (volume-backed) reader to local disk."""
    for entry in source.list_directory(src_dir, recursive=True):
        # Copy only regular files byte-for-byte. Directories are implied by the recursive
        # walk (their files carry the full relative path); symlinks/devices/pipes/sockets are
        # deliberately not reproduced -- this path copies content, not filesystem structure.
        # A volume-backed offline source only ever yields FILE/DIRECTORY anyway, but checking
        # explicitly for FILE keeps a richer-typed source from silently changing behavior.
        if entry.file_type != FileType.FILE:
            continue
        relative = Path(entry.path).relative_to(src_dir)
        _write_local_file(dest_dir / relative, source.read_file(Path(entry.path)))


def _get_local_online_host(mngr_ctx: MngrContext) -> OnlineHostInterface:
    """Resolve the local host as an OnlineHostInterface (the rsync copy target)."""
    host_interface = get_provider_instance(LOCAL_PROVIDER_NAME, mngr_ctx).get_host(HostName("localhost"))
    if not isinstance(host_interface, OnlineHostInterface):
        raise MngrError("Local host is not online")
    return host_interface
