"""Unit tests for ``api/git.py``."""

import subprocess
from pathlib import Path
from typing import cast

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.api.git import GitSyncError
from imbue.mngr.api.git import GitignoreStatus
from imbue.mngr.api.git import LocalGitContext
from imbue.mngr.api.git import RemoteGitContext
from imbue.mngr.api.git import UncommittedChangesError
from imbue.mngr.api.git import _build_ssh_git_url
from imbue.mngr.api.git import check_path_gitignore_status
from imbue.mngr.api.git import check_path_repo_gitignore_status
from imbue.mngr.api.git import git_pull
from imbue.mngr.api.git import git_push
from imbue.mngr.api.testing import FakeHost
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import run_git_command

# =============================================================================
# Errors
# =============================================================================


def test_uncommitted_changes_error_contains_path_in_message() -> None:
    error = UncommittedChangesError(Path("/some/path"))
    assert "Uncommitted changes" in str(error)
    assert "/some/path" in str(error)


def test_uncommitted_changes_error_stores_destination_path() -> None:
    error = UncommittedChangesError(Path("/test/path"))
    assert error.destination == Path("/test/path")


def test_git_sync_error_contains_message_in_str() -> None:
    error = GitSyncError("something went wrong")
    assert "Git sync failed" in str(error)
    assert "something went wrong" in str(error)


# =============================================================================
# LocalGitContext (using real git repos)
# =============================================================================


def test_local_git_context_has_uncommitted_changes_returns_true_when_changes_exist(
    temp_git_repo: Path,
    cg: ConcurrencyGroup,
) -> None:
    (temp_git_repo / "dirty.txt").write_text("dirty")

    ctx = LocalGitContext(cg=cg)
    assert ctx.has_uncommitted_changes(temp_git_repo) is True


def test_local_git_context_has_uncommitted_changes_returns_false_when_clean(
    temp_git_repo: Path,
    cg: ConcurrencyGroup,
) -> None:
    ctx = LocalGitContext(cg=cg)
    assert ctx.has_uncommitted_changes(temp_git_repo) is False


def test_local_git_context_has_uncommitted_changes_raises_on_non_git_dir(
    tmp_path: Path,
    cg: ConcurrencyGroup,
) -> None:
    ctx = LocalGitContext(cg=cg)
    with pytest.raises(MngrError, match="git status failed"):
        ctx.has_uncommitted_changes(tmp_path)


def test_local_git_context_git_stash_returns_true_on_success(
    temp_git_repo: Path,
    cg: ConcurrencyGroup,
) -> None:
    (temp_git_repo / "README.md").write_text("modified")

    ctx = LocalGitContext(cg=cg)
    result = ctx.git_stash(temp_git_repo)
    assert result is True


def test_local_git_context_git_stash_returns_false_when_no_changes_to_save(
    temp_git_repo: Path,
    cg: ConcurrencyGroup,
) -> None:
    ctx = LocalGitContext(cg=cg)
    result = ctx.git_stash(temp_git_repo)
    assert result is False


def test_local_git_context_git_stash_pop_succeeds(
    temp_git_repo: Path,
    cg: ConcurrencyGroup,
) -> None:
    (temp_git_repo / "README.md").write_text("modified")

    ctx = LocalGitContext(cg=cg)
    ctx.git_stash(temp_git_repo)
    ctx.git_stash_pop(temp_git_repo)

    assert (temp_git_repo / "README.md").read_text() == "modified"


def test_local_git_context_git_stash_pop_raises_when_no_stash(
    temp_git_repo: Path,
    cg: ConcurrencyGroup,
) -> None:
    ctx = LocalGitContext(cg=cg)
    with pytest.raises(MngrError, match="git stash pop failed"):
        ctx.git_stash_pop(temp_git_repo)


