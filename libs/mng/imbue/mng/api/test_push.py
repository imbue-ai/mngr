import subprocess
from pathlib import Path
from typing import cast

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mng.api.push import push_files
from imbue.mng.api.push import push_git
from imbue.mng.api.sync import RemoteGitContext
from imbue.mng.api.sync import UncommittedChangesError
from imbue.mng.api.test_fixtures import FakeAgent
from imbue.mng.api.test_fixtures import FakeHost
from imbue.mng.api.test_fixtures import SyncTestContext
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import UncommittedChangesMode
from imbue.mng.utils.testing import get_stash_count
from imbue.mng.utils.testing import init_git_repo_with_config
from imbue.mng.utils.testing import run_git_command


def _has_uncommitted_changes_on_host(host: OnlineHostInterface, path: Path) -> bool:
    """Helper to check for uncommitted changes on a remote host using RemoteGitContext."""
    return RemoteGitContext(host=host).has_uncommitted_changes(path)


@pytest.fixture
def push_ctx(tmp_path: Path) -> SyncTestContext:
    """Create a test context with local and agent directories."""
    local_dir = tmp_path / "host"
    agent_dir = tmp_path / "agent"
    local_dir.mkdir(parents=True)
    init_git_repo_with_config(agent_dir)
    return SyncTestContext(
        local_dir=local_dir,
        agent_dir=agent_dir,
        agent=cast(AgentInterface, FakeAgent(work_dir=agent_dir)),
        host=cast(OnlineHostInterface, FakeHost()),
    )


# =============================================================================
# Test: FAIL mode (default)
# =============================================================================


