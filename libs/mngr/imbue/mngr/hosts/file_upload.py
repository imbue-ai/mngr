import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path

from loguru import logger

from imbue.imbue_common.logging import log_span
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
        # so treat that as a no-op -- the file is already in place. (This matches the
        # old write_file(path, path.read_bytes()) behavior, which was a harmless
        # rewrite.) For a remote target the dest is in the staging dir, never the
        # source, so this never triggers there.
        if source.resolve() == dest.resolve():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        # copyfile (not copy2) writes with default perms, matching the previous
        # SFTP putfo / write_file behavior (which did not preserve source mode).
        shutil.copyfile(source, dest)
    elif isinstance(source, bytes):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(source)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(source)


def upload_files_in_bulk(
    target_host: OnlineHostInterface,
    local_host: OnlineHostInterface,
    files: Mapping[Path, FileSource],
    remote_home: str,
) -> int:
    """Transfer many files to ``target_host`` in a single rsync (remote) or copy (local).

    ``files`` maps a destination to a source (a local ``Path`` to copy, or
    ``bytes``/``str`` content to write). ``Path`` sources that do not exist on the
    local filesystem are skipped. Returns the number of files transferred.

    For a remote target, destinations are interpreted as remote paths (absolute,
    ``~``/``~/...``, or relative-to-home via ``remote_home``), staged into a local
    temp dir mirroring those absolute paths, and transferred with one
    ``copy_directory`` (rsync). For a local target the files are written directly to
    the destination as given (preserving ``write_file``'s relative-to-cwd semantics);
    ``remote_home`` is unused and rsync is unnecessary since writes are local.
    """
    present = [(dest, src) for dest, src in files.items() if not (isinstance(src, Path) and not src.exists())]
    skipped = len(files) - len(present)
    if skipped:
        logger.debug("Skipping {} upload source(s) that do not exist locally", skipped)
    if not present:
        return 0

    if target_host.is_local:
        for dest_path, source in present:
            _write_source(source, dest_path)
        return len(present)

    with tempfile.TemporaryDirectory(prefix="mngr-upload-") as staging:
        staging_dir = Path(staging)
        for dest_path, source in present:
            remote_abs = resolve_remote_path(dest_path, remote_home)
            _write_source(source, staging_dir / remote_abs.relative_to(remote_abs.anchor))
        with log_span("Uploading {} files to remote host via rsync", len(present)):
            target_host.copy_directory(local_host, staging_dir, Path("/"))
    return len(present)
