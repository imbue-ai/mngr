"""Unit tests for the shared restic backup master-password store."""

import stat
from pathlib import Path

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.backup_password_store import backup_password_file_path
from imbue.minds.desktop_client.backup_password_store import has_saved_backup_password
from imbue.minds.desktop_client.backup_password_store import read_saved_backup_password
from imbue.minds.desktop_client.backup_password_store import save_backup_password_if_absent


def _paths(tmp_path: Path) -> WorkspacePaths:
    return WorkspacePaths(data_dir=tmp_path)


def test_read_returns_none_when_absent(tmp_path: Path) -> None:
    assert read_saved_backup_password(_paths(tmp_path)) is None
    assert has_saved_backup_password(_paths(tmp_path)) is False


def test_save_then_read_round_trips(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    assert save_backup_password_if_absent(paths, "hunter2") is True
    assert read_saved_backup_password(paths) == "hunter2"
    assert has_saved_backup_password(paths) is True


def test_save_is_first_time_only(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    assert save_backup_password_if_absent(paths, "first") is True
    # A second save must not overwrite the established password.
    assert save_backup_password_if_absent(paths, "second") is False
    assert read_saved_backup_password(paths) == "first"


def test_saved_file_is_owner_only(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    save_backup_password_if_absent(paths, "secret")
    mode = stat.S_IMODE(backup_password_file_path(paths).stat().st_mode)
    assert mode == 0o600


def test_read_treats_blank_file_as_unset(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    backup_password_file_path(paths).write_text("   \n")
    assert read_saved_backup_password(paths) is None
    assert has_saved_backup_password(paths) is False


def test_file_lives_under_data_dir(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    assert backup_password_file_path(paths) == tmp_path / "backup_password"
