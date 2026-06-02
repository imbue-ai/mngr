import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path

from loguru import logger

from imbue.imbue_common.logging import log_span
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.host import OnlineHostInterface

# A source is either a local file to copy, or in-memory content to write.
FileSource = bytes | str | Path


def resolve_remote_path(dest_path: Path, remote_home: str) -> Path:
    """Resolve a destination to an absolute remote path.

    ``~`` resolves to the remote home, ``~/x`` to ``<home>/x``, a relative path to
    ``<home>/<path>`` (matching ``write_file``'s relative-to-login-dir semantics on a
    remote host), and an absolute path is returned unchanged.
    """
    dest_str = str(dest_path)
    if dest_str == "~":
        return Path(remote_home)
    if dest_str.startswith("~/"):
        return Path(remote_home) / dest_str.removeprefix("~/")
    if dest_path.is_absolute():
        return dest_path
    return Path(remote_home) / dest_path


def _write_source(source: FileSource, dest: Path) -> None:
    """Write a single source to an absolute local ``dest`` (creating parents)."""
    if isinstance(source, Path):
        # On a local host the source can already BE the destination (e.g. a file
        # already present in the agent work_dir). copyfile would raise SameFileError,
        # so treat that as a no-op -- the file is already in place. For a remote
        # target the dest is in the staging dir, never the source, so this never
        # triggers there.
        if source.resolve() == dest.resolve():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        # copyfile (not copy2) writes with default perms and does not preserve the
        # source file's mode.
        shutil.copyfile(source, dest)
    elif isinstance(source, bytes):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(source)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(source)


def upload_files_in_bulk(
    target_host: OnlineHostInterface,
    files: Mapping[Path, FileSource],
    remote_home: str,
    skip_missing: bool,
) -> int:
    """Transfer many files to ``target_host`` in a single rsync (remote) or copy (local).

    ``files`` maps a destination to a source (a local ``Path`` to copy, or
    ``bytes``/``str`` content to write). Returns the number of files transferred.

    A ``Path`` source that does not exist locally raises ``MngrError`` by default --
    callers that genuinely upload an optional/best-effort set (e.g. agent file
    transfers whose required files were already validated, or collected deploy files)
    pass ``skip_missing=True`` to skip them instead.

    For a remote target, destinations are interpreted as remote paths (absolute,
    ``~``/``~/...``, or relative-to-home via ``remote_home``), staged into a local
    temp dir, and transferred with one ``copy_local_directory`` (rsync) into the
    *tightest common-ancestor directory* of the destinations. Targeting the common
    ancestor (rather than "/") keeps rsync from stamping perms/mtimes on intermediate
    directories we are not writing into (the volume root, the agent-state dir, ...).
    Note that what actually protects against clobbering a symlinked directory (e.g.
    Modal's host_dir symlink into the mounted volume) is ``--keep-dirlinks`` inside
    ``copy_local_directory`` -- the common-ancestor scoping is hygiene on top of that.
    For a local target the files are written directly to the destination as given
    (preserving ``write_file``'s relative-to-cwd semantics); ``remote_home`` is unused
    and rsync is unnecessary since writes are local.
    """
    missing = [src for src in files.values() if isinstance(src, Path) and not src.exists()]
    if missing:
        if not skip_missing:
            raise MngrError("Upload source(s) do not exist locally: " + ", ".join(str(m) for m in missing))
        logger.debug("Skipping {} upload source(s) that do not exist locally", len(missing))
    present = [(dest, src) for dest, src in files.items() if not (isinstance(src, Path) and not src.exists())]
    if not present:
        return 0

    if target_host.is_local:
        for dest_path, source in present:
            _write_source(source, dest_path)
        return len(present)

    resolved = [(resolve_remote_path(dest, remote_home), source) for dest, source in present]
    # Rsync into the tightest directory containing every destination (commonpath of
    # the parent dirs), so rsync never touches unrelated ancestors like the volume
    # root or the agent-state directory.
    common_ancestor = Path(os.path.commonpath([str(remote_abs.parent) for remote_abs, _ in resolved]))
    with tempfile.TemporaryDirectory(prefix="mngr-upload-") as staging:
        staging_dir = Path(staging)
        for remote_abs, source in resolved:
            _write_source(source, staging_dir / remote_abs.relative_to(common_ancestor))
        with log_span("Uploading {} files to remote host via rsync into {}", len(present), common_ancestor):
            target_host.copy_local_directory(staging_dir, common_ancestor, None)
    return len(present)
