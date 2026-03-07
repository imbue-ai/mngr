"""Unit tests for git utilities."""

import subprocess
from pathlib import Path

import pytest

from imbue.mng_schedule.errors import ScheduleDeployError
from imbue.mng_schedule.git import ensure_current_branch_is_pushed
from imbue.mng_schedule.git import get_current_mng_git_hash
from imbue.mng_schedule.git import resolve_current_branch_name
from imbue.mng_schedule.git import resolve_git_ref


def _init_git_repo(path: Path) -> None:
    """Initialize a git repo with an initial commit.

    Explicitly sets init.defaultBranch to 'master' so the test is
    deterministic regardless of the system git configuration.
    """
    subprocess.run(["git", "init", "--initial-branch=master", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@test.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True, capture_output=True)
    (path / "README.md").write_text("init")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], check=True, capture_output=True)


def _init_repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """Create a git repo with a bare remote and push to it.

    Returns (repo_path, remote_path).
    """
    remote_path = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote_path)], check=True, capture_output=True)

    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)
    subprocess.run(
        ["git", "-C", str(repo_path), "remote", "add", "origin", str(remote_path)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_path), "push", "-u", "origin", "master"],
        check=True,
        capture_output=True,
    )
    return repo_path, remote_path


def test_resolve_git_ref_resolves_head(tmp_path: Path) -> None:
    """resolve_git_ref should resolve HEAD to a full SHA."""
    _init_git_repo(tmp_path)
    result = resolve_git_ref("HEAD", cwd=tmp_path)
    assert len(result) == 40
    assert all(c in "0123456789abcdef" for c in result)


def test_resolve_git_ref_raises_for_invalid_ref(tmp_path: Path) -> None:
    """resolve_git_ref should raise ScheduleDeployError for invalid refs."""
    _init_git_repo(tmp_path)
    with pytest.raises(ScheduleDeployError, match="Could not resolve git ref"):
        resolve_git_ref("nonexistent-ref-xyz", cwd=tmp_path)


def test_ensure_branch_is_pushed_succeeds_when_pushed(tmp_path: Path) -> None:
    """ensure_current_branch_is_pushed should succeed when the branch is up to date."""
    repo_path, _ = _init_repo_with_remote(tmp_path)
    ensure_current_branch_is_pushed(cwd=repo_path)


def test_ensure_branch_is_pushed_fails_when_ahead(tmp_path: Path) -> None:
    """ensure_current_branch_is_pushed should raise when there are unpushed commits."""
    repo_path, _ = _init_repo_with_remote(tmp_path)

    # Make a new commit without pushing
    (repo_path / "new_file.txt").write_text("new content")
    subprocess.run(["git", "-C", str(repo_path), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_path), "commit", "-m", "unpushed"], check=True, capture_output=True)

    with pytest.raises(ScheduleDeployError, match="unpushed commit"):
        ensure_current_branch_is_pushed(cwd=repo_path)


def test_ensure_branch_is_pushed_fails_without_upstream(tmp_path: Path) -> None:
    """ensure_current_branch_is_pushed should raise when there is no upstream tracking branch."""
    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)

    with pytest.raises(ScheduleDeployError, match="no remote tracking branch"):
        ensure_current_branch_is_pushed(cwd=repo_path)


def test_ensure_branch_is_pushed_fails_on_detached_head(tmp_path: Path) -> None:
    """ensure_current_branch_is_pushed should raise on a detached HEAD."""
    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)

    # Detach HEAD
    head_sha = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(repo_path), "checkout", head_sha], check=True, capture_output=True)

    with pytest.raises(ScheduleDeployError, match="detached HEAD"):
        ensure_current_branch_is_pushed(cwd=repo_path)


def test_resolve_current_branch_name_succeeds(tmp_path: Path) -> None:
    """resolve_current_branch_name should return the current branch name."""
    _init_git_repo(tmp_path)
    result = resolve_current_branch_name(cwd=tmp_path)
    assert result == "master"


def test_resolve_current_branch_name_raises_on_detached_head(tmp_path: Path) -> None:
    """resolve_current_branch_name should raise on a detached HEAD."""
    _init_git_repo(tmp_path)
    head_sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(tmp_path), "checkout", head_sha], check=True, capture_output=True)
    with pytest.raises(ScheduleDeployError, match="detached HEAD"):
        resolve_current_branch_name(cwd=tmp_path)


def test_resolve_current_branch_name_raises_outside_git_repo(tmp_path: Path) -> None:
    """resolve_current_branch_name should raise outside a git repo."""
    with pytest.raises(ScheduleDeployError, match="Could not determine current branch"):
        resolve_current_branch_name(cwd=tmp_path)


def test_get_current_mng_git_hash_returns_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """get_current_mng_git_hash should return a hash in a git repo."""
    _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = get_current_mng_git_hash()
    assert result != "unknown"
    assert len(result) == 40


def test_get_current_mng_git_hash_returns_unknown_outside_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_current_mng_git_hash should return 'unknown' outside a git repo."""
    monkeypatch.chdir(tmp_path)
    result = get_current_mng_git_hash()
    assert result == "unknown"
