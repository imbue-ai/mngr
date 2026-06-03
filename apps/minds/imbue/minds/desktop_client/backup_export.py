"""Export a workspace's latest restic snapshot as a downloadable zip.

minds holds the canonical ``restic.env`` for each workspace, so it can build a
zip of the latest snapshot from the minds machine -- without the workspace
being reachable -- by restoring the snapshot to a temporary directory and
zipping it. (``restic restore`` downloads in parallel and is ~50x faster than
``restic dump --archive zip``, which fetches blobs sequentially.) The zip is
written to a ``/tmp`` path keyed by host id, so repeated exports overwrite the
previous file rather than accumulating.
"""

import os
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client import restic_cli
from imbue.minds.desktop_client.backup_env_store import parse_restic_env
from imbue.minds.desktop_client.backup_env_store import read_canonical_env
from imbue.minds.errors import BackupProvisioningError
from imbue.mngr.primitives import AgentId

_EXPORT_DIR = Path("/tmp")
_EXPORT_FILENAME_PREFIX = "minds-backup-export-"
_RESTORE_DIR_PREFIX = "minds-backup-restore-"
# Fast zip: the restored files aren't pre-compressed, but download convenience
# matters more than ratio here, so use the cheapest deflate level.
_ZIP_COMPRESS_LEVEL = 1
# The zip format can't represent timestamps before 1980 (and ``ZipFile.write``
# rejects them outright). Some restored files legitimately carry such mtimes
# (e.g. epoch-0 placeholders), so clamp to this floor.
_MIN_ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)


class BackupExportError(BackupProvisioningError):
    """Raised when a workspace's snapshot cannot be exported to a zip."""


def export_zip_path_for_host(host_id: str) -> Path:
    """Return the /tmp path the export zip is written to (keyed by host id)."""
    return _EXPORT_DIR / f"{_EXPORT_FILENAME_PREFIX}{host_id}.zip"


def _zip_date_time(mtime: float) -> tuple[int, int, int, int, int, int]:
    """Return a zip-legal (>= 1980) local date_time tuple for ``mtime``."""
    try:
        parsed = time.localtime(mtime)[:6]
    except (OSError, ValueError, OverflowError):
        return _MIN_ZIP_DATE_TIME
    return parsed if parsed >= _MIN_ZIP_DATE_TIME else _MIN_ZIP_DATE_TIME


def _zip_directory_contents(source_dir: Path, zip_path: Path) -> None:
    """Write a zip of everything under ``source_dir`` (paths relative to it).

    Builds each ``ZipInfo`` by hand (rather than ``ZipFile.write``) so pre-1980
    mtimes are clamped instead of raising, symlinks are stored as symlinks
    rather than followed, and large files stream instead of loading into memory.
    """
    with zipfile.ZipFile(
        zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=_ZIP_COMPRESS_LEVEL
    ) as archive:
        for entry in sorted(source_dir.rglob("*")):
            info = entry.lstat()
            arcname = entry.relative_to(source_dir).as_posix()
            is_symlink = entry.is_symlink()
            is_dir = entry.is_dir() and not is_symlink
            zinfo = zipfile.ZipInfo(arcname + "/" if is_dir else arcname, date_time=_zip_date_time(info.st_mtime))
            # Preserve unix mode bits (incl. the file-type, so symlinks/dirs restore correctly).
            zinfo.external_attr = (info.st_mode & 0xFFFF) << 16
            if is_dir:
                archive.writestr(zinfo, b"")
            elif is_symlink:
                archive.writestr(zinfo, os.readlink(entry))
            else:
                zinfo.compress_type = zipfile.ZIP_DEFLATED
                with entry.open("rb") as source_file, archive.open(zinfo, "w") as dest_file:
                    shutil.copyfileobj(source_file, dest_file)


def export_latest_snapshot_zip(
    *,
    paths: WorkspacePaths,
    agent_id: AgentId,
    host_id: str,
    parent_cg: ConcurrencyGroup | None = None,
) -> Path:
    """Restore the workspace's latest snapshot and zip it; return the zip path.

    Raises ``BackupExportError`` when the workspace has no canonical restic.env
    (backups were never configured) or its repository address is missing;
    propagates ``BackupProvisioningError`` if restic itself fails.
    """
    content = read_canonical_env(paths, agent_id)
    if content is None:
        raise BackupExportError(f"No backups are configured for {agent_id}")
    env = parse_restic_env(content)
    repository = env.get("RESTIC_REPOSITORY", "")
    if not repository:
        raise BackupExportError(f"Canonical restic.env for {agent_id} has no RESTIC_REPOSITORY")
    password = env.get("RESTIC_PASSWORD")
    backend_env = {key: value for key, value in env.items() if key not in ("RESTIC_REPOSITORY", "RESTIC_PASSWORD")}

    zip_path = export_zip_path_for_host(host_id)
    restore_dir = Path(tempfile.mkdtemp(prefix=f"{_RESTORE_DIR_PREFIX}{host_id}-", dir=_EXPORT_DIR))
    try:
        restic_cli.restore_snapshot(
            repository=repository,
            backend_env=backend_env,
            password=password,
            target_dir=restore_dir,
            parent_cg=parent_cg,
        )
        _zip_directory_contents(restore_dir, zip_path)
    finally:
        # Always clean up the (potentially large) restored tree.
        try:
            shutil.rmtree(restore_dir, ignore_errors=True)
        except OSError as e:
            logger.warning("Could not remove temp restore dir {}: {}", restore_dir, e)
    return zip_path
