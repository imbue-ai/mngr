"""Unit + local-restic integration tests for backup_export.

restic is a required dependency of the minds app, so the integration test runs
unconditionally and FAILS (not skips) if ``restic`` is missing.
"""

import zipfile
from pathlib import Path
from uuid import uuid4

import pytest

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client import restic_cli
from imbue.minds.desktop_client.backup_env_store import write_canonical_env
from imbue.minds.desktop_client.backup_export import BackupExportError
from imbue.minds.desktop_client.backup_export import export_latest_snapshot_zip
from imbue.minds.desktop_client.backup_export import export_zip_path_for_host
from imbue.minds.desktop_client.backup_provisioning import build_canonical_env_content
from imbue.minds.desktop_client.testing import restic_backup_a_file
from imbue.mngr.primitives import AgentId


def _fresh_agent_id() -> AgentId:
    return AgentId("agent-" + uuid4().hex)


# --- export_zip_path_for_host ---


def test_export_zip_path_for_host_is_keyed_and_in_tmp() -> None:
    assert export_zip_path_for_host("host-abc123") == Path("/tmp/minds-backup-export-host-abc123.zip")


# --- error paths (no restic needed: they fail before invoking it) ---


def test_export_raises_without_canonical_env(tmp_path: Path) -> None:
    paths = WorkspacePaths(data_dir=tmp_path)
    with pytest.raises(BackupExportError):
        export_latest_snapshot_zip(paths=paths, agent_id=_fresh_agent_id(), host_id="host-" + uuid4().hex)


def test_export_raises_when_repository_missing(tmp_path: Path) -> None:
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = _fresh_agent_id()
    # Canonical env present but with no RESTIC_REPOSITORY line.
    write_canonical_env(paths, agent_id, "RESTIC_PASSWORD=secret\n")
    with pytest.raises(BackupExportError):
        export_latest_snapshot_zip(paths=paths, agent_id=agent_id, host_id="host-" + uuid4().hex)


# --- local restic integration ---


@pytest.mark.timeout(60)
def test_export_latest_snapshot_zip_produces_zip(tmp_path: Path) -> None:
    paths = WorkspacePaths(data_dir=tmp_path / "minds-data")
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

    target = export_latest_snapshot_zip(paths=paths, agent_id=agent_id, host_id=host_id)
    try:
        assert target == export_zip_path_for_host(host_id)
        assert zipfile.is_zipfile(target)
        with zipfile.ZipFile(target) as archive:
            names = archive.namelist()
        assert any(name.endswith("payload.txt") for name in names), names
    finally:
        target.unlink(missing_ok=True)
