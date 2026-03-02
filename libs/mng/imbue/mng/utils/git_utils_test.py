"""Tests for git utilities."""

import subprocess
from pathlib import Path

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mng.utils.git_utils import _parse_project_name_from_url
from imbue.mng.utils.git_utils import derive_project_name_from_path
from imbue.mng.utils.git_utils import find_git_common_dir
from imbue.mng.utils.git_utils import find_git_worktree_root
from imbue.mng.utils.git_utils import get_git_author_info
from imbue.mng.utils.git_utils import get_git_remote_url
from imbue.mng.utils.git_utils import is_git_repository


def test_github_https_url() -> None:
    """Test parsing a GitHub HTTPS URL."""
    url = "https://github.com/owner/my-project.git"
    assert _parse_project_name_from_url(url) == "my-project"


def test_github_https_url_without_git_suffix() -> None:
    """Test parsing a GitHub HTTPS URL without .git suffix."""
    url = "https://github.com/owner/my-project"
    assert _parse_project_name_from_url(url) == "my-project"


def test_github_ssh_url() -> None:
    """Test parsing a GitHub SSH URL."""
    url = "git@github.com:owner/my-project.git"
    assert _parse_project_name_from_url(url) == "my-project"


def test_github_ssh_url_without_git_suffix() -> None:
    """Test parsing a GitHub SSH URL without .git suffix."""
    url = "git@github.com:owner/my-project"
    assert _parse_project_name_from_url(url) == "my-project"


def test_gitlab_https_url() -> None:
    """Test parsing a GitLab HTTPS URL."""
    url = "https://gitlab.com/owner/my-project.git"
    assert _parse_project_name_from_url(url) == "my-project"


def test_gitlab_ssh_url() -> None:
    """Test parsing a GitLab SSH URL."""
    url = "git@gitlab.com:owner/my-project.git"
    assert _parse_project_name_from_url(url) == "my-project"


def test_nested_project_path() -> None:
    """Test parsing a URL with nested project path."""
    url = "https://github.com/org/group/subgroup/my-project.git"
    assert _parse_project_name_from_url(url) == "my-project"


def test_invalid_url() -> None:
    """Test parsing an invalid URL returns None."""
    url = "not-a-valid-url"
    assert _parse_project_name_from_url(url) is None


def test_empty_url() -> None:
    """Test parsing an empty URL returns None."""
    url = ""
    assert _parse_project_name_from_url(url) is None


