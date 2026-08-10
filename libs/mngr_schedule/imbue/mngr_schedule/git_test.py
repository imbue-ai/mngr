"""Unit tests for git utilities."""

from pathlib import Path

import pytest

from imbue.mngr.utils.testing import run_git_command
from imbue.mngr_schedule.errors import ScheduleDeployError
from imbue.mngr_schedule.git import ensure_current_branch_is_pushed
from imbue.mngr_schedule.git import get_current_mngr_git_hash
from imbue.mngr_schedule.git import resolve_current_branch_name
from imbue.mngr_schedule.git import resolve_git_ref


def _detach_head(repo_path: Path) -> None:
    """Detach HEAD in the given repo by checking out the HEAD commit SHA."""
    head_sha = run_git_command(repo_path, "rev-parse", "HEAD").stdout.strip()
    run_git_command(repo_path, "checkout", head_sha)


def test_resolve_git_ref_resolves_head(temp_git_repo: Path) -> None:
    """resolve_git_ref should resolve HEAD to a full SHA."""
    result = resolve_git_ref("HEAD", cwd=temp_git_repo)
    assert len(result) == 40
    assert all(c in "0123456789abcdef" for c in result)


def test_resolve_git_ref_raises_for_invalid_ref(temp_git_repo: Path) -> None:
    """resolve_git_ref should raise ScheduleDeployError for invalid refs."""
    with pytest.raises(ScheduleDeployError, match="Could not resolve git ref"):
        resolve_git_ref("nonexistent-ref-xyz", cwd=temp_git_repo)


def test_ensure_branch_is_pushed_succeeds_when_pushed(temp_git_repo_with_remote: Path) -> None:
    """ensure_current_branch_is_pushed should succeed when the branch is up to date."""
    ensure_current_branch_is_pushed(cwd=temp_git_repo_with_remote)


def test_ensure_branch_is_pushed_fails_when_ahead(temp_git_repo_with_remote: Path) -> None:
    """ensure_current_branch_is_pushed should raise when there are unpushed commits."""
    # Make a new commit without pushing
    (temp_git_repo_with_remote / "new_file.txt").write_text("new content")
    run_git_command(temp_git_repo_with_remote, "add", ".")
    run_git_command(temp_git_repo_with_remote, "commit", "-m", "unpushed")

    with pytest.raises(ScheduleDeployError, match="unpushed commit"):
        ensure_current_branch_is_pushed(cwd=temp_git_repo_with_remote)


def test_ensure_branch_is_pushed_fails_without_upstream(temp_git_repo: Path) -> None:
    """ensure_current_branch_is_pushed should raise when there is no upstream tracking branch."""
    with pytest.raises(ScheduleDeployError, match="no remote tracking branch"):
        ensure_current_branch_is_pushed(cwd=temp_git_repo)


def test_ensure_branch_is_pushed_fails_on_detached_head(temp_git_repo: Path) -> None:
    """ensure_current_branch_is_pushed should raise on a detached HEAD."""
    _detach_head(temp_git_repo)

    with pytest.raises(ScheduleDeployError, match="detached HEAD"):
        ensure_current_branch_is_pushed(cwd=temp_git_repo)


def test_resolve_current_branch_name_succeeds(temp_git_repo: Path) -> None:
    """resolve_current_branch_name should return the current branch name."""
    result = resolve_current_branch_name(cwd=temp_git_repo)
    assert result == "main"


def test_resolve_current_branch_name_raises_on_detached_head(temp_git_repo: Path) -> None:
    """resolve_current_branch_name should raise on a detached HEAD."""
    _detach_head(temp_git_repo)
    with pytest.raises(ScheduleDeployError, match="detached HEAD"):
        resolve_current_branch_name(cwd=temp_git_repo)


def test_resolve_current_branch_name_raises_outside_git_repo(tmp_path: Path, setup_git_config: None) -> None:
    """resolve_current_branch_name should raise outside a git repo."""
    with pytest.raises(ScheduleDeployError, match="Could not determine current branch"):
        resolve_current_branch_name(cwd=tmp_path)


def test_get_current_mngr_git_hash_returns_hash(temp_git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """get_current_mngr_git_hash should return a hash in a git repo."""
    monkeypatch.chdir(temp_git_repo)
    result = get_current_mngr_git_hash()
    assert result != "unknown"
    assert len(result) == 40


def test_get_current_mngr_git_hash_returns_unknown_outside_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setup_git_config: None
) -> None:
    """get_current_mngr_git_hash should return 'unknown' outside a git repo."""
    monkeypatch.chdir(tmp_path)
    result = get_current_mngr_git_hash()
    assert result == "unknown"
