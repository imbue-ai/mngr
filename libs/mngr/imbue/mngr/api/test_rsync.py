"""Integration tests for ``rsync_to_remote`` and ``rsync_from_remote``.

The API takes path strings verbatim; tests append ``/`` to source paths to get
"copy contents into destination" semantics.
"""

import subprocess
from pathlib import Path
from typing import cast

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.api.git import UncommittedChangesError
from imbue.mngr.api.rsync import rsync_from_remote
from imbue.mngr.api.rsync import rsync_to_remote
from imbue.mngr.api.testing import FakeAgent
from imbue.mngr.api.testing import FakeHost
from imbue.mngr.api.testing import SyncTestContext
from imbue.mngr.api.testing import has_uncommitted_changes
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import UncommittedChangesMode
from imbue.mngr.utils.testing import get_stash_count
from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import run_git_command


@pytest.fixture
def pull_ctx(tmp_path: Path) -> SyncTestContext:
    """Test context: a remote (agent) directory and a local directory with a git repo."""
    agent_dir = tmp_path / "agent"
    local_dir = tmp_path / "host"
    agent_dir.mkdir(parents=True)
    init_git_repo(local_dir)
    return SyncTestContext(
        agent_dir=agent_dir,
        local_dir=local_dir,
        agent=cast(AgentInterface, FakeAgent(work_dir=agent_dir)),
        host=cast(HostInterface, FakeHost()),
    )


def _agent_src(ctx: SyncTestContext) -> str:
    """Trailing-slash form of ``ctx.agent_dir`` -- "copy contents into destination"."""
    return f"{ctx.agent_dir}/"


def _local_src(ctx: SyncTestContext) -> str:
    """Trailing-slash form of ``ctx.local_dir`` -- "copy contents into destination"."""
    return f"{ctx.local_dir}/"


# =============================================================================
# rsync_from_remote: FAIL mode (default)
# =============================================================================


