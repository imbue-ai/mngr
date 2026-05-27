"""Unit tests for sync API functions."""

import subprocess
from pathlib import Path
from typing import cast

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.api.sync import GitSyncError
from imbue.mngr.api.sync import LocalGitContext
from imbue.mngr.api.sync import RemoteGitContext
from imbue.mngr.api.sync import RsyncEndpointError
from imbue.mngr.api.sync import RsyncResult
from imbue.mngr.api.sync import UncommittedChangesError
from imbue.mngr.api.sync import _build_remote_rsync_command
from imbue.mngr.api.sync import _build_rsync_command
from imbue.mngr.api.sync import _build_ssh_git_url
from imbue.mngr.api.sync import git_pull
from imbue.mngr.api.sync import git_push
from imbue.mngr.api.sync import rsync
from imbue.mngr.api.testing import FakeHost
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import UncommittedChangesMode
from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import run_git_command

# =============================================================================
# RsyncResult model tests
# =============================================================================


def test_rsync_result_can_be_created_with_all_fields() -> None:
    result = RsyncResult(
        files_transferred=10,
        bytes_transferred=1024,
        source_path=Path("/source"),
        destination_path=Path("/dest"),
        is_dry_run=False,
    )

    assert result.files_transferred == 10
    assert result.bytes_transferred == 1024
    assert result.source_path == Path("/source")
    assert result.destination_path == Path("/dest")
    assert result.is_dry_run is False


def test_rsync_result_supports_dry_run() -> None:
    result = RsyncResult(
        files_transferred=5,
        bytes_transferred=0,
        source_path=Path("/source"),
        destination_path=Path("/dest"),
        is_dry_run=True,
    )

    assert result.is_dry_run is True


# =============================================================================
# UncommittedChangesError tests
# =============================================================================


def test_uncommitted_changes_error_contains_path_in_message() -> None:
    error = UncommittedChangesError(Path("/some/path"))
    assert "Uncommitted changes" in str(error)
    assert "/some/path" in str(error)


def test_uncommitted_changes_error_stores_destination_path() -> None:
    error = UncommittedChangesError(Path("/test/path"))
    assert error.destination == Path("/test/path")


# =============================================================================
# GitSyncError tests
# =============================================================================


def test_git_sync_error_contains_message_in_str() -> None:
    error = GitSyncError("something went wrong")
    assert "Git sync failed" in str(error)
    assert "something went wrong" in str(error)


# =============================================================================
# LocalGitContext tests (using real git repos)
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
# RemoteGitContext tests (using FakeHost with real git repos)
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
# SSH helper function tests
# =============================================================================


def test_build_ssh_git_url_produces_correct_url() -> None:
    ssh_info = ("root", "example.com", 2222, Path("/tmp/key"))
    result = _build_ssh_git_url(ssh_info, Path("/home/user/project"))
    assert result == "ssh://root@example.com:2222/home/user/project/.git"


# =============================================================================
# rsync command builder tests
# =============================================================================


def test_build_rsync_command_includes_stats_and_excludes_git() -> None:
    cmd = _build_rsync_command(Path("/src"), Path("/dst"), is_dry_run=False, is_delete=False)
    assert "--stats" in cmd
    assert "--exclude=.git" in cmd
    assert cmd[-2] == "/src/"
    assert cmd[-1] == "/dst"


def test_build_rsync_command_adds_dry_run_flag() -> None:
    cmd = _build_rsync_command(Path("/src"), Path("/dst"), is_dry_run=True, is_delete=False)
    assert "--dry-run" in cmd


def test_build_rsync_command_adds_delete_flag() -> None:
    cmd = _build_rsync_command(Path("/src"), Path("/dst"), is_dry_run=False, is_delete=True)
    assert "--delete" in cmd


def test_build_remote_rsync_command_push_uses_remote_destination() -> None:
    ssh_info = ("root", "example.com", 22, Path("/tmp/key"))
    cmd = _build_remote_rsync_command(
        local_path=Path("/local/src"),
        remote_path=Path("/remote/dst"),
        ssh_info=ssh_info,
        known_hosts_file=None,
        is_push=True,
        is_dry_run=False,
        is_delete=False,
    )
    assert cmd[-2] == "/local/src/"
    assert cmd[-1] == "root@example.com:/remote/dst/"
    assert "-e" in cmd


def test_build_remote_rsync_command_pull_uses_remote_source() -> None:
    ssh_info = ("user", "host.com", 2222, Path("/key"))
    cmd = _build_remote_rsync_command(
        local_path=Path("/local/dst"),
        remote_path=Path("/remote/src"),
        ssh_info=ssh_info,
        known_hosts_file=None,
        is_push=False,
        is_dry_run=False,
        is_delete=False,
    )
    assert cmd[-2] == "user@host.com:/remote/src/"
    assert cmd[-1] == "/local/dst"


# =============================================================================
# rsync endpoint validation
# =============================================================================


def test_rsync_rejects_remote_to_remote_transfers(
    tmp_path: Path,
    cg: ConcurrencyGroup,
) -> None:
    source_host = cast(OnlineHostInterface, FakeHost(is_local=False))
    destination_host = cast(OnlineHostInterface, FakeHost(is_local=False))
    with pytest.raises(RsyncEndpointError):
        rsync(
            source_host=source_host,
            source_path=tmp_path / "src",
            destination_host=destination_host,
            destination_path=tmp_path / "dst",
            is_dry_run=False,
            is_delete=False,
            uncommitted_changes=UncommittedChangesMode.FAIL,
            cg=cg,
        )


# =============================================================================
# git_pull / git_push end-to-end (local host)
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
