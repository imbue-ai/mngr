"""Unit tests for the per-env data-root path helpers."""

from pathlib import Path

import pytest

from imbue.minds.envs.paths import list_env_root_dirs
from imbue.minds.errors import MindError


def test_list_env_root_dirs_lists_minds_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # production root + two dev roots, plus a decoy that must be ignored.
    (tmp_path / ".minds").mkdir()
    (tmp_path / ".minds-dev-josh").mkdir()
    (tmp_path / ".minds-staging").mkdir()
    (tmp_path / ".minds-backup-2024-01-01").mkdir()  # illegal env name -> excluded
    (tmp_path / ".unrelated").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))

    roots = list_env_root_dirs()
    names = [p.name for p in roots]
    # production (".minds") sorts first; the backup/unrelated dirs are excluded.
    assert names == [".minds", ".minds-dev-josh", ".minds-staging"]


def test_list_env_root_dirs_raises_when_home_is_not_a_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A broken $HOME is not a legitimate "zero envs" state -- surface it
    # rather than silently returning ().
    monkeypatch.setenv("HOME", str(tmp_path / "does-not-exist"))
    with pytest.raises(MindError, match="not a directory"):
        list_env_root_dirs()