@pytest.mark.rsync
def test_rsync_from_remote_fail_mode_with_clean_destination_succeeds(
    pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    (pull_ctx.agent_dir / "file.txt").write_text("agent content")
    assert not has_uncommitted_changes(pull_ctx.local_dir, cg)

    rsync_from_remote(
        remote_host=pull_ctx.host,
        remote_path=_agent_src(pull_ctx),
        local_path=pull_ctx.local_dir,
        extra_args=(),
        uncommitted_changes=UncommittedChangesMode.FAIL,
        cg=cg,
    )

    assert (pull_ctx.local_dir / "file.txt").read_text() == "agent content"


def test_rsync_from_remote_fail_mode_with_uncommitted_changes_raises(
    pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    (pull_ctx.agent_dir / "file.txt").write_text("agent content")
    (pull_ctx.local_dir / "uncommitted.txt").write_text("uncommitted content")
    assert has_uncommitted_changes(pull_ctx.local_dir, cg)

    with pytest.raises(UncommittedChangesError) as exc_info:
        rsync_from_remote(
            remote_host=pull_ctx.host,
            remote_path=_agent_src(pull_ctx),
            local_path=pull_ctx.local_dir,
            extra_args=(),
            uncommitted_changes=UncommittedChangesMode.FAIL,
            cg=cg,
        )

    assert exc_info.value.destination == pull_ctx.local_dir


# =============================================================================
# rsync_from_remote: CLOBBER mode
# =============================================================================


@pytest.mark.rsync
def test_rsync_from_remote_clobber_overwrites_local_changes(
    pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    (pull_ctx.agent_dir / "shared.txt").write_text("agent version")
    (pull_ctx.local_dir / "shared.txt").write_text("host version")
    assert has_uncommitted_changes(pull_ctx.local_dir, cg)

    rsync_from_remote(
        remote_host=pull_ctx.host,
        remote_path=_agent_src(pull_ctx),
        local_path=pull_ctx.local_dir,
        extra_args=(),
        uncommitted_changes=UncommittedChangesMode.CLOBBER,
        cg=cg,
    )

    assert (pull_ctx.local_dir / "shared.txt").read_text() == "agent version"


@pytest.mark.rsync
def test_rsync_from_remote_clobber_with_delete_removes_local_only_files(
    pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    (pull_ctx.agent_dir / "agent_file.txt").write_text("agent content")
    (pull_ctx.local_dir / "host_extra.txt").write_text("this should be deleted")
    run_git_command(pull_ctx.local_dir, "add", "host_extra.txt")
    run_git_command(pull_ctx.local_dir, "commit", "-m", "Add host extra file")

    rsync_from_remote(
        remote_host=pull_ctx.host,
        remote_path=_agent_src(pull_ctx),
        local_path=pull_ctx.local_dir,
        extra_args=("--delete",),
        uncommitted_changes=UncommittedChangesMode.CLOBBER,
        cg=cg,
    )

    assert not (pull_ctx.local_dir / "host_extra.txt").exists()
    assert (pull_ctx.local_dir / "agent_file.txt").read_text() == "agent content"


# =============================================================================
# rsync_from_remote: STASH mode
# =============================================================================


@pytest.mark.rsync
def test_rsync_from_remote_stash_leaves_changes_stashed(
    pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    (pull_ctx.agent_dir / "agent_file.txt").write_text("agent content")
    (pull_ctx.local_dir / "README.md").write_text("modified content")
    initial_stash_count = get_stash_count(pull_ctx.local_dir)
    assert has_uncommitted_changes(pull_ctx.local_dir, cg)

    rsync_from_remote(
        remote_host=pull_ctx.host,
        remote_path=_agent_src(pull_ctx),
        local_path=pull_ctx.local_dir,
        extra_args=(),
        uncommitted_changes=UncommittedChangesMode.STASH,
        cg=cg,
    )

    final_stash_count = get_stash_count(pull_ctx.local_dir)
    assert final_stash_count == initial_stash_count + 1
    assert (pull_ctx.local_dir / "README.md").read_text() == "Initial content"
    assert (pull_ctx.local_dir / "agent_file.txt").read_text() == "agent content"


@pytest.mark.rsync
def test_rsync_from_remote_stash_stashes_untracked_files(
    pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    (pull_ctx.agent_dir / "agent_file.txt").write_text("agent content")
    (pull_ctx.local_dir / "untracked_file.txt").write_text("untracked content")
    assert has_uncommitted_changes(pull_ctx.local_dir, cg)

    rsync_from_remote(
        remote_host=pull_ctx.host,
        remote_path=_agent_src(pull_ctx),
        local_path=pull_ctx.local_dir,
        extra_args=(),
        uncommitted_changes=UncommittedChangesMode.STASH,
        cg=cg,
    )

    assert not (pull_ctx.local_dir / "untracked_file.txt").exists()
    assert (pull_ctx.local_dir / "agent_file.txt").read_text() == "agent content"


# =============================================================================
# rsync_from_remote: MERGE mode
# =============================================================================


@pytest.mark.rsync
def test_rsync_from_remote_merge_restores_changes(
    pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    (pull_ctx.agent_dir / "agent_file.txt").write_text("agent content")
    (pull_ctx.local_dir / "README.md").write_text("host modified content")
    initial_stash_count = get_stash_count(pull_ctx.local_dir)
    assert has_uncommitted_changes(pull_ctx.local_dir, cg)

    rsync_from_remote(
        remote_host=pull_ctx.host,
        remote_path=_agent_src(pull_ctx),
        local_path=pull_ctx.local_dir,
        extra_args=(),
        uncommitted_changes=UncommittedChangesMode.MERGE,
        cg=cg,
    )

    final_stash_count = get_stash_count(pull_ctx.local_dir)
    assert final_stash_count == initial_stash_count
    assert (pull_ctx.local_dir / "README.md").read_text() == "host modified content"
    assert (pull_ctx.local_dir / "agent_file.txt").read_text() == "agent content"


@pytest.mark.rsync
def test_rsync_from_remote_merge_restores_untracked_files(
    pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    (pull_ctx.agent_dir / "agent_file.txt").write_text("agent content")
    (pull_ctx.local_dir / "untracked_file.txt").write_text("untracked content")

    rsync_from_remote(
        remote_host=pull_ctx.host,
        remote_path=_agent_src(pull_ctx),
        local_path=pull_ctx.local_dir,
        extra_args=(),
        uncommitted_changes=UncommittedChangesMode.MERGE,
        cg=cg,
    )

    assert (pull_ctx.local_dir / "untracked_file.txt").read_text() == "untracked content"
    assert (pull_ctx.local_dir / "agent_file.txt").read_text() == "agent content"


# =============================================================================
# rsync_from_remote: .git exclusion
# =============================================================================


@pytest.mark.rsync
def test_rsync_from_remote_excludes_git_directory(
    pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that rsync excludes the .git directory."""
    run_git_command(pull_ctx.agent_dir, "init")
    run_git_command(pull_ctx.agent_dir, "config", "user.email", "test@example.com")
    run_git_command(pull_ctx.agent_dir, "config", "user.name", "Test User")
    (pull_ctx.agent_dir / "file.txt").write_text("agent content")
    run_git_command(pull_ctx.agent_dir, "add", "file.txt")
    run_git_command(pull_ctx.agent_dir, "commit", "-m", "Add file")

    host_commit_before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=pull_ctx.local_dir,
        capture_output=True,
        text=True,
    ).stdout.strip()

    rsync_from_remote(
        remote_host=pull_ctx.host,
        remote_path=_agent_src(pull_ctx),
        local_path=pull_ctx.local_dir,
        extra_args=(),
        uncommitted_changes=UncommittedChangesMode.CLOBBER,
        cg=cg,
    )

    host_commit_after = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=pull_ctx.local_dir,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert host_commit_before == host_commit_after
    assert (pull_ctx.local_dir / "file.txt").read_text() == "agent content"


# =============================================================================
# rsync_from_remote: dry-run pass-through
# =============================================================================


@pytest.mark.rsync
def test_rsync_from_remote_dry_run_does_not_modify_files(
    pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    (pull_ctx.agent_dir / "new_file.txt").write_text("agent content")
    assert not (pull_ctx.local_dir / "new_file.txt").exists()

    rsync_from_remote(
        remote_host=pull_ctx.host,
        remote_path=_agent_src(pull_ctx),
        local_path=pull_ctx.local_dir,
        extra_args=("--dry-run",),
        uncommitted_changes=UncommittedChangesMode.FAIL,
        cg=cg,
    )

    assert not (pull_ctx.local_dir / "new_file.txt").exists()


# =============================================================================
# rsync_from_remote: non-git destination
# =============================================================================


@pytest.mark.rsync
def test_rsync_from_remote_to_non_git_directory_succeeds(
    tmp_path: Path,
    cg: ConcurrencyGroup,
) -> None:
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "file.txt").write_text("agent content")

    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()

    host = cast(OnlineHostInterface, FakeHost())

    rsync_from_remote(
        remote_host=host,
        remote_path=f"{agent_dir}/",
        local_path=dest_dir,
        extra_args=(),
        uncommitted_changes=UncommittedChangesMode.FAIL,
        cg=cg,
    )

    assert (dest_dir / "file.txt").read_text() == "agent content"


# =============================================================================
# rsync_to_remote
# =============================================================================


@pytest.fixture
def push_ctx(tmp_path: Path) -> SyncTestContext:
    """Test context: a local directory (with git repo) and a remote (agent) directory."""
    agent_dir = tmp_path / "agent"
    local_dir = tmp_path / "host"
    init_git_repo(local_dir)
    agent_dir.mkdir(parents=True)
    return SyncTestContext(
        agent_dir=agent_dir,
        local_dir=local_dir,
        agent=cast(AgentInterface, FakeAgent(work_dir=agent_dir)),
        host=cast(HostInterface, FakeHost()),
    )


@pytest.mark.rsync
def test_rsync_to_remote_transfers_files(
    push_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    (push_ctx.local_dir / "file.txt").write_text("local content")
    run_git_command(push_ctx.local_dir, "add", "file.txt")
    run_git_command(push_ctx.local_dir, "commit", "-m", "Add file")

    rsync_to_remote(
        local_path=_local_src(push_ctx),
        remote_host=push_ctx.host,
        remote_path=push_ctx.agent_dir,
        extra_args=(),
        uncommitted_changes=UncommittedChangesMode.FAIL,
        cg=cg,
    )

    assert (push_ctx.agent_dir / "file.txt").read_text() == "local content"


@pytest.mark.rsync
def test_rsync_to_remote_creates_destination_subdirectory(
    push_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    (push_ctx.local_dir / "file.txt").write_text("local content")
    run_git_command(push_ctx.local_dir, "add", "file.txt")
    run_git_command(push_ctx.local_dir, "commit", "-m", "Add file")

    new_remote = push_ctx.agent_dir / "new" / "subdir"
    assert not new_remote.exists()

    rsync_to_remote(
        local_path=_local_src(push_ctx),
        remote_host=push_ctx.host,
        remote_path=new_remote,
        extra_args=(),
        uncommitted_changes=UncommittedChangesMode.FAIL,
        cg=cg,
    )

    assert new_remote.exists()
    assert (new_remote / "file.txt").read_text() == "local content"


@pytest.mark.rsync
def test_rsync_to_remote_dry_run_does_not_transfer(
    push_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    (push_ctx.local_dir / "file.txt").write_text("local content")
    run_git_command(push_ctx.local_dir, "add", "file.txt")
    run_git_command(push_ctx.local_dir, "commit", "-m", "Add file")

    rsync_to_remote(
        local_path=_local_src(push_ctx),
        remote_host=push_ctx.host,
        remote_path=push_ctx.agent_dir,
        extra_args=("--dry-run",),
        uncommitted_changes=UncommittedChangesMode.FAIL,
        cg=cg,
    )

    assert not (push_ctx.agent_dir / "file.txt").exists()


@pytest.fixture
def remote_pull_ctx(tmp_path: Path) -> SyncTestContext:
    """Create a test context where the agent host is marked as non-local."""
    agent_dir = tmp_path / "agent"
    local_dir = tmp_path / "host"
    agent_dir.mkdir(parents=True)
    init_git_repo(local_dir)
    return SyncTestContext(
        agent_dir=agent_dir,
        local_dir=local_dir,
        agent=cast(AgentInterface, FakeAgent(work_dir=agent_dir)),
        host=cast(HostInterface, FakeHost(is_local=False)),
    )


def test_rsync_from_remote_without_ssh_info_raises_assertion(
    remote_pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    (remote_pull_ctx.agent_dir / "file.txt").write_text("agent content")

    with pytest.raises(AssertionError, match="SSH connection info"):
        rsync_from_remote(
            remote_host=remote_pull_ctx.host,
            remote_path=_agent_src(remote_pull_ctx),
            local_path=remote_pull_ctx.local_dir,
            extra_args=(),
            uncommitted_changes=UncommittedChangesMode.CLOBBER,
            cg=cg,
        )
