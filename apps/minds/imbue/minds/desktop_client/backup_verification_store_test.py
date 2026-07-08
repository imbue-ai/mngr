"""Unit tests for the per-workspace backup verification flag store."""

from pathlib import Path

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.backup_verification_store import is_backup_verification_enabled
from imbue.minds.desktop_client.backup_verification_store import set_backup_verification_enabled
from imbue.mngr.primitives import AgentId


def _paths(tmp_path: Path) -> WorkspacePaths:
    return WorkspacePaths(data_dir=tmp_path)


def test_verification_is_enabled_by_default(tmp_path: Path) -> None:
    assert is_backup_verification_enabled(_paths(tmp_path), AgentId.generate()) is True


def test_disable_then_enable_round_trips(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    agent_id = AgentId.generate()
    set_backup_verification_enabled(paths, agent_id, False)
    assert is_backup_verification_enabled(paths, agent_id) is False
    # Other workspaces are unaffected.
    assert is_backup_verification_enabled(paths, AgentId.generate()) is True
    set_backup_verification_enabled(paths, agent_id, True)
    assert is_backup_verification_enabled(paths, agent_id) is True


def test_enable_when_already_enabled_is_a_noop(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    agent_id = AgentId.generate()
    set_backup_verification_enabled(paths, agent_id, True)
    assert is_backup_verification_enabled(paths, agent_id) is True
