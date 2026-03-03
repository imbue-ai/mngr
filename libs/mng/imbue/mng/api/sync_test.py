"""Unit tests for sync API functions."""

import subprocess
from pathlib import Path
from typing import cast

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mng.api.sync import GitSyncError
from imbue.mng.api.sync import LocalGitContext
from imbue.mng.api.sync import NotAGitRepositoryError
from imbue.mng.api.sync import RemoteGitContext
from imbue.mng.api.sync import SyncFilesResult
from imbue.mng.api.sync import SyncGitResult
from imbue.mng.api.sync import UncommittedChangesError
from imbue.mng.api.sync import sync_git
from imbue.mng.api.test_fixtures import FakeAgent
from imbue.mng.api.test_fixtures import FakeHost
from imbue.mng.errors import MngError
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import SyncMode
from imbue.mng.primitives import UncommittedChangesMode
from imbue.mng.utils.testing import init_git_repo_with_config
from imbue.mng.utils.testing import run_git_command

# =============================================================================
# SyncMode enum tests
# =============================================================================


def test_sync_mode_push_has_correct_value() -> None:
    assert SyncMode.PUSH.value == "PUSH"


def test_sync_mode_pull_has_correct_value() -> None:
    assert SyncMode.PULL.value == "PULL"


# =============================================================================
# SyncFilesResult model tests
# =============================================================================


def test_sync_files_result_can_be_created_with_all_fields() -> None:
    result = SyncFilesResult(
        files_transferred=10,
        bytes_transferred=1024,
        source_path=Path("/source"),
        destination_path=Path("/dest"),
        is_dry_run=False,
        mode=SyncMode.PUSH,
    )

    assert result.files_transferred == 10
    assert result.bytes_transferred == 1024
    assert result.source_path == Path("/source")
    assert result.destination_path == Path("/dest")
    assert result.is_dry_run is False
    assert result.mode == SyncMode.PUSH


def test_sync_files_result_supports_dry_run_mode() -> None:
    result = SyncFilesResult(
        files_transferred=5,
        bytes_transferred=0,
        source_path=Path("/source"),
        destination_path=Path("/dest"),
        is_dry_run=True,
        mode=SyncMode.PULL,
    )

    assert result.is_dry_run is True
    assert result.mode == SyncMode.PULL


def test_sync_files_result_can_be_serialized_to_dict() -> None:
    result = SyncFilesResult(
        files_transferred=3,
        bytes_transferred=500,
        source_path=Path("/src"),
        destination_path=Path("/dst"),
        is_dry_run=False,
        mode=SyncMode.PUSH,
    )

    data = result.model_dump()
    assert data["files_transferred"] == 3
    assert data["bytes_transferred"] == 500
    assert data["mode"] == SyncMode.PUSH


# =============================================================================
# SyncGitResult model tests
# =============================================================================


def test_sync_git_result_can_be_created_with_all_fields() -> None:
    result = SyncGitResult(
        source_branch="feature",
        target_branch="main",
        source_path=Path("/source"),
        destination_path=Path("/dest"),
        is_dry_run=False,
        commits_transferred=5,
        mode=SyncMode.PUSH,
    )

    assert result.source_branch == "feature"
    assert result.target_branch == "main"
    assert result.source_path == Path("/source")
    assert result.destination_path == Path("/dest")
    assert result.is_dry_run is False
    assert result.commits_transferred == 5
    assert result.mode == SyncMode.PUSH


def test_sync_git_result_supports_dry_run_mode() -> None:
    result = SyncGitResult(
        source_branch="dev",
        target_branch="main",
        source_path=Path("/src"),
        destination_path=Path("/dst"),
        is_dry_run=True,
        commits_transferred=0,
        mode=SyncMode.PULL,
    )

    assert result.is_dry_run is True
    assert result.mode == SyncMode.PULL


# =============================================================================
# UncommittedChangesError tests
# =============================================================================


def test_uncommitted_changes_error_contains_path_in_message() -> None:
    error = UncommittedChangesError(Path("/some/path"))
    assert "Uncommitted changes" in str(error)
    assert "/some/path" in str(error)


