"""Unit + local-restic integration tests for backup snapshot-state reads.

restic is a required dependency of the minds app (installed in the test
images), so the integration test runs unconditionally and FAILs -- not
skips -- if the ``restic`` binary is missing.
"""

from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client import restic_cli
from imbue.minds.desktop_client.backup_env_store import write_canonical_env
from imbue.minds.desktop_client.backup_status import is_workspace_backing_up
from imbue.minds.desktop_client.backup_status import list_workspace_snapshots
from imbue.minds.desktop_client.testing import restic_backup_a_file
from imbue.minds.errors import BackupProvisioningError
from imbue.mngr.primitives import AgentId


def _paths(tmp_path: Path) -> InstallationPaths:
    return InstallationPaths(data_dir=tmp_path)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- non-restic paths ---


def test_list_workspace_snapshots_raises_without_canonical_env(tmp_path: Path) -> None:
    with pytest.raises(BackupProvisioningError):
        list_workspace_snapshots(_paths(tmp_path), AgentId.generate())


def test_list_workspace_snapshots_raises_without_repository(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    agent_id = AgentId.generate()
    write_canonical_env(paths, agent_id, "RESTIC_PASSWORD=p\n")
    with pytest.raises(BackupProvisioningError):
        list_workspace_snapshots(paths, agent_id)


def test_is_workspace_backing_up_is_false_without_canonical_env(tmp_path: Path) -> None:
    assert is_workspace_backing_up(_paths(tmp_path), AgentId.generate(), now=_now()) is False


# --- local restic integration ---


@pytest.mark.timeout(60)
def test_snapshots_empty_then_populated_against_local_repo(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    agent_id = AgentId.generate()
    repo = str(tmp_path / "repo")
    password = "workspace-key"
    restic_cli.init_repo(repository=repo, backend_env={}, password=password)
    write_canonical_env(paths, agent_id, f"RESTIC_REPOSITORY={repo}\nRESTIC_PASSWORD={password}\n")

    # No snapshots yet.
    assert list_workspace_snapshots(paths, agent_id) == ()

    # A configured repo with no backup running -> not backing up.
    assert is_workspace_backing_up(paths, agent_id, now=_now()) is False

    # After a successful backup, the snapshot shows up with a real timestamp.
    source = tmp_path / "data.txt"
    source.write_text("hello")
    restic_backup_a_file(repo, password, source)
    snapshots = list_workspace_snapshots(paths, agent_id)
    assert len(snapshots) == 1
    assert snapshots[0].time.tzinfo is not None


def _install_slow_restic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, sleep_seconds: float) -> None:
    """Point the app at a restic stand-in that stalls before answering."""
    stub = tmp_path / "slow-restic"
    stub.write_text(f"#!/bin/sh\nsleep {sleep_seconds}\necho '[]'\n")
    stub.chmod(0o755)
    monkeypatch.setenv("MINDS_RESTIC_BINARY", str(stub))


@pytest.mark.timeout(60)
def test_snapshot_listing_honors_the_budget_it_is_given(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A listing slower than its budget fails naming the budget; a larger budget lets it through.

    The backup history page is served with a budget of its own precisely
    because a cold listing outlives the sidebar's status budget.
    """
    paths = _paths(tmp_path)
    agent_id = AgentId.generate()
    write_canonical_env(paths, agent_id, "RESTIC_REPOSITORY=/unused\nRESTIC_PASSWORD=p\n")
    _install_slow_restic(tmp_path, monkeypatch, sleep_seconds=2.0)

    with pytest.raises(restic_cli.ResticTimeoutError) as exc_info:
        list_workspace_snapshots(paths, agent_id, timeout_seconds=0.5)
    assert "timed out after 0.5s" in str(exc_info.value)

    assert list_workspace_snapshots(paths, agent_id, timeout_seconds=30.0) == ()