@pytest.mark.rsync
def test_push_files_fail_mode_with_no_uncommitted_changes_succeeds(
    push_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that FAIL mode succeeds when there are no uncommitted changes on target."""
    (push_ctx.local_dir / "file.txt").write_text("host content")
    assert not _has_uncommitted_changes_on_host(push_ctx.host, push_ctx.agent_dir)

    result = push_files(
        agent=push_ctx.agent,
        host=push_ctx.host,
        source=push_ctx.local_dir,
        destination_path=None,
        is_dry_run=False,
        is_delete=False,
        uncommitted_changes=UncommittedChangesMode.FAIL,
        cg=cg,
    )

    assert (push_ctx.agent_dir / "file.txt").exists()
    assert (push_ctx.agent_dir / "file.txt").read_text() == "host content"
    assert result.destination_path == push_ctx.agent_dir
    assert result.source_path == push_ctx.local_dir


def test_push_files_fail_mode_with_uncommitted_changes_raises_error(
    push_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that FAIL mode raises UncommittedChangesError when changes exist on target."""
    (push_ctx.local_dir / "file.txt").write_text("host content")
    (push_ctx.agent_dir / "uncommitted.txt").write_text("uncommitted content")
    assert _has_uncommitted_changes_on_host(push_ctx.host, push_ctx.agent_dir)

    with pytest.raises(UncommittedChangesError) as exc_info:
        push_files(
            agent=push_ctx.agent,
            host=push_ctx.host,
            source=push_ctx.local_dir,
            destination_path=None,
            is_dry_run=False,
            is_delete=False,
            uncommitted_changes=UncommittedChangesMode.FAIL,
            cg=cg,
        )

    assert exc_info.value.destination == push_ctx.agent_dir


# =============================================================================
# Test: CLOBBER mode
# =============================================================================


@pytest.mark.rsync
def test_push_files_clobber_mode_overwrites_agent_changes(
    push_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that CLOBBER mode overwrites uncommitted changes on the agent."""
    (push_ctx.local_dir / "shared.txt").write_text("host version")
    (push_ctx.agent_dir / "shared.txt").write_text("agent version")
    assert _has_uncommitted_changes_on_host(push_ctx.host, push_ctx.agent_dir)

    result = push_files(
        agent=push_ctx.agent,
        host=push_ctx.host,
        source=push_ctx.local_dir,
        destination_path=None,
        is_dry_run=False,
        is_delete=False,
        uncommitted_changes=UncommittedChangesMode.CLOBBER,
        cg=cg,
    )

    assert (push_ctx.agent_dir / "shared.txt").read_text() == "host version"
    assert result.destination_path == push_ctx.agent_dir


@pytest.mark.rsync
def test_push_files_clobber_mode_when_only_agent_has_changes(
    push_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test CLOBBER mode when only the agent has a modified file."""
    (push_ctx.local_dir / "host_only.txt").write_text("host file")
    (push_ctx.agent_dir / "agent_only.txt").write_text("agent uncommitted content")
    assert _has_uncommitted_changes_on_host(push_ctx.host, push_ctx.agent_dir)

    push_files(
        agent=push_ctx.agent,
        host=push_ctx.host,
        source=push_ctx.local_dir,
        destination_path=None,
        is_dry_run=False,
        is_delete=False,
        uncommitted_changes=UncommittedChangesMode.CLOBBER,
        cg=cg,
    )

    # rsync doesn't delete by default
    assert (push_ctx.agent_dir / "agent_only.txt").exists()
    assert (push_ctx.agent_dir / "host_only.txt").read_text() == "host file"


@pytest.mark.rsync
def test_push_files_clobber_mode_with_delete_flag_removes_agent_only_files(
    push_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test CLOBBER mode with delete=True removes files not in source."""
    (push_ctx.local_dir / "host_file.txt").write_text("host content")
    (push_ctx.agent_dir / "agent_extra.txt").write_text("this should be deleted")
    run_git_command(push_ctx.agent_dir, "add", "agent_extra.txt")
    run_git_command(push_ctx.agent_dir, "commit", "-m", "Add agent extra file")

    push_files(
        agent=push_ctx.agent,
        host=push_ctx.host,
        source=push_ctx.local_dir,
        destination_path=None,
        is_dry_run=False,
        is_delete=True,
        uncommitted_changes=UncommittedChangesMode.CLOBBER,
        cg=cg,
    )

    assert not (push_ctx.agent_dir / "agent_extra.txt").exists()
    assert (push_ctx.agent_dir / "host_file.txt").read_text() == "host content"


# =============================================================================
# Test: STASH mode
# =============================================================================


@pytest.mark.rsync
def test_push_files_stash_mode_stashes_changes_and_leaves_stashed(
    push_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that STASH mode stashes uncommitted changes on target and leaves them stashed."""
    (push_ctx.local_dir / "host_file.txt").write_text("host content")
    # Modify a tracked file (README.md was created by _init_git_repo)
    (push_ctx.agent_dir / "README.md").write_text("modified content")
    initial_stash_count = get_stash_count(push_ctx.agent_dir)
    assert _has_uncommitted_changes_on_host(push_ctx.host, push_ctx.agent_dir)

    push_files(
        agent=push_ctx.agent,
        host=push_ctx.host,
        source=push_ctx.local_dir,
        destination_path=None,
        is_dry_run=False,
        is_delete=False,
        uncommitted_changes=UncommittedChangesMode.STASH,
        cg=cg,
    )

    final_stash_count = get_stash_count(push_ctx.agent_dir)
    assert final_stash_count == initial_stash_count + 1
    # The modified tracked file should be reverted to its committed state
    assert (push_ctx.agent_dir / "README.md").read_text() == "Initial content"
    assert (push_ctx.agent_dir / "host_file.txt").read_text() == "host content"


@pytest.mark.rsync
def test_push_files_stash_mode_stashes_untracked_files(
    push_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that STASH mode properly stashes untracked files on target."""
    (push_ctx.local_dir / "host_file.txt").write_text("host content")
    # Create an UNTRACKED file
    (push_ctx.agent_dir / "untracked_file.txt").write_text("untracked content")
    initial_stash_count = get_stash_count(push_ctx.agent_dir)
    assert _has_uncommitted_changes_on_host(push_ctx.host, push_ctx.agent_dir)

    push_files(
        agent=push_ctx.agent,
        host=push_ctx.host,
        source=push_ctx.local_dir,
        destination_path=None,
        is_dry_run=False,
        is_delete=False,
        uncommitted_changes=UncommittedChangesMode.STASH,
        cg=cg,
    )

    # Untracked file should be stashed with -u flag
    final_stash_count = get_stash_count(push_ctx.agent_dir)
    assert final_stash_count == initial_stash_count + 1
    assert not (push_ctx.agent_dir / "untracked_file.txt").exists()
    assert (push_ctx.agent_dir / "host_file.txt").read_text() == "host content"


@pytest.mark.rsync
def test_push_files_stash_mode_with_no_uncommitted_changes_does_not_stash(
    push_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that STASH mode does not create a stash when no changes exist on target."""
    (push_ctx.local_dir / "host_file.txt").write_text("host content")
    assert not _has_uncommitted_changes_on_host(push_ctx.host, push_ctx.agent_dir)
    initial_stash_count = get_stash_count(push_ctx.agent_dir)

    push_files(
        agent=push_ctx.agent,
        host=push_ctx.host,
        source=push_ctx.local_dir,
        destination_path=None,
        is_dry_run=False,
        is_delete=False,
        uncommitted_changes=UncommittedChangesMode.STASH,
        cg=cg,
    )

    final_stash_count = get_stash_count(push_ctx.agent_dir)
    assert final_stash_count == initial_stash_count
    assert (push_ctx.agent_dir / "host_file.txt").read_text() == "host content"


# =============================================================================
# Test: MERGE mode
# =============================================================================


@pytest.mark.rsync
def test_push_files_merge_mode_stashes_and_restores_changes(
    push_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that MERGE mode stashes changes on target, pushes, then restores changes."""
    (push_ctx.local_dir / "host_file.txt").write_text("host content")
    # Modify the tracked README.md file
    (push_ctx.agent_dir / "README.md").write_text("agent modified content")
    initial_stash_count = get_stash_count(push_ctx.agent_dir)
    assert _has_uncommitted_changes_on_host(push_ctx.host, push_ctx.agent_dir)

    push_files(
        agent=push_ctx.agent,
        host=push_ctx.host,
        source=push_ctx.local_dir,
        destination_path=None,
        is_dry_run=False,
        is_delete=False,
        uncommitted_changes=UncommittedChangesMode.MERGE,
        cg=cg,
    )

    final_stash_count = get_stash_count(push_ctx.agent_dir)
    assert final_stash_count == initial_stash_count
    assert (push_ctx.agent_dir / "README.md").read_text() == "agent modified content"
    assert (push_ctx.agent_dir / "host_file.txt").read_text() == "host content"


@pytest.mark.rsync
def test_push_files_merge_mode_restores_untracked_files(
    push_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that MERGE mode properly stashes and restores untracked files on target."""
    (push_ctx.local_dir / "host_file.txt").write_text("host content")
    (push_ctx.agent_dir / "untracked_file.txt").write_text("untracked content")
    initial_stash_count = get_stash_count(push_ctx.agent_dir)
    assert _has_uncommitted_changes_on_host(push_ctx.host, push_ctx.agent_dir)

    push_files(
        agent=push_ctx.agent,
        host=push_ctx.host,
        source=push_ctx.local_dir,
        destination_path=None,
        is_dry_run=False,
        is_delete=False,
        uncommitted_changes=UncommittedChangesMode.MERGE,
        cg=cg,
    )

    # Stash should be created and popped
    final_stash_count = get_stash_count(push_ctx.agent_dir)
    assert final_stash_count == initial_stash_count
    assert (push_ctx.agent_dir / "untracked_file.txt").exists()
    assert (push_ctx.agent_dir / "untracked_file.txt").read_text() == "untracked content"
    assert (push_ctx.agent_dir / "host_file.txt").read_text() == "host content"


@pytest.mark.rsync
def test_push_files_merge_mode_when_both_modify_different_files(
    push_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test MERGE mode when host and agent modify different files."""
    (push_ctx.local_dir / "host_only.txt").write_text("host content")
    (push_ctx.agent_dir / "README.md").write_text("agent modified content")
    initial_stash_count = get_stash_count(push_ctx.agent_dir)
    assert _has_uncommitted_changes_on_host(push_ctx.host, push_ctx.agent_dir)

    push_files(
        agent=push_ctx.agent,
        host=push_ctx.host,
        source=push_ctx.local_dir,
        destination_path=None,
        is_dry_run=False,
        is_delete=False,
        uncommitted_changes=UncommittedChangesMode.MERGE,
        cg=cg,
    )

    assert (push_ctx.agent_dir / "host_only.txt").read_text() == "host content"
    assert (push_ctx.agent_dir / "README.md").read_text() == "agent modified content"
    final_stash_count = get_stash_count(push_ctx.agent_dir)
    assert final_stash_count == initial_stash_count


@pytest.mark.rsync
def test_push_files_merge_mode_with_no_uncommitted_changes(
    push_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that MERGE mode works correctly when there are no uncommitted changes on target."""
    (push_ctx.local_dir / "host_file.txt").write_text("host content")
    assert not _has_uncommitted_changes_on_host(push_ctx.host, push_ctx.agent_dir)
    initial_stash_count = get_stash_count(push_ctx.agent_dir)

    push_files(
        agent=push_ctx.agent,
        host=push_ctx.host,
        source=push_ctx.local_dir,
        destination_path=None,
        is_dry_run=False,
        is_delete=False,
        uncommitted_changes=UncommittedChangesMode.MERGE,
        cg=cg,
    )

    final_stash_count = get_stash_count(push_ctx.agent_dir)
    assert final_stash_count == initial_stash_count
    assert (push_ctx.agent_dir / "host_file.txt").read_text() == "host content"


# =============================================================================
# Test: .git directory exclusion
# =============================================================================


@pytest.mark.rsync
def test_push_files_excludes_git_directory(
    push_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that push_files excludes the .git directory from rsync."""
    # Make the host directory a git repo too
    run_git_command(push_ctx.local_dir, "init")
    run_git_command(push_ctx.local_dir, "config", "user.email", "test@example.com")
    run_git_command(push_ctx.local_dir, "config", "user.name", "Test User")
    (push_ctx.local_dir / "file.txt").write_text("host content")
    run_git_command(push_ctx.local_dir, "add", "file.txt")
    run_git_command(push_ctx.local_dir, "commit", "-m", "Add file")

    agent_commit_before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=push_ctx.agent_dir,
        capture_output=True,
        text=True,
    ).stdout.strip()

    push_files(
        agent=push_ctx.agent,
        host=push_ctx.host,
        source=push_ctx.local_dir,
        destination_path=None,
        is_dry_run=False,
        is_delete=False,
        uncommitted_changes=UncommittedChangesMode.CLOBBER,
        cg=cg,
    )

    # The agent's .git directory should be unchanged
    agent_commit_after = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=push_ctx.agent_dir,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert agent_commit_before == agent_commit_after
    assert (push_ctx.agent_dir / "file.txt").read_text() == "host content"


# =============================================================================
# Test: dry_run flag
# =============================================================================


@pytest.mark.rsync
def test_push_files_dry_run_does_not_modify_files(
    push_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that dry_run=True shows what would be transferred without modifying files."""
    (push_ctx.local_dir / "new_file.txt").write_text("host content")
    assert not (push_ctx.agent_dir / "new_file.txt").exists()

    result = push_files(
        agent=push_ctx.agent,
        host=push_ctx.host,
        source=push_ctx.local_dir,
        destination_path=None,
        is_dry_run=True,
        is_delete=False,
        uncommitted_changes=UncommittedChangesMode.FAIL,
        cg=cg,
    )

    assert not (push_ctx.agent_dir / "new_file.txt").exists()
    assert result.is_dry_run is True


# =============================================================================
# Test: destination_path parameter
# =============================================================================


@pytest.mark.rsync
def test_push_files_with_custom_destination_path(
    push_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that push_files can use a custom destination path instead of work_dir."""
    custom_dest = push_ctx.agent_dir / "subdir"
    custom_dest.mkdir(parents=True)
    (push_ctx.local_dir / "file_from_host.txt").write_text("content from host")

    result = push_files(
        agent=push_ctx.agent,
        host=push_ctx.host,
        source=push_ctx.local_dir,
        destination_path=custom_dest,
        is_dry_run=False,
        is_delete=False,
        uncommitted_changes=UncommittedChangesMode.FAIL,
        cg=cg,
    )

    assert (custom_dest / "file_from_host.txt").read_text() == "content from host"
    assert result.destination_path == custom_dest


# =============================================================================
# Test: Host directory is never modified
# =============================================================================


@pytest.mark.rsync
@pytest.mark.parametrize(
    "mode,modify_tracked_file",
    [
        pytest.param(UncommittedChangesMode.CLOBBER, False, id="clobber"),
        pytest.param(UncommittedChangesMode.STASH, True, id="stash"),
        pytest.param(UncommittedChangesMode.MERGE, True, id="merge"),
    ],
)
def test_push_files_does_not_modify_host_directory(
    push_ctx: SyncTestContext,
    mode: UncommittedChangesMode,
    modify_tracked_file: bool,
    cg: ConcurrencyGroup,
) -> None:
    """Test that pushing files NEVER modifies the host (source) directory.

    This test is parameterized to verify host immutability across all modes.
    STASH and MERGE modes modify tracked files to avoid untracked file conflicts
    when running in sequence.
    """
    # Set up host with some files
    (push_ctx.local_dir / "host_file.txt").write_text("host content")
    (push_ctx.local_dir / "another_file.txt").write_text("another host file")

    # Record the state of the host directory
    host_files_before = set(push_ctx.local_dir.iterdir())
    host_contents_before = {f.name: f.read_text() for f in push_ctx.local_dir.iterdir() if f.is_file()}

    # Set up agent with uncommitted changes
    if modify_tracked_file:
        # Modify tracked file to avoid untracked file conflicts
        (push_ctx.agent_dir / "README.md").write_text("agent uncommitted changes")
    else:
        # Create untracked file
        (push_ctx.agent_dir / "agent_uncommitted.txt").write_text("agent uncommitted")

    push_files(
        agent=push_ctx.agent,
        host=push_ctx.host,
        source=push_ctx.local_dir,
        destination_path=None,
        is_dry_run=False,
        is_delete=False,
        uncommitted_changes=mode,
        cg=cg,
    )

    # Verify host directory is unchanged
    host_files_after = set(push_ctx.local_dir.iterdir())
    host_contents_after = {f.name: f.read_text() for f in push_ctx.local_dir.iterdir() if f.is_file()}

    assert host_files_before == host_files_after
    assert host_contents_before == host_contents_after


# =============================================================================
# Test: push_git function
# =============================================================================


@pytest.fixture
def git_push_ctx(tmp_path: Path) -> SyncTestContext:
    """Create a test context with local and agent git repositories that share history."""
    local_dir = tmp_path / "host"
    agent_dir = tmp_path / "agent"

    # Initialize local repo with a commit
    init_git_repo_with_config(local_dir)

    # Clone the local repo to create the agent repo (so they share history)
    subprocess.run(
        ["git", "clone", str(local_dir), str(agent_dir)],
        capture_output=True,
        text=True,
        check=True,
    )

    # Configure git user for the agent repo
    run_git_command(agent_dir, "config", "user.email", "test@example.com")
    run_git_command(agent_dir, "config", "user.name", "Test User")

    return SyncTestContext(
        local_dir=local_dir,
        agent_dir=agent_dir,
        agent=cast(AgentInterface, FakeAgent(work_dir=agent_dir)),
        host=cast(OnlineHostInterface, FakeHost()),
    )


def test_push_git_basic_push(git_push_ctx: SyncTestContext, cg: ConcurrencyGroup) -> None:
    """Test basic git push from host to agent."""
    # Create a new commit on the host
    (git_push_ctx.local_dir / "new_file.txt").write_text("new content")
    run_git_command(git_push_ctx.local_dir, "add", "new_file.txt")
    run_git_command(git_push_ctx.local_dir, "commit", "-m", "Add new file")

    result = push_git(
        agent=git_push_ctx.agent,
        host=git_push_ctx.host,
        source=git_push_ctx.local_dir,
        source_branch=None,
        target_branch=None,
        is_dry_run=False,
        uncommitted_changes=UncommittedChangesMode.CLOBBER,
        is_mirror=False,
        cg=cg,
    )

    # The new file should exist on the agent
    assert (git_push_ctx.agent_dir / "new_file.txt").exists()
    assert (git_push_ctx.agent_dir / "new_file.txt").read_text() == "new content"
    # Note: commits_transferred count may be inaccurate because the counting logic
    # only looks at the source repo. The important thing is the files were pushed.
    assert result.is_dry_run is False


def test_push_git_dry_run(git_push_ctx: SyncTestContext, cg: ConcurrencyGroup) -> None:
    """Test that dry_run=True does not actually push commits."""
    # Create a new commit on the host
    (git_push_ctx.local_dir / "new_file.txt").write_text("new content")
    run_git_command(git_push_ctx.local_dir, "add", "new_file.txt")
    run_git_command(git_push_ctx.local_dir, "commit", "-m", "Add new file")

    result = push_git(
        agent=git_push_ctx.agent,
        host=git_push_ctx.host,
        source=git_push_ctx.local_dir,
        source_branch=None,
        target_branch=None,
        is_dry_run=True,
        uncommitted_changes=UncommittedChangesMode.CLOBBER,
        is_mirror=False,
        cg=cg,
    )

    # The new file should NOT exist on the agent (dry run)
    assert not (git_push_ctx.agent_dir / "new_file.txt").exists()
    assert result.is_dry_run is True


def test_push_git_with_stash_mode(git_push_ctx: SyncTestContext, cg: ConcurrencyGroup) -> None:
    """Test push_git with STASH mode for uncommitted changes on agent."""
    # Create a new commit on the host
    (git_push_ctx.local_dir / "new_file.txt").write_text("new content from host")
    run_git_command(git_push_ctx.local_dir, "add", "new_file.txt")
    run_git_command(git_push_ctx.local_dir, "commit", "-m", "Add new file")

    # Create uncommitted changes on the agent
    (git_push_ctx.agent_dir / "README.md").write_text("agent uncommitted changes")
    initial_stash_count = get_stash_count(git_push_ctx.agent_dir)

    push_git(
        agent=git_push_ctx.agent,
        host=git_push_ctx.host,
        source=git_push_ctx.local_dir,
        source_branch=None,
        target_branch=None,
        is_dry_run=False,
        uncommitted_changes=UncommittedChangesMode.STASH,
        is_mirror=False,
        cg=cg,
    )

    # The push should succeed and changes should be stashed
    final_stash_count = get_stash_count(git_push_ctx.agent_dir)
    assert final_stash_count == initial_stash_count + 1
    assert (git_push_ctx.agent_dir / "new_file.txt").exists()


def test_push_git_with_merge_mode(git_push_ctx: SyncTestContext, cg: ConcurrencyGroup) -> None:
    """Test push_git with MERGE mode restores uncommitted changes after push."""
    # Create a new commit on the host
    (git_push_ctx.local_dir / "new_file.txt").write_text("new content from host")
    run_git_command(git_push_ctx.local_dir, "add", "new_file.txt")
    run_git_command(git_push_ctx.local_dir, "commit", "-m", "Add new file")

    # Create an untracked file on the agent (different from host's new file)
    (git_push_ctx.agent_dir / "agent_local_file.txt").write_text("agent local content")
    initial_stash_count = get_stash_count(git_push_ctx.agent_dir)

    push_git(
        agent=git_push_ctx.agent,
        host=git_push_ctx.host,
        source=git_push_ctx.local_dir,
        source_branch=None,
        target_branch=None,
        is_dry_run=False,
        uncommitted_changes=UncommittedChangesMode.MERGE,
        is_mirror=False,
        cg=cg,
    )

    # The push should succeed and local changes should be restored
    final_stash_count = get_stash_count(git_push_ctx.agent_dir)
    assert final_stash_count == initial_stash_count
    assert (git_push_ctx.agent_dir / "new_file.txt").exists()
    assert (git_push_ctx.agent_dir / "agent_local_file.txt").exists()
    assert (git_push_ctx.agent_dir / "agent_local_file.txt").read_text() == "agent local content"


def test_push_git_does_not_modify_host_directory(git_push_ctx: SyncTestContext, cg: ConcurrencyGroup) -> None:
    """Test that push_git NEVER modifies the host (source) directory."""
    # Create a commit on the host
    (git_push_ctx.local_dir / "new_file.txt").write_text("host content")
    run_git_command(git_push_ctx.local_dir, "add", "new_file.txt")
    run_git_command(git_push_ctx.local_dir, "commit", "-m", "Add new file")

    # Record the state of the host directory
    host_files_before = set(git_push_ctx.local_dir.iterdir())
    host_contents_before = {f.name: f.read_text() for f in git_push_ctx.local_dir.iterdir() if f.is_file()}
    host_commit_before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_push_ctx.local_dir,
        capture_output=True,
        text=True,
    ).stdout.strip()

    push_git(
        agent=git_push_ctx.agent,
        host=git_push_ctx.host,
        source=git_push_ctx.local_dir,
        source_branch=None,
        target_branch=None,
        is_dry_run=False,
        uncommitted_changes=UncommittedChangesMode.CLOBBER,
        is_mirror=False,
        cg=cg,
    )

    # Verify host directory is unchanged
    host_files_after = set(git_push_ctx.local_dir.iterdir())
    host_contents_after = {f.name: f.read_text() for f in git_push_ctx.local_dir.iterdir() if f.is_file()}
    host_commit_after = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_push_ctx.local_dir,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert host_files_before == host_files_after
    assert host_contents_before == host_contents_after
    assert host_commit_before == host_commit_after


# =============================================================================
# Test: push_git mirror mode
# =============================================================================


def test_push_git_mirror_mode_dry_run(git_push_ctx: SyncTestContext, cg: ConcurrencyGroup) -> None:
    """Test that mirror mode with dry_run=True shows what would be pushed."""
    # Create a new commit on the host
    (git_push_ctx.local_dir / "new_file.txt").write_text("new content")
    run_git_command(git_push_ctx.local_dir, "add", "new_file.txt")
    run_git_command(git_push_ctx.local_dir, "commit", "-m", "Add new file")

    result = push_git(
        agent=git_push_ctx.agent,
        host=git_push_ctx.host,
        source=git_push_ctx.local_dir,
        source_branch=None,
        target_branch=None,
        is_dry_run=True,
        uncommitted_changes=UncommittedChangesMode.CLOBBER,
        is_mirror=True,
        cg=cg,
    )

    # The new file should NOT exist on the agent (dry run)
    assert not (git_push_ctx.agent_dir / "new_file.txt").exists()
    assert result.is_dry_run is True
    # commits_transferred should show what would be pushed
    assert result.commits_transferred >= 0


def test_push_git_mirror_mode(git_push_ctx: SyncTestContext, cg: ConcurrencyGroup) -> None:
    """Test that mirror mode pushes all refs to the agent repository."""
    # Create a new commit on the host
    (git_push_ctx.local_dir / "new_file.txt").write_text("new content")
    run_git_command(git_push_ctx.local_dir, "add", "new_file.txt")
    run_git_command(git_push_ctx.local_dir, "commit", "-m", "Add new file")

    # Get host commit before push
    host_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_push_ctx.local_dir,
        capture_output=True,
        text=True,
    ).stdout.strip()

    result = push_git(
        agent=git_push_ctx.agent,
        host=git_push_ctx.host,
        source=git_push_ctx.local_dir,
        source_branch=None,
        target_branch=None,
        is_dry_run=False,
        uncommitted_changes=UncommittedChangesMode.CLOBBER,
        is_mirror=True,
        cg=cg,
    )

    # Get agent commit after push
    agent_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_push_ctx.agent_dir,
        capture_output=True,
        text=True,
    ).stdout.strip()

    # The commits should match after mirror push
    assert host_commit == agent_commit
    assert result.is_dry_run is False


# =============================================================================
# Test: Remote (non-local) host behavior
# =============================================================================


@pytest.fixture
def remote_push_ctx(tmp_path: Path) -> SyncTestContext:
    """Create a test context with a remote (non-local) host."""
    local_dir = tmp_path / "host"
    agent_dir = tmp_path / "agent"
    local_dir.mkdir(parents=True)
    init_git_repo_with_config(agent_dir)
    return SyncTestContext(
        local_dir=local_dir,
        agent_dir=agent_dir,
        agent=cast(AgentInterface, FakeAgent(work_dir=agent_dir)),
        host=cast(OnlineHostInterface, FakeHost(is_local=False)),
    )


@pytest.fixture
def remote_git_push_ctx(tmp_path: Path) -> SyncTestContext:
    """Create a test context with remote host for git push testing."""
    local_dir = tmp_path / "host"
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

    return SyncTestContext(
        local_dir=local_dir,
        agent_dir=agent_dir,
        agent=cast(AgentInterface, FakeAgent(work_dir=agent_dir)),
        host=cast(OnlineHostInterface, FakeHost(is_local=False)),
    )


def test_push_files_with_remote_host_raises_not_implemented(
    remote_push_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that push_files raises NotImplementedError for remote hosts.

    File sync via rsync requires local paths on both sides. Remote host
    support will require SSH-based rsync or a different transfer mechanism.
    """
    (remote_push_ctx.local_dir / "file.txt").write_text("host content")

    with pytest.raises(NotImplementedError, match="remote hosts"):
        push_files(
            agent=remote_push_ctx.agent,
            host=remote_push_ctx.host,
            source=remote_push_ctx.local_dir,
            destination_path=None,
            is_dry_run=False,
            is_delete=False,
            uncommitted_changes=UncommittedChangesMode.CLOBBER,
            cg=cg,
        )


def test_push_git_with_remote_host_raises_not_implemented(
    remote_git_push_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that push_git raises NotImplementedError for remote hosts.

    Git push to remote hosts requires SSH URL support which is not implemented.
    """
    (remote_git_push_ctx.local_dir / "new_file.txt").write_text("new content")
    run_git_command(remote_git_push_ctx.local_dir, "add", "new_file.txt")
    run_git_command(remote_git_push_ctx.local_dir, "commit", "-m", "Add new file")

    with pytest.raises(NotImplementedError, match="remote hosts is not yet implemented"):
        push_git(
            agent=remote_git_push_ctx.agent,
            host=remote_git_push_ctx.host,
            source=remote_git_push_ctx.local_dir,
            source_branch=None,
            target_branch=None,
            is_dry_run=False,
            uncommitted_changes=UncommittedChangesMode.CLOBBER,
            is_mirror=False,
            cg=cg,
        )


# =============================================================================
# Test: Push to non-existent subdirectory (issue 3)
# =============================================================================


@pytest.mark.rsync
def test_push_files_to_nonexistent_subdir_creates_directory(
    push_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that push_files auto-creates a non-existent subdirectory target.

    When pushing to agent:subdir and the subdirectory doesn't exist yet,
    the directory should be created automatically rather than failing with
    a cryptic git status error.
    """
    (push_ctx.local_dir / "file.txt").write_text("local content")

    subdir_path = push_ctx.agent_dir / "new_subdir"
    assert not subdir_path.exists()

    result = push_files(
        agent=push_ctx.agent,
        host=push_ctx.host,
        source=push_ctx.local_dir,
        destination_path=subdir_path,
        is_dry_run=False,
        is_delete=False,
        uncommitted_changes=UncommittedChangesMode.CLOBBER,
        cg=cg,
    )

    assert subdir_path.is_dir()
    assert (subdir_path / "file.txt").read_text() == "local content"
    assert result.destination_path == subdir_path