def test_uncommitted_changes_error_provides_user_help_text() -> None:
    error = UncommittedChangesError(Path("/some/path"))
    assert "stash" in error.user_help_text.lower()
    assert "clobber" in error.user_help_text.lower()


def test_uncommitted_changes_error_stores_destination_path() -> None:
    error = UncommittedChangesError(Path("/test/path"))
    assert error.destination == Path("/test/path")


# =============================================================================
# NotAGitRepositoryError tests
# =============================================================================


def test_not_a_git_repository_error_contains_path_in_message() -> None:
    error = NotAGitRepositoryError(Path("/not/a/repo"))
    assert "Not a git repository" in str(error)
    assert "/not/a/repo" in str(error)


def test_not_a_git_repository_error_provides_user_help_text() -> None:
    error = NotAGitRepositoryError(Path("/some/path"))
    assert "sync-mode=files" in error.user_help_text


def test_not_a_git_repository_error_stores_path() -> None:
    error = NotAGitRepositoryError(Path("/test/path"))
    assert error.path == Path("/test/path")


# =============================================================================
# GitSyncError tests
# =============================================================================


def test_git_sync_error_contains_message_in_str() -> None:
    error = GitSyncError("something went wrong")
    assert "Git sync failed" in str(error)
    assert "something went wrong" in str(error)


def test_git_sync_error_provides_user_help_text() -> None:
    error = GitSyncError("test")
    assert error.user_help_text is not None


# =============================================================================
# LocalGitContext tests (using real git repos)
# =============================================================================


def test_local_git_context_has_uncommitted_changes_returns_true_when_changes_exist(
    tmp_path: Path,
    cg: ConcurrencyGroup,
) -> None:
    init_git_repo_with_config(tmp_path)
    (tmp_path / "dirty.txt").write_text("dirty")

    ctx = LocalGitContext(cg=cg)
    assert ctx.has_uncommitted_changes(tmp_path) is True


def test_local_git_context_has_uncommitted_changes_returns_false_when_clean(
    tmp_path: Path,
    cg: ConcurrencyGroup,
) -> None:
    init_git_repo_with_config(tmp_path)

    ctx = LocalGitContext(cg=cg)
    assert ctx.has_uncommitted_changes(tmp_path) is False


def test_local_git_context_has_uncommitted_changes_raises_on_non_git_dir(
    tmp_path: Path,
    cg: ConcurrencyGroup,
) -> None:
    ctx = LocalGitContext(cg=cg)
    with pytest.raises(MngError, match="git status failed"):
        ctx.has_uncommitted_changes(tmp_path)


def test_local_git_context_git_stash_returns_true_on_success(
    tmp_path: Path,
    cg: ConcurrencyGroup,
) -> None:
    init_git_repo_with_config(tmp_path)
    (tmp_path / "README.md").write_text("modified")

    ctx = LocalGitContext(cg=cg)
    result = ctx.git_stash(tmp_path)
    assert result is True


def test_local_git_context_git_stash_returns_false_when_no_changes_to_save(
    tmp_path: Path,
    cg: ConcurrencyGroup,
) -> None:
    init_git_repo_with_config(tmp_path)

    ctx = LocalGitContext(cg=cg)
    result = ctx.git_stash(tmp_path)
    assert result is False


def test_local_git_context_git_stash_pop_succeeds(
    tmp_path: Path,
    cg: ConcurrencyGroup,
) -> None:
    init_git_repo_with_config(tmp_path)
    (tmp_path / "README.md").write_text("modified")

    ctx = LocalGitContext(cg=cg)
    ctx.git_stash(tmp_path)
    ctx.git_stash_pop(tmp_path)

    assert (tmp_path / "README.md").read_text() == "modified"


def test_local_git_context_git_stash_pop_raises_when_no_stash(
    tmp_path: Path,
    cg: ConcurrencyGroup,
) -> None:
    init_git_repo_with_config(tmp_path)

    ctx = LocalGitContext(cg=cg)
    with pytest.raises(MngError, match="git stash pop failed"):
        ctx.git_stash_pop(tmp_path)


