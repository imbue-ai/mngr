import subprocess
from pathlib import Path
from typing import cast

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mng.api.pull import pull_files
from imbue.mng.api.pull import pull_git
from imbue.mng.api.sync import GitSyncError
from imbue.mng.api.sync import UncommittedChangesError
from imbue.mng.api.test_fixtures import FakeAgent
from imbue.mng.api.test_fixtures import FakeHost
from imbue.mng.api.test_fixtures import SyncTestContext
from imbue.mng.api.test_fixtures import has_uncommitted_changes
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.host import HostInterface
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import UncommittedChangesMode
from imbue.mng.utils.git_utils import get_current_branch
from imbue.mng.utils.testing import get_stash_count
from imbue.mng.utils.testing import init_git_repo_with_config
from imbue.mng.utils.testing import run_git_command


@pytest.fixture
def pull_ctx(tmp_path: Path) -> SyncTestContext:
    """Create a test context with agent and host directories."""
    agent_dir = tmp_path / "agent"
    local_dir = tmp_path / "host"
    agent_dir.mkdir(parents=True)
    init_git_repo_with_config(local_dir)
    return SyncTestContext(
        agent_dir=agent_dir,
        local_dir=local_dir,
        agent=cast(AgentInterface, FakeAgent(work_dir=agent_dir)),
        host=cast(HostInterface, FakeHost()),
    )


# =============================================================================
# Test: FAIL mode (default)
# =============================================================================


