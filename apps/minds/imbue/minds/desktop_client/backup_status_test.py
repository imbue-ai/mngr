"""Unit + local-restic integration tests for backup status computation.

restic is a required dependency of the minds app (installed in the test
images), so the integration test runs unconditionally and FAILs -- not
skips -- if the ``restic`` binary is missing.
"""

import os
import subprocess
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client import restic_cli
from imbue.minds.desktop_client.backup_env_store import write_canonical_env
from imbue.minds.desktop_client.backup_status import BackupStatusState
from imbue.minds.desktop_client.backup_status import compute_backup_status_for_workspace
from imbue.minds.desktop_client.backup_status import compute_backup_status_for_workspaces
from imbue.minds.desktop_client.restic_cli import _get_restic_binary
from imbue.mngr.primitives import AgentId


def _paths(tmp_path: Path) -> WorkspacePaths:
    return WorkspacePaths(data_dir=tmp_path)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- non-restic paths ---


def test_no_canonical_env_is_not_configured(tmp_path: Path) -> None:
    status = compute_backup_status_for_workspace(_paths(tmp_path), AgentId.generate(), now=_now())
    assert status.state is BackupStatusState.NOT_CONFIGURED


def test_malformed_env_without_repository_is_unknown(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    agent_id = AgentId.generate()
    write_canonical_env(paths, agent_id, "RESTIC_PASSWORD=p\n")
    status = compute_backup_status_for_workspace(paths, agent_id, now=_now())
    assert status.state is BackupStatusState.UNKNOWN


def test_compute_for_no_workspaces_is_empty(tmp_path: Path) -> None:
    assert compute_backup_status_for_workspaces(_paths(tmp_path), []) == {}


def test_compute_for_workspaces_includes_not_configured(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    agent_id = AgentId.generate()
    result = compute_backup_status_for_workspaces(paths, [agent_id])
    assert result[str(agent_id)].state is BackupStatusState.NOT_CONFIGURED


# --- local restic integration ---


def _backup_a_file(repo: str, password: str, source: Path) -> None:
    env = dict(os.environ)
    env.update({"RESTIC_REPOSITORY": repo, "RESTIC_PASSWORD": password})
    result = subprocess.run(
        [_get_restic_binary(), "backup", str(source)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=120.0,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.timeout(60)
def test_status_never_then_backed_up_against_local_repo(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    agent_id = AgentId.generate()
    repo = str(tmp_path / "repo")
    password = "workspace-key"
    restic_cli.init_repo(repository=repo, backend_env={}, password=password)
    write_canonical_env(paths, agent_id, f"RESTIC_REPOSITORY={repo}\nRESTIC_PASSWORD={password}\n")

    # No snapshots yet -> NEVER.
    before = compute_backup_status_for_workspace(paths, agent_id, now=_now())
    assert before.state is BackupStatusState.NEVER
    assert before.last_success_at is None

    # After a successful backup -> BACKED_UP with a timestamp.
    source = tmp_path / "data.txt"
    source.write_text("hello")
    _backup_a_file(repo, password, source)
    after = compute_backup_status_for_workspace(paths, agent_id, now=_now())
    assert after.state is BackupStatusState.BACKED_UP
    assert after.last_success_at is not None
