"""Tests for git utilities."""

import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import UserInputError
from imbue.mngr.utils.git_utils import GIT_MIRROR_PUSH_REFSPECS
from imbue.mngr.utils.git_utils import build_project_filter_clause
from imbue.mngr.utils.git_utils import clone_git_url_to_managed_dir
from imbue.mngr.utils.git_utils import delete_git_branch
from imbue.mngr.utils.git_utils import derive_project_name_for_source
from imbue.mngr.utils.git_utils import derive_project_name_from_path
from imbue.mngr.utils.git_utils import find_git_common_dir
from imbue.mngr.utils.git_utils import find_git_source_path
from imbue.mngr.utils.git_utils import find_git_worktree_root
from imbue.mngr.utils.git_utils import find_source_repo_of_worktree
from imbue.mngr.utils.git_utils import get_current_branch
from imbue.mngr.utils.git_utils import get_head_commit
from imbue.mngr.utils.git_utils import is_git_repository
from imbue.mngr.utils.git_utils import is_git_url
from imbue.mngr.utils.git_utils import parse_project_name_from_url
from imbue.mngr.utils.git_utils import parse_worktree_git_file
from imbue.mngr.utils.git_utils import resolve_project_filter_values
from imbue.mngr.utils.git_utils import rsync_worktree_over_clone
from imbue.mngr.utils.testing import write_executable_script


def test_github_https_url() -> None:
    """Test parsing a GitHub HTTPS URL."""
    url = "https://github.com/owner/my-project.git"
    assert parse_project_name_from_url(url) == "my-project"


def test_github_https_url_without_git_suffix() -> None:
    """Test parsing a GitHub HTTPS URL without .git suffix."""
    url = "https://github.com/owner/my-project"
    assert parse_project_name_from_url(url) == "my-project"


def test_github_ssh_url() -> None:
    """Test parsing a GitHub SSH URL."""
    url = "git@github.com:owner/my-project.git"
    assert parse_project_name_from_url(url) == "my-project"


def test_github_ssh_url_without_git_suffix() -> None:
    """Test parsing a GitHub SSH URL without .git suffix."""
    url = "git@github.com:owner/my-project"
    assert parse_project_name_from_url(url) == "my-project"


def test_gitlab_https_url() -> None:
    """Test parsing a GitLab HTTPS URL."""
    url = "https://gitlab.com/owner/my-project.git"
    assert parse_project_name_from_url(url) == "my-project"


def test_gitlab_ssh_url() -> None:
    """Test parsing a GitLab SSH URL."""
    url = "git@gitlab.com:owner/my-project.git"
    assert parse_project_name_from_url(url) == "my-project"


def test_nested_project_path() -> None:
    """Test parsing a URL with nested project path."""
    url = "https://github.com/org/group/subgroup/my-project.git"
    assert parse_project_name_from_url(url) == "my-project"


def test_invalid_url() -> None:
    """Test parsing an invalid URL returns None."""
    url = "not-a-valid-url"
    assert parse_project_name_from_url(url) is None


def test_empty_url() -> None:
    """Test parsing an empty URL returns None."""
    url = ""
    assert parse_project_name_from_url(url) is None


def test_resolve_project_filter_values_passes_through_non_dot_values(cg: ConcurrencyGroup) -> None:
    """Non-dot values are returned unchanged."""
    assert resolve_project_filter_values(("foo", "bar"), cg) == ("foo", "bar")