@pytest.mark.rsync
def test_pull_files_fail_mode_with_no_uncommitted_changes_succeeds(
    pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that FAIL mode succeeds when there are no uncommitted changes."""
    (pull_ctx.agent_dir / "file.txt").write_text("agent content")
    assert not has_uncommitted_changes(pull_ctx.local_dir, cg)

    result = pull_files(
        agent=pull_ctx.agent,
        host=pull_ctx.host,
        destination=pull_ctx.local_dir,
        source_path=None,
        is_dry_run=False,
        is_delete=False,
        uncommitted_changes=UncommittedChangesMode.FAIL,
        cg=cg,
    )

    assert (pull_ctx.local_dir / "file.txt").exists()
    assert (pull_ctx.local_dir / "file.txt").read_text() == "agent content"
    assert result.destination_path == pull_ctx.local_dir
    assert result.source_path == pull_ctx.agent_dir


def test_pull_files_fail_mode_with_uncommitted_changes_raises_error(
    pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that FAIL mode raises UncommittedChangesError when changes exist."""
    (pull_ctx.agent_dir / "file.txt").write_text("agent content")
    (pull_ctx.local_dir / "uncommitted.txt").write_text("uncommitted content")
    assert has_uncommitted_changes(pull_ctx.local_dir, cg)

    with pytest.raises(UncommittedChangesError) as exc_info:
        pull_files(
            agent=pull_ctx.agent,
            host=pull_ctx.host,
            destination=pull_ctx.local_dir,
            source_path=None,
            is_dry_run=False,
            is_delete=False,
            uncommitted_changes=UncommittedChangesMode.FAIL,
            cg=cg,
        )

    assert exc_info.value.destination == pull_ctx.local_dir


# =============================================================================
# Test: CLOBBER mode
# =============================================================================


@pytest.mark.rsync
def test_pull_files_clobber_mode_overwrites_host_changes(
    pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that CLOBBER mode overwrites uncommitted changes in the host."""
    (pull_ctx.agent_dir / "shared.txt").write_text("agent version")
    (pull_ctx.local_dir / "shared.txt").write_text("host version")
    assert has_uncommitted_changes(pull_ctx.local_dir, cg)

    result = pull_files(
        agent=pull_ctx.agent,
        host=pull_ctx.host,
        destination=pull_ctx.local_dir,
        source_path=None,
        is_dry_run=False,
        is_delete=False,
        uncommitted_changes=UncommittedChangesMode.CLOBBER,
        cg=cg,
    )

    assert (pull_ctx.local_dir / "shared.txt").read_text() == "agent version"
    assert result.destination_path == pull_ctx.local_dir


@pytest.mark.rsync
def test_pull_files_clobber_mode_when_only_host_has_changes(
    pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test CLOBBER mode when only the host has a modified file."""
    (pull_ctx.agent_dir / "agent_only.txt").write_text("agent file")
    (pull_ctx.local_dir / "host_only.txt").write_text("host uncommitted content")
    assert has_uncommitted_changes(pull_ctx.local_dir, cg)

    pull_files(
        agent=pull_ctx.agent,
        host=pull_ctx.host,
        destination=pull_ctx.local_dir,
        source_path=None,
        is_dry_run=False,
        is_delete=False,
        uncommitted_changes=UncommittedChangesMode.CLOBBER,
        cg=cg,
    )

    # rsync doesn't delete by default
    assert (pull_ctx.local_dir / "host_only.txt").exists()
    assert (pull_ctx.local_dir / "agent_only.txt").read_text() == "agent file"


@pytest.mark.rsync
def test_pull_files_clobber_mode_with_delete_flag_removes_host_only_files(
    pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test CLOBBER mode with delete=True removes files not in agent."""
    (pull_ctx.agent_dir / "agent_file.txt").write_text("agent content")
    (pull_ctx.local_dir / "host_extra.txt").write_text("this should be deleted")
    run_git_command(pull_ctx.local_dir, "add", "host_extra.txt")
    run_git_command(pull_ctx.local_dir, "commit", "-m", "Add host extra file")

    pull_files(
        agent=pull_ctx.agent,
        host=pull_ctx.host,
        destination=pull_ctx.local_dir,
        source_path=None,
        is_dry_run=False,
        is_delete=True,
        uncommitted_changes=UncommittedChangesMode.CLOBBER,
        cg=cg,
    )

    assert not (pull_ctx.local_dir / "host_extra.txt").exists()
    assert (pull_ctx.local_dir / "agent_file.txt").read_text() == "agent content"


# =============================================================================
# Test: STASH mode
# =============================================================================


@pytest.mark.rsync
def test_pull_files_stash_mode_stashes_changes_and_leaves_stashed(
    pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that STASH mode stashes uncommitted changes and leaves them stashed."""
    (pull_ctx.agent_dir / "agent_file.txt").write_text("agent content")
    # Modify a tracked file (README.md was created by _init_git_repo)
    (pull_ctx.local_dir / "README.md").write_text("modified content")
    initial_stash_count = get_stash_count(pull_ctx.local_dir)
    assert has_uncommitted_changes(pull_ctx.local_dir, cg)

    pull_files(
        agent=pull_ctx.agent,
        host=pull_ctx.host,
        destination=pull_ctx.local_dir,
        source_path=None,
        is_dry_run=False,
        is_delete=False,
        uncommitted_changes=UncommittedChangesMode.STASH,
        cg=cg,
    )

    final_stash_count = get_stash_count(pull_ctx.local_dir)
    assert final_stash_count == initial_stash_count + 1
    # The modified tracked file should be reverted to its committed state
    assert (pull_ctx.local_dir / "README.md").read_text() == "Initial content"
    assert (pull_ctx.local_dir / "agent_file.txt").read_text() == "agent content"


@pytest.mark.rsync
def test_pull_files_stash_mode_when_both_agent_and_host_modify_same_file(
    pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test STASH mode when both agent and host have modified the same file."""
    # Add and commit a shared file in host
    (pull_ctx.local_dir / "shared.txt").write_text("original content")
    run_git_command(pull_ctx.local_dir, "add", "shared.txt")
    run_git_command(pull_ctx.local_dir, "commit", "-m", "Add shared file")

    # Modify the shared file (uncommitted change to a tracked file)
    (pull_ctx.local_dir / "shared.txt").write_text("host version of shared")
    assert has_uncommitted_changes(pull_ctx.local_dir, cg)

    # Agent has a different version
    (pull_ctx.agent_dir / "shared.txt").write_text("agent version of shared")

    pull_files(
        agent=pull_ctx.agent,
        host=pull_ctx.host,
        destination=pull_ctx.local_dir,
        source_path=None,
        is_dry_run=False,
        is_delete=False,
        uncommitted_changes=UncommittedChangesMode.STASH,
        cg=cg,
    )

    assert (pull_ctx.local_dir / "shared.txt").read_text() == "agent version of shared"
    assert get_stash_count(pull_ctx.local_dir) == 1


@pytest.mark.rsync
def test_pull_files_stash_mode_stashes_untracked_files(
    pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that STASH mode properly stashes untracked files (not just tracked modifications)."""
    (pull_ctx.agent_dir / "agent_file.txt").write_text("agent content")
    # Create an UNTRACKED file (git status --porcelain includes these)
    (pull_ctx.local_dir / "untracked_file.txt").write_text("untracked content")
    initial_stash_count = get_stash_count(pull_ctx.local_dir)
    assert has_uncommitted_changes(pull_ctx.local_dir, cg)

    pull_files(
        agent=pull_ctx.agent,
        host=pull_ctx.host,
        destination=pull_ctx.local_dir,
        source_path=None,
        is_dry_run=False,
        is_delete=False,
        uncommitted_changes=UncommittedChangesMode.STASH,
        cg=cg,
    )

    # Untracked file should be stashed with -u flag
    final_stash_count = get_stash_count(pull_ctx.local_dir)
    assert final_stash_count == initial_stash_count + 1
    assert not (pull_ctx.local_dir / "untracked_file.txt").exists()
    assert (pull_ctx.local_dir / "agent_file.txt").read_text() == "agent content"


@pytest.mark.rsync
def test_pull_files_merge_mode_restores_untracked_files(
    pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that MERGE mode properly stashes and restores untracked files."""
    (pull_ctx.agent_dir / "agent_file.txt").write_text("agent content")
    (pull_ctx.local_dir / "untracked_file.txt").write_text("untracked content")
    initial_stash_count = get_stash_count(pull_ctx.local_dir)
    assert has_uncommitted_changes(pull_ctx.local_dir, cg)

    pull_files(
        agent=pull_ctx.agent,
        host=pull_ctx.host,
        destination=pull_ctx.local_dir,
        source_path=None,
        is_dry_run=False,
        is_delete=False,
        uncommitted_changes=UncommittedChangesMode.MERGE,
        cg=cg,
    )

    # Stash should be created and popped
    final_stash_count = get_stash_count(pull_ctx.local_dir)
    assert final_stash_count == initial_stash_count
    assert (pull_ctx.local_dir / "untracked_file.txt").exists()
    assert (pull_ctx.local_dir / "untracked_file.txt").read_text() == "untracked content"
    assert (pull_ctx.local_dir / "agent_file.txt").read_text() == "agent content"


@pytest.mark.rsync
def test_pull_files_stash_mode_with_no_uncommitted_changes_does_not_stash(
    pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that STASH mode does not create a stash when no changes exist."""
    (pull_ctx.agent_dir / "agent_file.txt").write_text("agent content")
    assert not has_uncommitted_changes(pull_ctx.local_dir, cg)
    initial_stash_count = get_stash_count(pull_ctx.local_dir)

    pull_files(
        agent=pull_ctx.agent,
        host=pull_ctx.host,
        destination=pull_ctx.local_dir,
        source_path=None,
        is_dry_run=False,
        is_delete=False,
        uncommitted_changes=UncommittedChangesMode.STASH,
        cg=cg,
    )

    final_stash_count = get_stash_count(pull_ctx.local_dir)
    assert final_stash_count == initial_stash_count
    assert (pull_ctx.local_dir / "agent_file.txt").read_text() == "agent content"


# =============================================================================
# Test: MERGE mode
# =============================================================================


@pytest.mark.rsync
def test_pull_files_merge_mode_stashes_and_restores_changes(
    pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that MERGE mode stashes changes, pulls, then restores changes."""
    (pull_ctx.agent_dir / "agent_file.txt").write_text("agent content")
    # Modify the tracked README.md file
    (pull_ctx.local_dir / "README.md").write_text("host modified content")
    initial_stash_count = get_stash_count(pull_ctx.local_dir)
    assert has_uncommitted_changes(pull_ctx.local_dir, cg)

    pull_files(
        agent=pull_ctx.agent,
        host=pull_ctx.host,
        destination=pull_ctx.local_dir,
        source_path=None,
        is_dry_run=False,
        is_delete=False,
        uncommitted_changes=UncommittedChangesMode.MERGE,
        cg=cg,
    )

    final_stash_count = get_stash_count(pull_ctx.local_dir)
    assert final_stash_count == initial_stash_count
    assert (pull_ctx.local_dir / "README.md").read_text() == "host modified content"
    assert (pull_ctx.local_dir / "agent_file.txt").read_text() == "agent content"


@pytest.mark.rsync
def test_pull_files_merge_mode_when_only_agent_file_is_modified(
    pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test MERGE mode when only the agent has changed a file."""
    (pull_ctx.agent_dir / "shared.txt").write_text("agent modified content")
    # Add and commit the file in host first
    (pull_ctx.local_dir / "shared.txt").write_text("original content")
    run_git_command(pull_ctx.local_dir, "add", "shared.txt")
    run_git_command(pull_ctx.local_dir, "commit", "-m", "Add shared file")
    assert not has_uncommitted_changes(pull_ctx.local_dir, cg)

    pull_files(
        agent=pull_ctx.agent,
        host=pull_ctx.host,
        destination=pull_ctx.local_dir,
        source_path=None,
        is_dry_run=False,
        is_delete=False,
        uncommitted_changes=UncommittedChangesMode.MERGE,
        cg=cg,
    )

    assert (pull_ctx.local_dir / "shared.txt").read_text() == "agent modified content"


@pytest.mark.rsync
def test_pull_files_merge_mode_when_only_host_has_changes(
    pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test MERGE mode when only the host has uncommitted changes."""
    (pull_ctx.agent_dir / "agent_file.txt").write_text("agent content")
    (pull_ctx.local_dir / "README.md").write_text("host modified content")
    assert has_uncommitted_changes(pull_ctx.local_dir, cg)

    pull_files(
        agent=pull_ctx.agent,
        host=pull_ctx.host,
        destination=pull_ctx.local_dir,
        source_path=None,
        is_dry_run=False,
        is_delete=False,
        uncommitted_changes=UncommittedChangesMode.MERGE,
        cg=cg,
    )

    assert (pull_ctx.local_dir / "README.md").read_text() == "host modified content"
    assert (pull_ctx.local_dir / "agent_file.txt").read_text() == "agent content"


@pytest.mark.rsync
def test_pull_files_merge_mode_when_both_modify_different_files(
    pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test MERGE mode when agent and host modify different files."""
    (pull_ctx.agent_dir / "agent_only.txt").write_text("agent content")
    (pull_ctx.local_dir / "README.md").write_text("host modified content")
    initial_stash_count = get_stash_count(pull_ctx.local_dir)
    assert has_uncommitted_changes(pull_ctx.local_dir, cg)

    pull_files(
        agent=pull_ctx.agent,
        host=pull_ctx.host,
        destination=pull_ctx.local_dir,
        source_path=None,
        is_dry_run=False,
        is_delete=False,
        uncommitted_changes=UncommittedChangesMode.MERGE,
        cg=cg,
    )

    assert (pull_ctx.local_dir / "agent_only.txt").read_text() == "agent content"
    assert (pull_ctx.local_dir / "README.md").read_text() == "host modified content"
    final_stash_count = get_stash_count(pull_ctx.local_dir)
    assert final_stash_count == initial_stash_count


@pytest.mark.rsync
def test_pull_files_merge_mode_with_no_uncommitted_changes(
    pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that MERGE mode works correctly when there are no uncommitted changes."""
    (pull_ctx.agent_dir / "agent_file.txt").write_text("agent content")
    assert not has_uncommitted_changes(pull_ctx.local_dir, cg)
    initial_stash_count = get_stash_count(pull_ctx.local_dir)

    pull_files(
        agent=pull_ctx.agent,
        host=pull_ctx.host,
        destination=pull_ctx.local_dir,
        source_path=None,
        is_dry_run=False,
        is_delete=False,
        uncommitted_changes=UncommittedChangesMode.MERGE,
        cg=cg,
    )

    final_stash_count = get_stash_count(pull_ctx.local_dir)
    assert final_stash_count == initial_stash_count
    assert (pull_ctx.local_dir / "agent_file.txt").read_text() == "agent content"


# =============================================================================
# Test: .git directory exclusion
# =============================================================================


@pytest.mark.rsync
def test_pull_files_excludes_git_directory(
    pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that pull_files excludes the .git directory from rsync."""
    # Make the agent directory a git repo too
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

    pull_files(
        agent=pull_ctx.agent,
        host=pull_ctx.host,
        destination=pull_ctx.local_dir,
        source_path=None,
        is_dry_run=False,
        is_delete=False,
        uncommitted_changes=UncommittedChangesMode.CLOBBER,
        cg=cg,
    )

    # The host's .git directory should be unchanged
    host_commit_after = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=pull_ctx.local_dir,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert host_commit_before == host_commit_after
    assert (pull_ctx.local_dir / "file.txt").read_text() == "agent content"


# =============================================================================
# Test: dry_run flag
# =============================================================================


@pytest.mark.rsync
def test_pull_files_dry_run_does_not_modify_files(
    pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that dry_run=True shows what would be transferred without modifying files."""
    (pull_ctx.agent_dir / "new_file.txt").write_text("agent content")
    assert not (pull_ctx.local_dir / "new_file.txt").exists()

    result = pull_files(
        agent=pull_ctx.agent,
        host=pull_ctx.host,
        destination=pull_ctx.local_dir,
        source_path=None,
        is_dry_run=True,
        is_delete=False,
        uncommitted_changes=UncommittedChangesMode.FAIL,
        cg=cg,
    )

    assert not (pull_ctx.local_dir / "new_file.txt").exists()
    assert result.is_dry_run is True


# =============================================================================
# Test: source_path parameter
# =============================================================================


@pytest.mark.rsync
def test_pull_files_with_custom_source_path(
    pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that pull_files can use a custom source path instead of work_dir."""
    custom_source = pull_ctx.agent_dir / "subdir"
    custom_source.mkdir(parents=True)
    (custom_source / "file_in_subdir.txt").write_text("content from subdir")
    (pull_ctx.agent_dir / "file_in_root.txt").write_text("content from root")

    result = pull_files(
        agent=pull_ctx.agent,
        host=pull_ctx.host,
        destination=pull_ctx.local_dir,
        source_path=custom_source,
        is_dry_run=False,
        is_delete=False,
        uncommitted_changes=UncommittedChangesMode.FAIL,
        cg=cg,
    )

    assert (pull_ctx.local_dir / "file_in_subdir.txt").read_text() == "content from subdir"
    assert not (pull_ctx.local_dir / "file_in_root.txt").exists()
    assert result.source_path == custom_source


# =============================================================================
# Test: Remote (non-local) host behavior
# =============================================================================


@pytest.fixture
def remote_pull_ctx(tmp_path: Path) -> SyncTestContext:
    """Create a test context with a remote (non-local) host."""
    agent_dir = tmp_path / "agent"
    local_dir = tmp_path / "host"
    agent_dir.mkdir(parents=True)
    init_git_repo_with_config(local_dir)
    return SyncTestContext(
        agent_dir=agent_dir,
        local_dir=local_dir,
        agent=cast(AgentInterface, FakeAgent(work_dir=agent_dir)),
        host=cast(HostInterface, FakeHost(is_local=False)),
    )


@pytest.fixture
def remote_git_pull_ctx(tmp_path: Path) -> SyncTestContext:
    """Create a test context with remote host for git pull testing.

    Both agent and host directories are git repos with shared history.
    """
    agent_dir = tmp_path / "agent"
    local_dir = tmp_path / "host"

    # Initialize agent repo (the source for pull)
    init_git_repo_with_config(agent_dir)

    # Clone agent to create local (so they share history)
    subprocess.run(
        ["git", "clone", str(agent_dir), str(local_dir)],
        capture_output=True,
        text=True,
        check=True,
    )
    run_git_command(local_dir, "config", "user.email", "test@example.com")
    run_git_command(local_dir, "config", "user.name", "Test User")

    return SyncTestContext(
        agent_dir=agent_dir,
        local_dir=local_dir,
        agent=cast(AgentInterface, FakeAgent(work_dir=agent_dir)),
        host=cast(OnlineHostInterface, FakeHost(is_local=False)),
    )


def test_pull_files_with_remote_host_raises_not_implemented(
    remote_pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that pull_files raises NotImplementedError for remote hosts.

    File sync via rsync requires local paths on both sides. Remote host
    support will require SSH-based rsync or a different transfer mechanism.
    """
    (remote_pull_ctx.agent_dir / "file.txt").write_text("agent content")

    with pytest.raises(NotImplementedError, match="remote hosts"):
        pull_files(
            agent=remote_pull_ctx.agent,
            host=remote_pull_ctx.host,
            destination=remote_pull_ctx.local_dir,
            source_path=None,
            is_dry_run=False,
            is_delete=False,
            uncommitted_changes=UncommittedChangesMode.CLOBBER,
            cg=cg,
        )


def test_pull_git_with_local_path_from_remote_host_works(
    remote_git_pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that pull_git works when the agent path is locally accessible.

    Even when the host is marked as non-local (is_local=False), if the agent's
    work_dir is actually a local path, git operations will succeed. This is
    the case for our test environment where we simulate remote hosts locally.

    In a real remote scenario (where the path is on a different machine),
    this would fail because git fetch cannot access the remote path directly.
    """
    # Create a new commit on the agent
    (remote_git_pull_ctx.agent_dir / "new_file.txt").write_text("agent content")
    run_git_command(remote_git_pull_ctx.agent_dir, "add", "new_file.txt")
    run_git_command(remote_git_pull_ctx.agent_dir, "commit", "-m", "Add new file")

    result = pull_git(
        agent=remote_git_pull_ctx.agent,
        host=remote_git_pull_ctx.host,
        destination=remote_git_pull_ctx.local_dir,
        source_branch=None,
        target_branch=None,
        is_dry_run=False,
        uncommitted_changes=UncommittedChangesMode.CLOBBER,
        cg=cg,
    )

    # The new file should now exist in the host directory
    assert (remote_git_pull_ctx.local_dir / "new_file.txt").exists()
    assert (remote_git_pull_ctx.local_dir / "new_file.txt").read_text() == "agent content"
    assert result.is_dry_run is False


def test_pull_git_merge_mode_with_different_branch_restores_stash_on_original(
    remote_git_pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that pull_git with MERGE mode restores stash on original branch.

    When pulling to a different target branch, uncommitted changes should be
    stashed on the original branch, and after the merge, the stash should be
    restored on the original branch (not the target branch).
    """
    local_dir = remote_git_pull_ctx.local_dir
    agent_dir = remote_git_pull_ctx.agent_dir

    # Create a target branch on the agent with a new commit
    run_git_command(agent_dir, "checkout", "-b", "feature-branch")
    (agent_dir / "feature.txt").write_text("feature content")
    run_git_command(agent_dir, "add", "feature.txt")
    run_git_command(agent_dir, "commit", "-m", "Add feature")

    # Create the same branch name on local (so checkout can succeed)
    original_branch = get_current_branch(local_dir, cg)
    run_git_command(local_dir, "checkout", "-b", "feature-branch")
    run_git_command(local_dir, "checkout", original_branch)

    # Make uncommitted changes on the local's original branch
    (local_dir / "README.md").write_text("uncommitted change")
    assert has_uncommitted_changes(local_dir, cg)

    result = pull_git(
        agent=remote_git_pull_ctx.agent,
        host=remote_git_pull_ctx.host,
        destination=local_dir,
        source_branch="feature-branch",
        target_branch="feature-branch",
        is_dry_run=False,
        uncommitted_changes=UncommittedChangesMode.MERGE,
        cg=cg,
    )

    # Verify the merge happened
    assert result.commits_transferred > 0

    # Verify we're back on the original branch with uncommitted changes restored
    current_branch = get_current_branch(local_dir, cg)
    assert current_branch == original_branch
    assert (local_dir / "README.md").read_text() == "uncommitted change"


# =============================================================================
# Git pull: branch defaulting, dry run, stash/merge modes, merge failure
# =============================================================================


@pytest.fixture
def local_git_pull_ctx(tmp_path: Path) -> SyncTestContext:
    """Create a test context with local host for git pull tests.

    Both agent and local directories are git repos with shared history.
    """
    agent_dir = tmp_path / "agent"
    local_dir = tmp_path / "host"

    init_git_repo_with_config(agent_dir)

    subprocess.run(
        ["git", "clone", str(agent_dir), str(local_dir)],
        capture_output=True,
        text=True,
        check=True,
    )
    run_git_command(local_dir, "config", "user.email", "test@example.com")
    run_git_command(local_dir, "config", "user.name", "Test User")

    return SyncTestContext(
        agent_dir=agent_dir,
        local_dir=local_dir,
        agent=cast(AgentInterface, FakeAgent(work_dir=agent_dir)),
        host=cast(OnlineHostInterface, FakeHost(is_local=True)),
    )


def test_pull_git_uses_agent_branch_as_default_source(
    local_git_pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that pull_git uses agent's current branch when source_branch is None."""
    ctx = local_git_pull_ctx

    # Create a feature branch on the agent with a new commit
    run_git_command(ctx.agent_dir, "checkout", "-b", "feature-branch")
    (ctx.agent_dir / "feature.txt").write_text("feature")
    run_git_command(ctx.agent_dir, "add", "feature.txt")
    run_git_command(ctx.agent_dir, "commit", "-m", "Feature commit")

    # Create the same branch on host so checkout works
    run_git_command(ctx.local_dir, "checkout", "-b", "feature-branch")

    result = pull_git(
        agent=ctx.agent,
        host=ctx.host,
        destination=ctx.local_dir,
        source_branch=None,
        target_branch=None,
        is_dry_run=True,
        uncommitted_changes=UncommittedChangesMode.FAIL,
        cg=cg,
    )

    assert result.source_branch == "feature-branch"


def test_pull_git_uses_destination_branch_as_default_target(
    local_git_pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that pull_git uses destination's current branch when target_branch is None."""
    ctx = local_git_pull_ctx

    (ctx.agent_dir / "new.txt").write_text("new")
    run_git_command(ctx.agent_dir, "add", "new.txt")
    run_git_command(ctx.agent_dir, "commit", "-m", "New commit")

    result = pull_git(
        agent=ctx.agent,
        host=ctx.host,
        destination=ctx.local_dir,
        source_branch=None,
        target_branch=None,
        is_dry_run=True,
        uncommitted_changes=UncommittedChangesMode.FAIL,
        cg=cg,
    )

    assert result.target_branch == "main"


def test_pull_git_dry_run_does_not_merge(
    local_git_pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that pull_git with dry_run=True does not actually merge."""
    ctx = local_git_pull_ctx

    for i in range(3):
        (ctx.agent_dir / f"file{i}.txt").write_text(f"content{i}")
        run_git_command(ctx.agent_dir, "add", f"file{i}.txt")
        run_git_command(ctx.agent_dir, "commit", "-m", f"Commit {i}")

    result = pull_git(
        agent=ctx.agent,
        host=ctx.host,
        destination=ctx.local_dir,
        source_branch=None,
        target_branch=None,
        is_dry_run=True,
        uncommitted_changes=UncommittedChangesMode.FAIL,
        cg=cg,
    )

    assert result.is_dry_run is True
    assert result.commits_transferred == 3
    assert not (ctx.local_dir / "file0.txt").exists()


def test_pull_git_merge_mode_stashes_and_restores(
    local_git_pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that pull_git in MERGE mode restores stashed changes after pull."""
    ctx = local_git_pull_ctx

    (ctx.agent_dir / "agent_file.txt").write_text("from agent")
    run_git_command(ctx.agent_dir, "add", "agent_file.txt")
    run_git_command(ctx.agent_dir, "commit", "-m", "Agent commit")

    (ctx.local_dir / "README.md").write_text("uncommitted local change")

    pull_git(
        agent=ctx.agent,
        host=ctx.host,
        destination=ctx.local_dir,
        source_branch=None,
        target_branch=None,
        is_dry_run=False,
        uncommitted_changes=UncommittedChangesMode.MERGE,
        cg=cg,
    )

    assert (ctx.local_dir / "agent_file.txt").exists()
    assert "uncommitted local change" in (ctx.local_dir / "README.md").read_text()
    assert get_stash_count(ctx.local_dir) == 0


def test_pull_git_stash_mode_does_not_restore(
    local_git_pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that pull_git in STASH mode does NOT restore stashed changes after pull."""
    ctx = local_git_pull_ctx

    (ctx.agent_dir / "agent_file.txt").write_text("from agent")
    run_git_command(ctx.agent_dir, "add", "agent_file.txt")
    run_git_command(ctx.agent_dir, "commit", "-m", "Agent commit")

    (ctx.local_dir / "README.md").write_text("uncommitted local change")

    pull_git(
        agent=ctx.agent,
        host=ctx.host,
        destination=ctx.local_dir,
        source_branch=None,
        target_branch=None,
        is_dry_run=False,
        uncommitted_changes=UncommittedChangesMode.STASH,
        cg=cg,
    )

    assert (ctx.local_dir / "agent_file.txt").exists()
    assert get_stash_count(ctx.local_dir) == 1


def test_pull_git_raises_on_merge_failure(
    local_git_pull_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that pull_git raises GitSyncError when merge fails."""
    ctx = local_git_pull_ctx

    (ctx.agent_dir / "README.md").write_text("agent version of README")
    run_git_command(ctx.agent_dir, "add", "README.md")
    run_git_command(ctx.agent_dir, "commit", "-m", "Agent change to README")

    (ctx.local_dir / "README.md").write_text("host version of README")
    run_git_command(ctx.local_dir, "add", "README.md")
    run_git_command(ctx.local_dir, "commit", "-m", "Host change to README")

    with pytest.raises(GitSyncError):
        pull_git(
            agent=ctx.agent,
            host=ctx.host,
            destination=ctx.local_dir,
            source_branch=None,
            target_branch=None,
            is_dry_run=False,
            uncommitted_changes=UncommittedChangesMode.FAIL,
            cg=cg,
        )


# =============================================================================
# Test: Pulling to non-git destination (issue 4)
# =============================================================================


@pytest.mark.rsync
def test_pull_files_to_non_git_directory_succeeds(
    tmp_path: Path,
    cg: ConcurrencyGroup,
) -> None:
    """Test that pull_files works when destination is not a git repo.

    When using --sync-mode=files (the default), pulling to a plain directory
    should work without requiring git. The git uncommitted changes check
    should be skipped.
    """
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "file.txt").write_text("agent content")

    # Create a plain (non-git) destination directory
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()

    agent = cast(AgentInterface, FakeAgent(work_dir=agent_dir))
    host = cast(OnlineHostInterface, FakeHost())

    result = pull_files(
        agent=agent,
        host=host,
        destination=dest_dir,
        source_path=None,
        is_dry_run=False,
        is_delete=False,
        uncommitted_changes=UncommittedChangesMode.FAIL,
        cg=cg,
    )

    assert (dest_dir / "file.txt").read_text() == "agent content"
    assert result.destination_path == dest_dir
