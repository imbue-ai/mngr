"""Unit + local-restic integration tests for backup_export.

restic is a required dependency of the Imbue Studio app, so the integration test runs
unconditionally and FAILS (not skips) if ``restic`` is missing.
"""

import struct
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest

from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client import restic_cli
from imbue.minds.desktop_client.backup_env_store import write_canonical_env
from imbue.minds.desktop_client.backup_export import BackupExportError
from imbue.minds.desktop_client.backup_export import _zip_directory_contents
from imbue.minds.desktop_client.backup_export import export_snapshot_zip
from imbue.minds.desktop_client.backup_export import export_zip_path_for_host
from imbue.minds.desktop_client.backup_provisioning import build_canonical_env_content
from imbue.minds.desktop_client.testing import restic_backup_a_file
from imbue.mngr.primitives import AgentId


def _fresh_agent_id() -> AgentId:
    return AgentId("agent-" + uuid4().hex)


# --- export_zip_path_for_host ---


def test_export_zip_path_for_host_is_keyed_and_in_tmp() -> None:
    assert export_zip_path_for_host("host-abc123") == Path("/tmp/minds-backup-export-host-abc123.zip")


_LOCAL_FILE_HEADER_SIZE = 30
_ZIP64_EXTRA_HEADER_ID = 0x0001


def _local_header_extra_ids(zip_path: Path, zinfo: zipfile.ZipInfo) -> set[int]:
    """Return the extra-field header ids in the *local* file header of ``zinfo``."""
    with zip_path.open("rb") as f:
        f.seek(zinfo.header_offset)
        header = f.read(_LOCAL_FILE_HEADER_SIZE)
        name_len, extra_len = struct.unpack("<HH", header[26:30])
        f.seek(name_len, 1)
        extra = f.read(extra_len)
    ids: set[int] = set()
    offset = 0
    while offset + 4 <= len(extra):
        header_id, size = struct.unpack("<HH", extra[offset : offset + 4])
        ids.add(header_id)
        offset += 4 + size
    return ids


def test_zip_directory_contents_streams_files_as_zip64(tmp_path: Path) -> None:
    # Streamed entries must be opened ZIP64-capable: the size check runs only when
    # the entry closes, so a non-ZIP64 entry for a file over 2 GiB raises then.
    source = tmp_path / "source"
    (source / "sub").mkdir(parents=True)
    (source / "sub" / "data.txt").write_text("payload")
    (source / "link").symlink_to("sub/data.txt")
    zip_path = tmp_path / "out.zip"

    _zip_directory_contents(source, zip_path)

    with zipfile.ZipFile(zip_path) as archive:
        infos = {info.filename: info for info in archive.infolist()}
        assert archive.read("sub/data.txt") == b"payload"
    assert set(infos) == {"link", "sub/", "sub/data.txt"}
    assert _ZIP64_EXTRA_HEADER_ID in _local_header_extra_ids(zip_path, infos["sub/data.txt"])


# --- error paths (no restic needed: they fail before invoking it) ---


def test_export_raises_without_canonical_env(tmp_path: Path) -> None:
    paths = InstallationPaths(data_dir=tmp_path)
    with pytest.raises(BackupExportError):
        export_snapshot_zip(paths=paths, agent_id=_fresh_agent_id(), host_id="host-" + uuid4().hex)


def test_export_raises_when_repository_missing(tmp_path: Path) -> None:
    paths = InstallationPaths(data_dir=tmp_path)
    agent_id = _fresh_agent_id()
    # Canonical env present but with no RESTIC_REPOSITORY line.
    write_canonical_env(paths, agent_id, "RESTIC_PASSWORD=secret\n")
    with pytest.raises(BackupExportError):
        export_snapshot_zip(paths=paths, agent_id=agent_id, host_id="host-" + uuid4().hex)


# --- local restic integration ---


@pytest.mark.timeout(60)
def test_export_snapshot_zip_produces_zip(tmp_path: Path) -> None:
    paths = InstallationPaths(data_dir=tmp_path / "minds-data")
    agent_id = _fresh_agent_id()
    host_id = "host-" + uuid4().hex
    repo = str(tmp_path / "repo")
    password = "export-int-pw"

    # Stand up a real local repo with one snapshot, then point the canonical env at it.
    restic_cli.init_repo(repository=repo, backend_env={}, password=password)
    source = tmp_path / "payload.txt"
    source.write_text("backed up content")
    restic_backup_a_file(repo, password, source)
    write_canonical_env(
        paths, agent_id, build_canonical_env_content(repository=repo, backend_env={}, workspace_password=password)
    )

    target = export_snapshot_zip(paths=paths, agent_id=agent_id, host_id=host_id)
    try:
        assert target == export_zip_path_for_host(host_id)
        assert zipfile.is_zipfile(target)
        with zipfile.ZipFile(target) as archive:
            names = archive.namelist()
        assert any(name.endswith("payload.txt") for name in names), names
    finally:
        target.unlink(missing_ok=True)