def test_resolve_project_filter_values_expands_dot_to_current_project(
    tmp_path: Path, cg: ConcurrencyGroup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The literal '.' is expanded to the current project name (derived from cwd)."""
    project_dir = tmp_path / "my-project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)

    assert resolve_project_filter_values((".",), cg) == ("my-project",)
    assert resolve_project_filter_values((".", "other"), cg) == ("my-project", "other")


def test_resolve_project_filter_values_handles_empty(cg: ConcurrencyGroup) -> None:
    """Empty input returns empty output without resolving the project."""
    assert resolve_project_filter_values((), cg) == ()


def test_resolve_project_filter_values_dedupes_preserving_order(
    tmp_path: Path, cg: ConcurrencyGroup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Duplicate values (including duplicate '.' expansions) collapse, in insertion order."""
    project_dir = tmp_path / "my-project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)

    # Plain duplicates collapse.
    assert resolve_project_filter_values(("foo", "foo"), cg) == ("foo",)
    # Duplicate '.' expansions collapse to a single project name.
    assert resolve_project_filter_values((".", "."), cg) == ("my-project",)
    # Mixed duplicates collapse and preserve relative insertion order of distinct values.
    assert resolve_project_filter_values(("foo", ".", "foo", "."), cg) == ("foo", "my-project")


def test_resolve_project_filter_values_uses_project_root_over_cwd(
    tmp_path: Path, cg: ConcurrencyGroup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When project_root is provided, '.' resolves from there, not the (possibly nested) cwd."""
    project_dir = tmp_path / "my-project"
    project_dir.mkdir()
    nested = project_dir / "nested" / "subdir"
    nested.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=project_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/owner/remote-project.git"],
        cwd=project_dir,
        check=True,
        capture_output=True,
    )
    monkeypatch.chdir(nested)

    # Without project_root, the cwd-based derivation cannot find the remote and falls back to the
    # subdir name -- a wrong answer for "the current project". With project_root pointing at the
    # worktree root, the remote is found and the correct project name is returned.
    assert resolve_project_filter_values((".",), cg) == ("subdir",)
    assert resolve_project_filter_values((".",), cg, project_root=project_dir) == ("remote-project",)


def test_build_project_filter_clause_returns_none_for_empty(cg: ConcurrencyGroup) -> None:
    """Empty input returns None so callers can skip appending a filter."""
    assert build_project_filter_clause((), cg) is None


def test_build_project_filter_clause_single_value(cg: ConcurrencyGroup) -> None:
    """A single project name produces a single CEL equality clause."""
    assert build_project_filter_clause(("foo",), cg) == 'labels.project == "foo"'


def test_build_project_filter_clause_multiple_values_or_joined(cg: ConcurrencyGroup) -> None:
    """Multiple project names are OR-joined in a single clause."""
    assert build_project_filter_clause(("foo", "bar"), cg) == 'labels.project == "foo" || labels.project == "bar"'


def test_build_project_filter_clause_expands_dot(
    tmp_path: Path, cg: ConcurrencyGroup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The literal '.' is expanded to the cwd-derived project name in the resulting clause."""
    project_dir = tmp_path / "my-project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)

    assert build_project_filter_clause((".",), cg) == 'labels.project == "my-project"'


def test_derive_project_name_for_source_prefers_label(tmp_path: Path) -> None:
    """source_project_label wins over remote_url and path."""
    project_dir = tmp_path / "path-name"
    project_dir.mkdir()

    result = derive_project_name_for_source(
        project_dir,
        remote_url="https://github.com/owner/url-name.git",
        source_project_label="label-name",
    )

    assert result == "label-name"


def test_derive_project_name_for_source_uses_remote_url(tmp_path: Path) -> None:
    """remote_url is used when no source_project_label is given."""
    project_dir = tmp_path / "path-name"
    project_dir.mkdir()

    result = derive_project_name_for_source(
        project_dir,
        remote_url="https://github.com/owner/url-name.git",
    )

    assert result == "url-name"


def test_derive_project_name_for_source_falls_back_to_path(tmp_path: Path) -> None:
    """With no hints, falls back to the path's directory name."""
    project_dir = tmp_path / "path-name"
    project_dir.mkdir()

    result = derive_project_name_for_source(project_dir)

    assert result == "path-name"


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


def test_derive_from_source_repo_name_for_worktree_without_origin(
    cg: ConcurrencyGroup, tmp_path: Path, temp_git_repo: Path
) -> None:
    """Test that worktrees without an origin remote use the source repo's directory name."""
    worktree_path = tmp_path / "ugly-worktree-name-abc123"
    subprocess.run(
        ["git", "worktree", "add", str(worktree_path), "-b", "test-branch"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
    )

    # temp_git_repo has no origin remote, so should fall back to source repo dir name
    assert derive_project_name_from_path(worktree_path, cg) == temp_git_repo.name


def test_derive_from_origin_for_worktree_with_origin(
    cg: ConcurrencyGroup, tmp_path: Path, temp_git_repo: Path
) -> None:
    """Test that worktrees with an origin remote use the remote project name."""
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/owner/remote-project.git"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
    )

    worktree_path = tmp_path / "ugly-worktree-name-abc123"
    subprocess.run(
        ["git", "worktree", "add", str(worktree_path), "-b", "test-branch"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
    )

    # Should use origin's project name, not the worktree or source repo dir name
    assert derive_project_name_from_path(worktree_path, cg) == "remote-project"


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


def _install_failing_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a fake ``git`` on PATH that exits non-zero with an unrelated error.

    Returns a work directory to run in. Used to verify the repo-detection helpers
    raise on unexpected git failures rather than swallowing them into a
    misleading "not a git repository" sentinel. PATH is prepended (not replaced)
    so the fake git wins while other tools (e.g. tmux, used by autouse fixtures)
    stay resolvable.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    write_executable_script(fake_bin / "git", "#!/bin/bash\necho 'fatal: something went wrong' >&2\nexit 1\n")
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    return work_dir


def test_find_git_worktree_root_raises_on_unexpected_git_failure(
    tmp_path: Path, cg: ConcurrencyGroup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """find_git_worktree_root should raise, not return None, when git fails for a
    reason other than "not a git repository".

    Only a clean "not a git repository" result means "no worktree root"; any
    other failure (here, a git that exits non-zero with an unrelated error) must
    surface loudly rather than be swallowed into None, which would silently drop
    the project config layer with no explanation.
    """
    work_dir = _install_failing_git(tmp_path, monkeypatch)

    with pytest.raises(ProcessError):
        find_git_worktree_root(work_dir, cg)


def test_is_git_repository_raises_on_unexpected_git_failure(
    tmp_path: Path, cg: ConcurrencyGroup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """is_git_repository should raise on an unexpected git failure rather than
    swallowing it into a misleading False."""
    work_dir = _install_failing_git(tmp_path, monkeypatch)

    with pytest.raises(ProcessError):
        is_git_repository(work_dir, cg)


def test_find_git_common_dir_raises_on_unexpected_git_failure(
    tmp_path: Path, cg: ConcurrencyGroup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """find_git_common_dir should raise on an unexpected git failure rather than
    swallowing it into a misleading None."""
    work_dir = _install_failing_git(tmp_path, monkeypatch)

    with pytest.raises(ProcessError):
        find_git_common_dir(work_dir, cg)


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


def test_find_git_source_path_returns_none_when_not_in_git(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """find_git_source_path returns None when the path is not inside a git repo."""
    non_git_dir = tmp_path / "not-a-repo"
    non_git_dir.mkdir()

    result = find_git_source_path(non_git_dir, cg)
    assert result is None


def test_find_git_source_path_returns_repo_root_for_regular_repo(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """find_git_source_path returns the repo root (parent of .git) for a regular repository."""
    git_dir = tmp_path / "my-repo"
    git_dir.mkdir()
    subprocess.run(["git", "init"], cwd=git_dir, check=True, capture_output=True)

    result = find_git_source_path(git_dir, cg)
    assert result == git_dir


def test_find_git_source_path_returns_main_repo_root_from_worktree(
    cg: ConcurrencyGroup, tmp_path: Path, temp_git_repo: Path
) -> None:
    """find_git_source_path returns the *main* repo root (not the worktree dir) from a worktree.

    This is the property both agent plugins rely on: a single trust grant on the
    source repo must cover every worktree of the same repo.
    """
    worktree_path = tmp_path / "worktree"
    subprocess.run(
        ["git", "worktree", "add", str(worktree_path), "-b", "test-branch"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
    )

    result = find_git_source_path(worktree_path, cg)
    assert result == temp_git_repo


# =============================================================================
# parse_worktree_git_file Tests
# =============================================================================


def test_parse_worktree_git_file_valid_gitdir() -> None:
    """parse_worktree_git_file should extract the source repo path from a valid gitdir line."""
    content = "gitdir: /home/user/myrepo/.git/worktrees/my-worktree"
    result = parse_worktree_git_file(content)
    assert result == Path("/home/user/myrepo")


def test_parse_worktree_git_file_with_trailing_whitespace() -> None:
    """parse_worktree_git_file should handle trailing whitespace."""
    content = "gitdir: /home/user/myrepo/.git/worktrees/my-worktree\n"
    result = parse_worktree_git_file(content)
    assert result == Path("/home/user/myrepo")


def test_parse_worktree_git_file_invalid_content() -> None:
    """parse_worktree_git_file should return None for content without gitdir prefix."""
    content = "not a valid gitdir line"
    result = parse_worktree_git_file(content)
    assert result is None


def test_parse_worktree_git_file_non_gitdir_path() -> None:
    """parse_worktree_git_file should return None when parent.parent is not .git."""
    # This is a gitdir line, but the path structure doesn't have .git as the grandparent
    content = "gitdir: /home/user/myrepo/.notgit/worktrees/my-worktree"
    result = parse_worktree_git_file(content)
    assert result is None


# =============================================================================
# find_source_repo_of_worktree Tests
# =============================================================================


def test_find_source_repo_of_worktree_returns_none_for_missing_git_file(tmp_path: Path) -> None:
    """find_source_repo_of_worktree should return None when .git file does not exist."""
    result = find_source_repo_of_worktree(tmp_path / "nonexistent")
    assert result is None


def test_find_source_repo_of_worktree_returns_none_for_directory_git(tmp_path: Path) -> None:
    """find_source_repo_of_worktree should return None when .git is a directory (regular repo)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    # .git is a directory, not a file, so read_text will raise
    result = find_source_repo_of_worktree(repo)
    assert result is None


def test_find_source_repo_of_worktree_returns_path_for_valid_worktree(
    cg: ConcurrencyGroup, tmp_path: Path, temp_git_repo: Path
) -> None:
    """find_source_repo_of_worktree should return the source repo from a real worktree."""
    worktree_path = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", str(worktree_path), "-b", "wt-branch"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
    )
    result = find_source_repo_of_worktree(worktree_path)
    assert result == temp_git_repo


# =============================================================================
# get_current_branch Tests
# =============================================================================


def test_get_current_branch_returns_branch_name(temp_git_repo: Path, cg: ConcurrencyGroup) -> None:
    """get_current_branch should return the exact name of the checked-out branch."""
    # Check out a deterministic, uniquely-named branch so we can assert the exact value
    # rather than just "some non-empty, non-HEAD string" (which would still pass if the
    # function returned a commit-ish or a remote ref).
    expected_branch = f"known-branch-{uuid4().hex}"
    subprocess.run(
        ["git", "checkout", "-b", expected_branch],
        cwd=temp_git_repo,
        capture_output=True,
        check=True,
    )

    branch = get_current_branch(temp_git_repo, cg)

    assert branch == expected_branch


def test_get_current_branch_raises_on_detached_head(temp_git_repo: Path, cg: ConcurrencyGroup) -> None:
    """get_current_branch should raise MngrError for detached HEAD."""
    # Get the commit hash, then detach HEAD
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=temp_git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    commit_hash = result.stdout.strip()
    subprocess.run(
        ["git", "checkout", commit_hash],
        cwd=temp_git_repo,
        capture_output=True,
        check=True,
    )

    with pytest.raises(MngrError, match="HEAD is detached"):
        get_current_branch(temp_git_repo, cg)


def test_get_current_branch_raises_for_non_git_dir(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """get_current_branch should raise MngrError for a non-git directory."""
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    with pytest.raises(MngrError, match="Failed to get current branch"):
        get_current_branch(plain_dir, cg)


# =============================================================================
# get_head_commit Tests
# =============================================================================


def test_get_head_commit_returns_commit_hash(temp_git_repo: Path, cg: ConcurrencyGroup) -> None:
    """get_head_commit should return the HEAD commit hash."""
    commit = get_head_commit(temp_git_repo, cg)
    assert commit is not None
    # SHA-1 hash is 40 hex characters
    assert len(commit) == 40
    assert all(c in "0123456789abcdef" for c in commit)


def test_get_head_commit_returns_none_for_non_git_dir(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """get_head_commit should return None for a non-git directory."""
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    result = get_head_commit(plain_dir, cg)
    assert result is None


# =============================================================================
# delete_git_branch Tests
# =============================================================================


def test_delete_git_branch_removes_branch(temp_git_repo: Path, cg: ConcurrencyGroup) -> None:
    """delete_git_branch returns True and removes a branch that exists."""
    branch_name = "feature/to-delete"
    subprocess.run(
        ["git", "-C", str(temp_git_repo), "branch", branch_name],
        capture_output=True,
        text=True,
        check=True,
    )

    assert delete_git_branch(branch_name, temp_git_repo, cg) is True

    list_result = subprocess.run(
        ["git", "-C", str(temp_git_repo), "for-each-ref", "--format=%(refname:short)", "refs/heads/"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert branch_name not in list_result.stdout.splitlines()


@pytest.mark.allow_warnings(match=r"Failed to delete branch does-not-exist")
def test_delete_git_branch_returns_false_for_missing_branch(temp_git_repo: Path, cg: ConcurrencyGroup) -> None:
    """delete_git_branch returns False (and does not raise) when the branch does not exist; a warning is emitted at the call site so the failure is visible."""
    assert delete_git_branch("does-not-exist", temp_git_repo, cg) is False


@pytest.mark.allow_warnings(match=r"Failed to delete branch any-branch")
def test_delete_git_branch_returns_false_for_non_git_dir(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """delete_git_branch returns False (and does not raise) when the path is not a git repo; a warning is emitted so the failure is visible."""
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    assert delete_git_branch("any-branch", plain_dir, cg) is False


# =============================================================================
# GIT_MIRROR_PUSH_REFSPECS Tests
# =============================================================================


def test_mirror_push_refspecs_do_not_push_remote_tracking_refs(temp_git_repo: Path, tmp_path: Path) -> None:
    """GIT_MIRROR_PUSH_REFSPECS must not push remote-tracking refs to the target.

    Pushing remote-tracking refs (refs/remotes/*) causes "inconsistent aliased
    update" errors on git 2.45+ when the source has symbolic refs like
    refs/remotes/origin/HEAD. GIT_MIRROR_PUSH_REFSPECS provides explicit
    refspecs for branches and tags only, ensuring remote-tracking refs are
    never pushed.
    """
    # Set up the source repo with remote-tracking refs including the symbolic
    # refs/remotes/origin/HEAD that triggers the bug over SSH.
    upstream = tmp_path / "upstream.git"
    subprocess.run(
        ["git", "clone", "--bare", str(temp_git_repo), str(upstream)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(upstream)],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "fetch", "origin"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
    )

    # Create a tag so we can verify tag refspecs work too
    subprocess.run(
        ["git", "tag", "v1.0.0"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
    )

    # Verify the source has remote-tracking refs (precondition for the test)
    ref_result = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", "refs/remotes/"],
        cwd=temp_git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "refs/remotes/origin/" in ref_result.stdout, "Source must have remote-tracking refs"

    # Determine the actual branch name (depends on system git config)
    source_branch_result = subprocess.run(
        ["git", "-C", str(temp_git_repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    source_branch = source_branch_result.stdout.strip()

    # Create a fresh bare target repo and push using GIT_MIRROR_PUSH_REFSPECS
    target = tmp_path / "target.git"
    subprocess.run(
        ["git", "init", "--bare", str(target)],
        check=True,
        capture_output=True,
    )
    push_result = subprocess.run(
        ["git", "-C", str(temp_git_repo), "push", "--force", "--prune", str(target), *GIT_MIRROR_PUSH_REFSPECS],
        capture_output=True,
        text=True,
    )
    assert push_result.returncode == 0, f"Push with GIT_MIRROR_PUSH_REFSPECS failed:\n{push_result.stderr}"

    # Verify branches were pushed
    branch_result = subprocess.run(
        ["git", "-C", str(target), "branch"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert source_branch in branch_result.stdout, (
        f"Branch '{source_branch}' should be pushed to the target, got: {branch_result.stdout}"
    )

    # Verify tags were pushed
    tag_result = subprocess.run(
        ["git", "-C", str(target), "tag"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "v1.0.0" in tag_result.stdout, f"Tag 'v1.0.0' should be pushed to the target, got: {tag_result.stdout}"

    # Verify NO remote-tracking refs were pushed -- this is the key assertion.
    # Without explicit refspecs, git push --mirror pushes refs/remotes/* to
    # the target, which causes "inconsistent aliased update" errors over SSH
    # on git 2.45+ due to symbolic refs like refs/remotes/origin/HEAD.
    target_refs = subprocess.run(
        ["git", "-C", str(target), "for-each-ref", "--format=%(refname)", "refs/remotes/"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert target_refs.stdout.strip() == "", (
        f"Remote-tracking refs should NOT be pushed to the target, but found:\n{target_refs.stdout}"
    )


# =============================================================================
# is_git_url Tests
# =============================================================================


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/owner/repo",
        "https://github.com/owner/repo.git",
        "http://example.com/owner/repo",
        "https://gitlab.com/group/subgroup/repo.git",
        "https://self-hosted.example.com:8080/team/repo.git",
        "git@github.com:owner/repo.git",
        "git@github.com:owner/repo",
        "git@gitlab.com:group/subgroup/repo.git",
        "ssh://git@github.com/owner/repo.git",
        "ssh://user@host.example.com:22/owner/repo",
        "git://github.com/owner/repo.git",
        "file:///tmp/local/repo.git",
    ],
)
def test_is_git_url_recognizes_git_urls(url: str) -> None:
    """is_git_url accepts all standard git URL shapes."""
    assert is_git_url(url) is True


@pytest.mark.parametrize(
    "value",
    [
        "",
        "foo",
        "my-agent",
        "my-agent@my-host",
        "my-agent@my-host.modal",
        "/abs/path",
        "./rel",
        "../rel",
        ":path",
        "@host:/path",
        "user@host:file",
    ],
)
def test_is_git_url_rejects_non_urls(value: str) -> None:
    """is_git_url returns False for agent addresses, paths, and empty strings."""
    assert is_git_url(value) is False


# =============================================================================
# clone_git_url_to_managed_dir Tests
# =============================================================================


def test_clone_git_url_to_managed_dir_clones_local_repo(
    cg: ConcurrencyGroup, tmp_path: Path, temp_git_repo: Path
) -> None:
    """clone_git_url_to_managed_dir produces a working clone at <base>/<name>-<hex>."""
    base_dir = tmp_path / "clones"
    dest = clone_git_url_to_managed_dir(str(temp_git_repo), base_dir, "my-agent", cg)

    assert dest.parent == base_dir
    assert dest.name.startswith("my-agent-")
    assert (dest / ".git").exists()


def test_clone_git_url_to_managed_dir_raises_on_invalid_url(
    cg: ConcurrencyGroup, tmp_path: Path, setup_git_config: None
) -> None:
    """clone_git_url_to_managed_dir raises UserInputError when git clone fails."""
    base_dir = tmp_path / "clones"
    with pytest.raises(UserInputError, match="Failed to clone"):
        clone_git_url_to_managed_dir(str(tmp_path / "does-not-exist"), base_dir, "agent", cg)

    # The failed-clone destination should not be left behind.
    if base_dir.exists():
        assert list(base_dir.iterdir()) == []


@pytest.mark.rsync
def test_rsync_worktree_over_clone_overlays_uncommitted_files(
    cg: ConcurrencyGroup, tmp_path: Path, temp_git_repo: Path
) -> None:
    """The overlay rsync delivers uncommitted worktree edits onto a clone of HEAD."""
    # Make an uncommitted edit in the worktree on top of HEAD content.
    (temp_git_repo / "tracked.txt").write_text("HEAD\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=temp_git_repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "x"],
        cwd=temp_git_repo,
        check=True,
    )
    (temp_git_repo / "tracked.txt").write_text("DIRTY\n")
    (temp_git_repo / "untracked.txt").write_text("hello\n")

    # Clone HEAD into a sibling dir (mirrors what mngr_vps does for a
    # worktree build context).
    clone_dir = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", f"file://{temp_git_repo}", str(clone_dir)],
        check=True,
    )
    assert (clone_dir / "tracked.txt").read_text() == "HEAD\n"
    assert not (clone_dir / "untracked.txt").exists()

    rsync_worktree_over_clone(temp_git_repo, clone_dir, cg=cg)

    assert (clone_dir / "tracked.txt").read_text() == "DIRTY\n"
    assert (clone_dir / "untracked.txt").read_text() == "hello\n"
    # The clone's .git was not clobbered (the worktree's .git -- a file --
    # would have broken it).
    assert (clone_dir / ".git").is_dir()


@pytest.mark.rsync
def test_rsync_worktree_over_clone_skips_default_excludes(
    cg: ConcurrencyGroup, tmp_path: Path, temp_git_repo: Path
) -> None:
    """Default excludes (.venv, __pycache__, node_modules, ...) don't pollute the clone."""
    (temp_git_repo / ".venv").mkdir()
    (temp_git_repo / ".venv" / "ghost.txt").write_text("nope\n")
    (temp_git_repo / "__pycache__").mkdir()
    (temp_git_repo / "__pycache__" / "x.pyc").write_text("nope\n")
    (temp_git_repo / "kept.txt").write_text("yes\n")

    clone_dir = tmp_path / "clone"
    subprocess.run(["git", "clone", f"file://{temp_git_repo}", str(clone_dir)], check=True)

    rsync_worktree_over_clone(temp_git_repo, clone_dir, cg=cg)

    assert (clone_dir / "kept.txt").read_text() == "yes\n"
    assert not (clone_dir / ".venv").exists()
    assert not (clone_dir / "__pycache__").exists()