def test_local_git_context_git_reset_hard_succeeds(
    temp_git_repo: Path,
    cg: ConcurrencyGroup,
) -> None:
    (temp_git_repo / "README.md").write_text("modified")
    (temp_git_repo / "untracked.txt").write_text("untracked")

    ctx = LocalGitContext(cg=cg)
    ctx.git_reset_hard(temp_git_repo)

    assert (temp_git_repo / "README.md").read_text() == "Initial content"
    assert not (temp_git_repo / "untracked.txt").exists()


def test_local_git_context_get_current_branch_returns_branch_name(
    temp_git_repo: Path,
    cg: ConcurrencyGroup,
) -> None:
    ctx = LocalGitContext(cg=cg)
    assert ctx.get_current_branch(temp_git_repo) == "main"


def test_local_git_context_is_git_repository_returns_true_for_git_repo(
    temp_git_repo: Path,
    cg: ConcurrencyGroup,
) -> None:
    ctx = LocalGitContext(cg=cg)
    assert ctx.is_git_repository(temp_git_repo) is True


def test_local_git_context_is_git_repository_returns_false_for_non_git_dir(
    tmp_path: Path,
    cg: ConcurrencyGroup,
) -> None:
    ctx = LocalGitContext(cg=cg)
    assert ctx.is_git_repository(tmp_path) is False


# =============================================================================
# RemoteGitContext (using FakeHost with real git repos)
# =============================================================================


def test_remote_git_context_has_uncommitted_changes_returns_true_when_changes_exist(
    temp_git_repo: Path,
) -> None:
    (temp_git_repo / "dirty.txt").write_text("dirty")

    host = cast(OnlineHostInterface, FakeHost())
    ctx = RemoteGitContext(host=host)
    assert ctx.has_uncommitted_changes(temp_git_repo) is True


def test_remote_git_context_has_uncommitted_changes_returns_false_when_clean(
    temp_git_repo: Path,
) -> None:
    host = cast(OnlineHostInterface, FakeHost())
    ctx = RemoteGitContext(host=host)
    assert ctx.has_uncommitted_changes(temp_git_repo) is False


def test_remote_git_context_git_stash_returns_true_on_success(
    temp_git_repo: Path,
) -> None:
    (temp_git_repo / "README.md").write_text("modified")

    host = cast(OnlineHostInterface, FakeHost())
    ctx = RemoteGitContext(host=host)
    result = ctx.git_stash(temp_git_repo)
    assert result is True


def test_remote_git_context_git_stash_returns_false_when_no_changes_to_save(
    temp_git_repo: Path,
) -> None:
    host = cast(OnlineHostInterface, FakeHost())
    ctx = RemoteGitContext(host=host)
    result = ctx.git_stash(temp_git_repo)
    assert result is False


def test_remote_git_context_git_reset_hard_succeeds(
    temp_git_repo: Path,
) -> None:
    (temp_git_repo / "README.md").write_text("modified")
    (temp_git_repo / "untracked.txt").write_text("untracked")

    host = cast(OnlineHostInterface, FakeHost())
    ctx = RemoteGitContext(host=host)
    ctx.git_reset_hard(temp_git_repo)

    assert (temp_git_repo / "README.md").read_text() == "Initial content"
    assert not (temp_git_repo / "untracked.txt").exists()


def test_remote_git_context_get_current_branch_returns_branch_name(
    temp_git_repo: Path,
) -> None:
    host = cast(OnlineHostInterface, FakeHost())
    ctx = RemoteGitContext(host=host)
    assert ctx.get_current_branch(temp_git_repo) == "main"


def test_remote_git_context_is_git_repository_returns_true_for_git_repo(
    temp_git_repo: Path,
) -> None:
    host = cast(OnlineHostInterface, FakeHost())
    ctx = RemoteGitContext(host=host)
    assert ctx.is_git_repository(temp_git_repo) is True


def test_remote_git_context_is_git_repository_returns_false_for_non_git_dir(
    tmp_path: Path,
) -> None:
    host = cast(OnlineHostInterface, FakeHost())
    ctx = RemoteGitContext(host=host)
    assert ctx.is_git_repository(tmp_path) is False