def test_derive_from_folder_name_when_no_git(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test deriving project name from folder name when there's no git repo."""
    project_dir = tmp_path / "my-project"
    project_dir.mkdir()

    assert derive_project_name_from_path(project_dir, cg) == "my-project"


def test_derive_from_folder_name_when_git_has_no_remote(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test deriving project name from folder name when git has no remote."""
    project_dir = tmp_path / "my-project"
    project_dir.mkdir()

    # Initialize git but don't add a remote
    subprocess.run(["git", "init"], cwd=project_dir, check=True, capture_output=True)

    assert derive_project_name_from_path(project_dir, cg) == "my-project"


def test_derive_from_git_remote_github(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test deriving project name from GitHub git remote."""
    project_dir = tmp_path / "local-folder"
    project_dir.mkdir()

    # Initialize git and add a GitHub remote
    subprocess.run(["git", "init"], cwd=project_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/owner/remote-project.git"],
        cwd=project_dir,
        check=True,
        capture_output=True,
    )

    # Should use the remote project name, not the folder name
    assert derive_project_name_from_path(project_dir, cg) == "remote-project"


def test_derive_from_git_remote_ssh(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test deriving project name from SSH git remote."""
    project_dir = tmp_path / "local-folder"
    project_dir.mkdir()

    # Initialize git and add an SSH remote
    subprocess.run(["git", "init"], cwd=project_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:owner/remote-project.git"],
        cwd=project_dir,
        check=True,
        capture_output=True,
    )

    # Should use the remote project name, not the folder name
    assert derive_project_name_from_path(project_dir, cg) == "remote-project"


def test_is_git_repository_returns_false_for_nonexistent_path(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that is_git_repository returns False for a non-existent path."""
    nonexistent = tmp_path / "does_not_exist"
    assert is_git_repository(nonexistent, cg) is False


def test_is_git_repository_returns_false_for_non_git_dir(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that is_git_repository returns False for a non-git directory."""
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    assert is_git_repository(plain_dir, cg) is False


def test_is_git_repository_returns_true_for_git_dir(
    cg: ConcurrencyGroup, tmp_path: Path, setup_git_config: None
) -> None:
    """Test that is_git_repository returns True for a git directory."""
    git_dir = tmp_path / "repo"
    git_dir.mkdir()
    subprocess.run(["git", "init"], cwd=git_dir, check=True, capture_output=True)
    assert is_git_repository(git_dir, cg) is True


def test_find_git_worktree_root_returns_none_when_not_in_git(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that find_git_worktree_root returns None when not in a git repo."""
    non_git_dir = tmp_path / "not-a-repo"
    non_git_dir.mkdir()

    result = find_git_worktree_root(non_git_dir, cg)
    assert result is None


def test_find_git_worktree_root_returns_root_when_in_git(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that find_git_worktree_root returns the root when in a git repo."""
    git_dir = tmp_path / "my-repo"
    git_dir.mkdir()
    subprocess.run(["git", "init"], cwd=git_dir, check=True, capture_output=True)

    subdir = git_dir / "some" / "nested" / "path"
    subdir.mkdir(parents=True)

    result = find_git_worktree_root(subdir, cg)
    assert result == git_dir


def test_find_git_common_dir_returns_none_when_not_in_git(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that find_git_common_dir returns None when not in a git repo."""
    non_git_dir = tmp_path / "not-a-repo"
    non_git_dir.mkdir()

    result = find_git_common_dir(non_git_dir, cg)
    assert result is None


def test_find_git_common_dir_returns_git_dir_for_regular_repo(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that find_git_common_dir returns .git for a regular repository."""
    git_dir = tmp_path / "my-repo"
    git_dir.mkdir()
    subprocess.run(["git", "init"], cwd=git_dir, check=True, capture_output=True)

    result = find_git_common_dir(git_dir, cg)
    assert result is not None
    assert result == git_dir / ".git"


def test_find_git_common_dir_returns_main_git_from_worktree(
    cg: ConcurrencyGroup, tmp_path: Path, temp_git_repo: Path
) -> None:
    """Test that find_git_common_dir returns main repo's .git from a worktree."""
    worktree_path = tmp_path / "worktree"
    subprocess.run(
        ["git", "worktree", "add", str(worktree_path), "-b", "test-branch"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
    )

    result = find_git_common_dir(worktree_path, cg)
    assert result is not None
    assert result == temp_git_repo / ".git"


def test_find_git_common_dir_from_subdirectory(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that find_git_common_dir works from a subdirectory."""
    git_dir = tmp_path / "my-repo"
    git_dir.mkdir()
    subprocess.run(["git", "init"], cwd=git_dir, check=True, capture_output=True)

    subdir = git_dir / "some" / "nested" / "path"
    subdir.mkdir(parents=True)

    result = find_git_common_dir(subdir, cg)
    assert result is not None
    assert result == git_dir / ".git"


def test_get_git_author_info_returns_configured_values(temp_git_repo: Path, cg: ConcurrencyGroup) -> None:
    """Test that get_git_author_info returns name and email from a configured repo."""
    name, email = get_git_author_info(temp_git_repo, cg)
    assert name == "Test User"
    assert email == "test@test.com"


def test_get_git_author_info_returns_none_when_not_configured(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that get_git_author_info returns (None, None) for a repo without author config."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    # HOME is already set to tmp_path by autouse fixture, and no .gitconfig exists
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    name, email = get_git_author_info(repo_dir, cg)
    assert name is None
    assert email is None


def test_get_git_author_info_returns_none_for_non_git_dir(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that get_git_author_info returns (None, None) for a non-git directory."""
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    name, email = get_git_author_info(plain_dir, cg)
    assert name is None
    assert email is None


def test_get_git_remote_url_returns_url_when_remote_exists(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that get_git_remote_url returns the URL when the remote exists."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/owner/repo.git"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    assert get_git_remote_url(repo, "origin", cg) == "https://github.com/owner/repo.git"


def test_get_git_remote_url_returns_none_when_remote_missing(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that get_git_remote_url returns None when the remote does not exist."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    assert get_git_remote_url(repo, "origin", cg) is None


def test_get_git_remote_url_returns_none_for_non_git_dir(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that get_git_remote_url returns None for a non-git directory."""
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    assert get_git_remote_url(plain_dir, "origin", cg) is None