def test_local_git_context_git_reset_hard_succeeds(
    tmp_path: Path,
    cg: ConcurrencyGroup,
) -> None:
    init_git_repo_with_config(tmp_path)
    (tmp_path / "README.md").write_text("modified")
    (tmp_path / "untracked.txt").write_text("untracked")

    ctx = LocalGitContext(cg=cg)
    ctx.git_reset_hard(tmp_path)

    assert (tmp_path / "README.md").read_text() == "Initial content"
    assert not (tmp_path / "untracked.txt").exists()


def test_local_git_context_get_current_branch_returns_branch_name(
    tmp_path: Path,
    cg: ConcurrencyGroup,
) -> None:
    init_git_repo_with_config(tmp_path)

    ctx = LocalGitContext(cg=cg)
    assert ctx.get_current_branch(tmp_path) == "main"


def test_local_git_context_is_git_repository_returns_true_for_git_repo(
    tmp_path: Path,
    cg: ConcurrencyGroup,
) -> None:
    init_git_repo_with_config(tmp_path)

    ctx = LocalGitContext(cg=cg)
    assert ctx.is_git_repository(tmp_path) is True


def test_local_git_context_is_git_repository_returns_false_for_non_git_dir(
    tmp_path: Path,
    cg: ConcurrencyGroup,
) -> None:
    ctx = LocalGitContext(cg=cg)
    assert ctx.is_git_repository(tmp_path) is False


# =============================================================================
# RemoteGitContext tests (using FakeHost with real git repos)
# =============================================================================


def test_remote_git_context_has_uncommitted_changes_returns_true_when_changes_exist(
    tmp_path: Path,
) -> None:
    init_git_repo_with_config(tmp_path)
    (tmp_path / "dirty.txt").write_text("dirty")

    host = cast(OnlineHostInterface, FakeHost())
    ctx = RemoteGitContext(host=host)
    assert ctx.has_uncommitted_changes(tmp_path) is True


def test_remote_git_context_has_uncommitted_changes_returns_false_when_clean(
    tmp_path: Path,
) -> None:
    init_git_repo_with_config(tmp_path)

    host = cast(OnlineHostInterface, FakeHost())
    ctx = RemoteGitContext(host=host)
    assert ctx.has_uncommitted_changes(tmp_path) is False


def test_remote_git_context_has_uncommitted_changes_raises_on_non_git_dir(
    tmp_path: Path,
) -> None:
    host = cast(OnlineHostInterface, FakeHost())
    ctx = RemoteGitContext(host=host)
    with pytest.raises(MngError, match="git status failed"):
        ctx.has_uncommitted_changes(tmp_path)


def test_remote_git_context_git_stash_returns_true_on_success(
    tmp_path: Path,
) -> None:
    init_git_repo_with_config(tmp_path)
    (tmp_path / "README.md").write_text("modified")

    host = cast(OnlineHostInterface, FakeHost())
    ctx = RemoteGitContext(host=host)
    result = ctx.git_stash(tmp_path)
    assert result is True


def test_remote_git_context_git_stash_returns_false_when_no_changes_to_save(
    tmp_path: Path,
) -> None:
    init_git_repo_with_config(tmp_path)

    host = cast(OnlineHostInterface, FakeHost())
    ctx = RemoteGitContext(host=host)
    result = ctx.git_stash(tmp_path)
    assert result is False


def test_remote_git_context_git_stash_pop_succeeds(
    tmp_path: Path,
) -> None:
    init_git_repo_with_config(tmp_path)
    (tmp_path / "README.md").write_text("modified")

    host = cast(OnlineHostInterface, FakeHost())
    ctx = RemoteGitContext(host=host)
    ctx.git_stash(tmp_path)
    ctx.git_stash_pop(tmp_path)

    assert (tmp_path / "README.md").read_text() == "modified"


def test_remote_git_context_git_stash_pop_raises_when_no_stash(
    tmp_path: Path,
) -> None:
    init_git_repo_with_config(tmp_path)

    host = cast(OnlineHostInterface, FakeHost())
    ctx = RemoteGitContext(host=host)
    with pytest.raises(MngError, match="git stash pop failed"):
        ctx.git_stash_pop(tmp_path)