# =============================================================================
# SSH helper
# =============================================================================


def test_build_ssh_git_url_produces_correct_url() -> None:
    ssh_info = ("root", "example.com", 2222, Path("/tmp/key"))
    result = _build_ssh_git_url(ssh_info, Path("/home/user/project"))
    assert result == "ssh://root@example.com:2222/home/user/project/.git"


# =============================================================================
# git_push / git_pull end-to-end (local host)
# =============================================================================


def test_git_pull_transfers_commit_from_remote_to_local(
    tmp_path: Path,
    cg: ConcurrencyGroup,
) -> None:
    """git_pull pulls a new commit from a remote (here: local FakeHost) into the local repo."""
    local_dir = tmp_path / "local"
    agent_dir = tmp_path / "agent"

    init_git_repo(local_dir)
    subprocess.run(["git", "clone", str(local_dir), str(agent_dir)], capture_output=True, check=True)
    run_git_command(agent_dir, "config", "user.email", "test@example.com")
    run_git_command(agent_dir, "config", "user.name", "Test User")

    (agent_dir / "agent_file.txt").write_text("agent content")
    run_git_command(agent_dir, "add", "agent_file.txt")
    run_git_command(agent_dir, "commit", "-m", "Agent commit")

    host = cast(OnlineHostInterface, FakeHost(is_local=True))
    git_pull(
        local_path=local_dir,
        remote_host=host,
        remote_path=agent_dir,
        extra_args=("main", "--no-edit"),
        cg=cg,
    )

    assert (local_dir / "agent_file.txt").read_text() == "agent content"


def test_git_push_transfers_commit_from_local_to_remote(
    tmp_path: Path,
    cg: ConcurrencyGroup,
) -> None:
    """git_push pushes a new commit from local to the remote (here: local FakeHost)."""
    local_dir = tmp_path / "local"
    agent_dir = tmp_path / "agent"

    init_git_repo(agent_dir)
    subprocess.run(["git", "clone", str(agent_dir), str(local_dir)], capture_output=True, check=True)
    run_git_command(local_dir, "config", "user.email", "test@example.com")
    run_git_command(local_dir, "config", "user.name", "Test User")

    (local_dir / "local_file.txt").write_text("local content")
    run_git_command(local_dir, "add", "local_file.txt")
    run_git_command(local_dir, "commit", "-m", "Local commit")

    host = cast(OnlineHostInterface, FakeHost(is_local=True))
    git_push(
        local_path=local_dir,
        remote_host=host,
        remote_path=agent_dir,
        extra_args=("main",),
        cg=cg,
    )

    assert (agent_dir / "local_file.txt").read_text() == "local content"


# === check_path_gitignore_status ===
#
# The helper is path-agnostic (it takes any repo-relative path, not just a
# ``.claude/`` subpath) and resolves a symlink anywhere in the path, not only a
# leading one. These tests pin both with non-``.claude`` paths.


def test_check_path_gitignore_status_returns_not_ignored_for_tracked_path(tmp_path: Path) -> None:
    """A path that no .gitignore rule covers is reported NOT_IGNORED, with the path echoed back."""
    init_git_repo(tmp_path, initial_commit=False)
    host = cast(OnlineHostInterface, FakeHost())

    status, checked = check_path_gitignore_status(host, tmp_path, Path(".config") / "app" / "state.json")

    assert status is GitignoreStatus.NOT_IGNORED
    assert checked == Path(".config") / "app" / "state.json"


def test_check_path_gitignore_status_returns_ignored_for_any_dir_rule(tmp_path: Path) -> None:
    """A directory .gitignore rule covers a non-.claude path -- the helper is not claude-specific."""
    init_git_repo(tmp_path, initial_commit=False)
    (tmp_path / ".gitignore").write_text(".config/\n")
    host = cast(OnlineHostInterface, FakeHost())

    status, _ = check_path_gitignore_status(host, tmp_path, Path(".config") / "app" / "state.json")

    assert status is GitignoreStatus.IGNORED


def test_check_path_gitignore_status_skips_outside_git_repo(tmp_path: Path) -> None:
    """Outside a git work tree there is nothing to enforce, so the status is SKIP."""
    host = cast(OnlineHostInterface, FakeHost())

    status, _ = check_path_gitignore_status(host, tmp_path, Path(".config") / "state.json")

    assert status is GitignoreStatus.SKIP


def test_check_path_gitignore_status_resolves_symlink_at_any_depth(tmp_path: Path) -> None:
    """A symlink mid-path (not just leading) is resolved before consulting git.

    ``state/cache`` is a symlink to a real ``backend`` dir; the checked path runs
    through it and does not exist yet. The helper must report the path against
    the symlink-resolved location git actually tracks (``backend/...``), which a
    leading-only resolver would miss -- git check-ignore would instead choke with
    "beyond a symbolic link".
    """
    init_git_repo(tmp_path, initial_commit=False)
    (tmp_path / "state").mkdir()
    (tmp_path / "backend").mkdir()
    (tmp_path / "state" / "cache").symlink_to(tmp_path / "backend")
    # Ignore the resolved real path, the one git sees -- not the symlink path.
    (tmp_path / ".gitignore").write_text("backend/\n")
    host = cast(OnlineHostInterface, FakeHost())

    status, checked = check_path_gitignore_status(host, tmp_path, Path("state") / "cache" / "app" / "state.json")

    assert status is GitignoreStatus.IGNORED
    assert checked == Path("backend") / "app" / "state.json"


# === check_path_repo_gitignore_status ===
#
# Same as check_path_gitignore_status, but distinguishes a repo-level rule from
# the user's global excludes (which won't exist on a fresh clone / remote host).


def test_check_path_repo_gitignore_status_ignored_by_repo_rule(tmp_path: Path) -> None:
    """A rule in the repo's own .gitignore counts as IGNORED."""
    init_git_repo(tmp_path, initial_commit=False)
    (tmp_path / ".gitignore").write_text(".config/\n")
    host = cast(OnlineHostInterface, FakeHost())

    status, _ = check_path_repo_gitignore_status(host, tmp_path, Path(".config") / "app" / "state.json")

    assert status is GitignoreStatus.IGNORED


def test_check_path_repo_gitignore_status_only_global(tmp_path: Path) -> None:
    """A path ignored solely via core.excludesFile is ONLY_GLOBAL, not IGNORED.

    The repo has no rule of its own; the match comes only from the configured
    excludes file. A remote host / fresh clone wouldn't have that file, so the
    repo-rule check must flag it.
    """
    init_git_repo(tmp_path, initial_commit=False)
    excludes_file = tmp_path / "global_excludes"
    excludes_file.write_text(".config/\n")
    # Point core.excludesFile (the "global" excludes) at it via local config --
    # this does not touch the developer's real global git config.
    run_git_command(tmp_path, "config", "core.excludesFile", str(excludes_file))
    host = cast(OnlineHostInterface, FakeHost())

    # Sanity: the any-rule check sees it as ignored...
    base_status, _ = check_path_gitignore_status(host, tmp_path, Path(".config") / "app" / "state.json")
    assert base_status is GitignoreStatus.IGNORED
    # ...but the repo-rule check flags that only a global rule covers it.
    status, _ = check_path_repo_gitignore_status(host, tmp_path, Path(".config") / "app" / "state.json")
    assert status is GitignoreStatus.ONLY_GLOBAL


def test_check_path_repo_gitignore_status_passes_through_not_ignored(tmp_path: Path) -> None:
    """When no rule matches at all, the repo-rule check returns NOT_IGNORED unchanged."""
    init_git_repo(tmp_path, initial_commit=False)
    host = cast(OnlineHostInterface, FakeHost())

    status, _ = check_path_repo_gitignore_status(host, tmp_path, Path(".config") / "app" / "state.json")

    assert status is GitignoreStatus.NOT_IGNORED