def test_remote_git_context_git_reset_hard_succeeds(
    tmp_path: Path,
) -> None:
    init_git_repo_with_config(tmp_path)
    (tmp_path / "README.md").write_text("modified")
    (tmp_path / "untracked.txt").write_text("untracked")

    host = cast(OnlineHostInterface, FakeHost())
    ctx = RemoteGitContext(host=host)
    ctx.git_reset_hard(tmp_path)

    assert (tmp_path / "README.md").read_text() == "Initial content"
    assert not (tmp_path / "untracked.txt").exists()


def test_remote_git_context_get_current_branch_returns_branch_name(
    tmp_path: Path,
) -> None:
    init_git_repo_with_config(tmp_path)

    host = cast(OnlineHostInterface, FakeHost())
    ctx = RemoteGitContext(host=host)
    assert ctx.get_current_branch(tmp_path) == "main"


def test_remote_git_context_get_current_branch_returns_feature_branch(
    tmp_path: Path,
) -> None:
    init_git_repo_with_config(tmp_path)
    run_git_command(tmp_path, "checkout", "-b", "feature-branch")

    host = cast(OnlineHostInterface, FakeHost())
    ctx = RemoteGitContext(host=host)
    assert ctx.get_current_branch(tmp_path) == "feature-branch"


def test_remote_git_context_is_git_repository_returns_true_for_git_repo(
    tmp_path: Path,
) -> None:
    init_git_repo_with_config(tmp_path)

    host = cast(OnlineHostInterface, FakeHost())
    ctx = RemoteGitContext(host=host)
    assert ctx.is_git_repository(tmp_path) is True


def test_remote_git_context_is_git_repository_returns_false_for_non_git_dir(
    tmp_path: Path,
) -> None:
    host = cast(OnlineHostInterface, FakeHost())
    ctx = RemoteGitContext(host=host)
    assert ctx.is_git_repository(tmp_path) is False


# =============================================================================
# sync_git safe.directory regression test
# =============================================================================


def test_sync_git_adds_safe_directory_for_non_local_host(
    tmp_path: Path,
    cg: ConcurrencyGroup,
) -> None:
    """Regression test: sync_git must add safe.directory for non-local hosts.

    Without this, git operations on remote hosts can fail with "detected dubious
    ownership" when file ownership differs from the SSH user (e.g., after rsync
    from a local machine with a different UID).
    """
    local_dir = tmp_path / "local"
    agent_dir = tmp_path / "agent"

    init_git_repo_with_config(local_dir)

    subprocess.run(
        ["git", "clone", str(local_dir), str(agent_dir)],
        capture_output=True,
        text=True,
        check=True,
    )
    run_git_command(agent_dir, "config", "user.email", "test@example.com")
    run_git_command(agent_dir, "config", "user.name", "Test User")

    host = cast(OnlineHostInterface, FakeHost(is_local=False))
    agent = cast(AgentInterface, FakeAgent(work_dir=agent_dir))

    # Add a commit to agent so there's something to pull
    (agent_dir / "agent_file.txt").write_text("agent content")
    run_git_command(agent_dir, "add", "agent_file.txt")
    run_git_command(agent_dir, "commit", "-m", "Agent commit")

    sync_git(
        agent=agent,
        host=host,
        mode=SyncMode.PULL,
        local_path=local_dir,
        source_branch=None,
        target_branch=None,
        is_dry_run=False,
        uncommitted_changes=UncommittedChangesMode.FAIL,
        is_mirror=False,
        cg=cg,
    )

    # Verify safe.directory was added to the global gitconfig
    result = subprocess.run(
        ["git", "config", "--global", "--get-all", "safe.directory"],
        capture_output=True,
        text=True,
    )
    assert str(agent_dir) in result.stdout.strip().splitlines()

    # Also verify the pull actually worked
    assert (local_dir / "agent_file.txt").read_text() == "agent content"
