import subprocess
from pathlib import Path
from typing import cast

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mng.api.test_fixtures import FakeAgent
from imbue.mng.api.test_fixtures import FakeHost
from imbue.mng.api.test_fixtures import SyncTestContext
from imbue.mng.errors import BinaryNotInstalledError
from imbue.mng.errors import MngError
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import ConflictMode
from imbue.mng.primitives import SyncDirection
from imbue.mng.primitives import UncommittedChangesMode
from imbue.mng.utils.polling import wait_for
from imbue.mng.utils.testing import init_git_repo_with_config
from imbue.mng.utils.testing import run_git_command
from imbue.mng_pair.api import UnisonSyncer
from imbue.mng_pair.api import determine_git_sync_actions
from imbue.mng_pair.api import pair_files
from imbue.mng_pair.api import sync_git_state


@pytest.fixture
def pair_ctx(tmp_path: Path) -> SyncTestContext:
    """Create a test context with agent and local directories as git repos."""
    agent_dir = tmp_path / "source"
    local_dir = tmp_path / "target"

    # Initialize both as git repos with shared history
    init_git_repo_with_config(agent_dir)
    subprocess.run(
        ["git", "clone", str(agent_dir), str(local_dir)],
        capture_output=True,
        check=True,
    )
    run_git_command(local_dir, "config", "user.email", "test@example.com")
    run_git_command(local_dir, "config", "user.name", "Test User")

    return SyncTestContext(
        agent_dir=agent_dir,
        local_dir=local_dir,
        agent=cast(AgentInterface, FakeAgent(work_dir=agent_dir)),
        host=cast(OnlineHostInterface, FakeHost()),
    )


# =============================================================================
# Test: sync_git_state
# =============================================================================


@pytest.mark.git
def test_sync_git_state_performs_push_when_local_is_ahead(pair_ctx: SyncTestContext, cg: ConcurrencyGroup) -> None:
    """Test that sync_git_state pushes commits from local to agent when local is ahead."""
    # Add a commit to target (local) that needs to be pushed to source (agent)
    (pair_ctx.local_dir / "new_file.txt").write_text("new content")
    run_git_command(pair_ctx.local_dir, "add", "new_file.txt")
    run_git_command(pair_ctx.local_dir, "commit", "-m", "Add new file")

    git_action = determine_git_sync_actions(pair_ctx.agent_dir, pair_ctx.local_dir, cg)
    assert git_action is not None
    assert git_action.local_is_ahead is True

    git_pull_performed, git_push_performed = sync_git_state(
        agent=pair_ctx.agent,
        host=pair_ctx.host,
        local_path=pair_ctx.local_dir,
        git_sync_action=git_action,
        uncommitted_changes=UncommittedChangesMode.CLOBBER,
        cg=cg,
    )

    assert git_push_performed is True
    assert git_pull_performed is False
    # Verify the file now exists in source (agent)
    assert (pair_ctx.agent_dir / "new_file.txt").exists()


@pytest.mark.git
def test_sync_git_state_performs_pull_when_agent_is_ahead(pair_ctx: SyncTestContext, cg: ConcurrencyGroup) -> None:
    """Test that sync_git_state pulls commits from agent to local when agent is ahead."""
    # Add a commit to source (agent) that needs to be pulled to target (local)
    (pair_ctx.agent_dir / "agent_file.txt").write_text("agent content")
    run_git_command(pair_ctx.agent_dir, "add", "agent_file.txt")
    run_git_command(pair_ctx.agent_dir, "commit", "-m", "Add agent file")

    git_action = determine_git_sync_actions(pair_ctx.agent_dir, pair_ctx.local_dir, cg)
    assert git_action is not None
    assert git_action.agent_is_ahead is True

    git_pull_performed, git_push_performed = sync_git_state(
        agent=pair_ctx.agent,
        host=pair_ctx.host,
        local_path=pair_ctx.local_dir,
        git_sync_action=git_action,
        uncommitted_changes=UncommittedChangesMode.CLOBBER,
        cg=cg,
    )

    assert git_pull_performed is True
    assert git_push_performed is False
    # Verify the file now exists in target (local)
    assert (pair_ctx.local_dir / "agent_file.txt").exists()


# =============================================================================
# Test: pair_files context manager
# =============================================================================


def test_pair_files_raises_when_unison_not_installed_and_mocked(
    pair_ctx: SyncTestContext,
    monkeypatch: pytest.MonkeyPatch,
    cg: ConcurrencyGroup,
) -> None:
    """Test that pair_files raises BinaryNotInstalledError when unison is not available."""
    monkeypatch.setattr("shutil.which", lambda _binary: None)

    with pytest.raises(BinaryNotInstalledError):
        with pair_files(
            agent=pair_ctx.agent,
            host=pair_ctx.host,
            agent_path=pair_ctx.agent_dir,
            local_path=pair_ctx.local_dir,
            sync_direction=SyncDirection.BOTH,
            conflict_mode=ConflictMode.NEWER,
            is_require_git=False,
            uncommitted_changes=UncommittedChangesMode.FAIL,
            exclude_patterns=(),
            include_patterns=(),
            cg=cg,
        ):
            pass


def test_pair_files_raises_when_git_required_but_not_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cg: ConcurrencyGroup,
) -> None:
    """Test that pair_files raises MngError when git is required but directories are not repos."""
    # Make all system dependency checks (unison, etc.) pass so we reach the git check
    monkeypatch.setattr("shutil.which", lambda binary: "/tmp/fake/path/" + binary)

    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()

    agent = cast(AgentInterface, FakeAgent(work_dir=source_dir))
    host = cast(OnlineHostInterface, FakeHost())

    with pytest.raises(MngError) as exc_info:
        with pair_files(
            agent=agent,
            host=host,
            agent_path=source_dir,
            local_path=target_dir,
            sync_direction=SyncDirection.BOTH,
            conflict_mode=ConflictMode.NEWER,
            is_require_git=True,
            uncommitted_changes=UncommittedChangesMode.FAIL,
            exclude_patterns=(),
            include_patterns=(),
            cg=cg,
        ):
            pass

    assert "Git repositories required" in str(exc_info.value)


@pytest.mark.unison
@pytest.mark.git
def test_pair_files_starts_and_stops_syncer(pair_ctx: SyncTestContext, cg: ConcurrencyGroup) -> None:
    """Test that pair_files properly starts and stops the unison syncer."""
    with pair_files(
        agent=pair_ctx.agent,
        host=pair_ctx.host,
        agent_path=pair_ctx.agent_dir,
        local_path=pair_ctx.local_dir,
        sync_direction=SyncDirection.BOTH,
        conflict_mode=ConflictMode.NEWER,
        is_require_git=True,
        uncommitted_changes=UncommittedChangesMode.CLOBBER,
        exclude_patterns=(),
        include_patterns=(),
        cg=cg,
    ) as syncer:
        # Wait for unison to start
        wait_for(
            lambda: syncer.is_running,
            error_message="Syncer did not start within timeout",
        )

        # Syncer should be running
        assert syncer.is_running is True

        # Stop it manually
        syncer.stop()

        # Wait for it to stop
        wait_for(
            lambda: not syncer.is_running,
            error_message="Syncer did not stop within timeout",
        )

        # Syncer should not be running
        assert syncer.is_running is False


@pytest.mark.git
@pytest.mark.unison
def test_pair_files_syncs_git_state_before_starting(pair_ctx: SyncTestContext, cg: ConcurrencyGroup) -> None:
    """Test that pair_files syncs git state before starting continuous sync."""
    # Add a commit to source (agent) that should be pulled to target
    (pair_ctx.agent_dir / "agent_commit.txt").write_text("agent content")
    run_git_command(pair_ctx.agent_dir, "add", "agent_commit.txt")
    run_git_command(pair_ctx.agent_dir, "commit", "-m", "Add agent commit")

    # Verify file doesn't exist in target yet
    assert not (pair_ctx.local_dir / "agent_commit.txt").exists()

    with pair_files(
        agent=pair_ctx.agent,
        host=pair_ctx.host,
        agent_path=pair_ctx.agent_dir,
        local_path=pair_ctx.local_dir,
        sync_direction=SyncDirection.BOTH,
        conflict_mode=ConflictMode.NEWER,
        is_require_git=True,
        uncommitted_changes=UncommittedChangesMode.CLOBBER,
        exclude_patterns=(),
        include_patterns=(),
        cg=cg,
    ) as syncer:
        # Git sync should have happened before unison started
        # The file should now exist in target
        assert (pair_ctx.local_dir / "agent_commit.txt").exists()

        # Stop immediately - we just want to test git sync
        syncer.stop()


@pytest.mark.unison
def test_pair_files_with_no_git_requirement(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that pair_files works without git when is_require_git=False."""
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()

    # Create a file in source
    (source_dir / "test_file.txt").write_text("test content")

    agent = cast(AgentInterface, FakeAgent(work_dir=source_dir))
    host = cast(OnlineHostInterface, FakeHost())

    with pair_files(
        agent=agent,
        host=host,
        agent_path=source_dir,
        local_path=target_dir,
        sync_direction=SyncDirection.BOTH,
        conflict_mode=ConflictMode.NEWER,
        is_require_git=False,
        uncommitted_changes=UncommittedChangesMode.FAIL,
        exclude_patterns=(),
        include_patterns=(),
        cg=cg,
    ) as syncer:
        # Wait for unison to start
        wait_for(
            lambda: syncer.is_running,
            error_message="Syncer did not start within timeout",
        )

        # Syncer should be running
        assert syncer.is_running is True

        syncer.stop()


# =============================================================================
# Test: UnisonSyncer with actual unison
# =============================================================================


@pytest.mark.unison
def test_unison_syncer_start_and_stop(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that UnisonSyncer can start and stop unison process."""
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()

    syncer = UnisonSyncer(
        source_path=source,
        target_path=target,
        sync_direction=SyncDirection.BOTH,
        conflict_mode=ConflictMode.NEWER,
        cg=cg,
    )

    try:
        syncer.start()

        # Wait for unison to start
        wait_for(
            lambda: syncer.is_running,
            error_message="Syncer did not start within timeout",
        )

        assert syncer.is_running is True
    finally:
        syncer.stop()

    # Wait for it to fully stop
    wait_for(
        lambda: not syncer.is_running,
        timeout=5.0,
        error_message="Syncer did not stop within timeout",
    )
    assert syncer.is_running is False


@pytest.mark.unison
def test_unison_syncer_syncs_file_changes(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that UnisonSyncer actually syncs file changes."""
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()

    # Create initial file in source
    (source / "initial.txt").write_text("initial content")

    syncer = UnisonSyncer(
        source_path=source,
        target_path=target,
        sync_direction=SyncDirection.BOTH,
        conflict_mode=ConflictMode.NEWER,
        cg=cg,
    )

    try:
        syncer.start()

        # Wait for initial sync to complete
        wait_for(
            lambda: (target / "initial.txt").exists(),
            error_message="File was not synced within timeout",
        )

        # File should be synced to target
        assert (target / "initial.txt").exists()
        assert (target / "initial.txt").read_text() == "initial content"
    finally:
        syncer.stop()


@pytest.mark.unison
def test_unison_syncer_syncs_symlinks(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that UnisonSyncer correctly syncs symlinks."""
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()

    # Create a regular file and a symlink to it in source
    (source / "real_file.txt").write_text("real content")
    (source / "link_to_file.txt").symlink_to(source / "real_file.txt")

    syncer = UnisonSyncer(
        source_path=source,
        target_path=target,
        sync_direction=SyncDirection.BOTH,
        conflict_mode=ConflictMode.NEWER,
        cg=cg,
    )

    try:
        syncer.start()

        # Wait for sync to complete
        wait_for(
            lambda: (target / "link_to_file.txt").exists(),
            error_message="Symlink was not synced within timeout",
        )

        # Both files should exist in target
        assert (target / "real_file.txt").exists()
        assert (target / "link_to_file.txt").exists()

        # The symlink should still be a symlink (not dereferenced)
        assert (target / "link_to_file.txt").is_symlink()
    finally:
        syncer.stop()


@pytest.mark.unison
def test_unison_syncer_syncs_directory_symlinks(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that UnisonSyncer correctly syncs directory symlinks."""
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()

    # Create a directory and a symlink to it in source
    (source / "real_dir").mkdir()
    (source / "real_dir" / "file.txt").write_text("content in dir")
    (source / "link_to_dir").symlink_to(source / "real_dir")

    syncer = UnisonSyncer(
        source_path=source,
        target_path=target,
        sync_direction=SyncDirection.BOTH,
        conflict_mode=ConflictMode.NEWER,
        cg=cg,
    )

    try:
        syncer.start()

        # Wait for sync to complete
        wait_for(
            lambda: (target / "link_to_dir").exists(),
            error_message="Directory symlink was not synced within timeout",
        )

        # Both the directory and symlink should exist
        assert (target / "real_dir").exists()
        assert (target / "real_dir").is_dir()
        assert (target / "link_to_dir").exists()
        assert (target / "link_to_dir").is_symlink()
    finally:
        syncer.stop()


@pytest.mark.unison
def test_unison_syncer_handles_process_crash(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that UnisonSyncer handles unison process crash gracefully."""
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()

    syncer = UnisonSyncer(
        source_path=source,
        target_path=target,
        sync_direction=SyncDirection.BOTH,
        conflict_mode=ConflictMode.NEWER,
        cg=cg,
    )

    try:
        syncer.start()

        # Wait for unison to start
        wait_for(
            lambda: syncer.is_running,
            error_message="Syncer did not start within timeout",
        )

        assert syncer.is_running is True
        assert syncer._running_process is not None

        # Kill the unison process forcefully via SIGKILL (simulating a crash).
        # Use pkill to find the actual unison process by its unique tmp_path args,
        # since RunningProcess doesn't expose the underlying PID directly.
        subprocess.run(
            ["pkill", "-KILL", "-f", f"unison {source} {target}"],
            capture_output=True,
        )

        # is_running should eventually become False
        wait_for(
            lambda: not syncer.is_running,
            error_message="Syncer did not detect process crash",
        )

        assert syncer.is_running is False
    finally:
        # stop() should be safe to call even after process crash
        syncer.stop()


@pytest.mark.unison
def test_unison_syncer_handles_large_files(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that UnisonSyncer correctly syncs large files (50MB)."""
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()

    # Create a 50MB file with random-ish content
    large_file = source / "large_file.bin"
    # 1MB chunks
    chunk_size = 1024 * 1024
    # 50MB
    total_size = 50 * chunk_size

    with open(large_file, "wb") as f:
        for i in range(50):
            # Use a repeating pattern based on chunk number for verification
            chunk = bytes([i % 256] * chunk_size)
            f.write(chunk)

    assert large_file.stat().st_size == total_size

    syncer = UnisonSyncer(
        source_path=source,
        target_path=target,
        sync_direction=SyncDirection.BOTH,
        conflict_mode=ConflictMode.NEWER,
        cg=cg,
    )

    try:
        syncer.start()

        # Wait for sync to complete (longer timeout for large file)
        wait_for(
            lambda: (target / "large_file.bin").exists() and (target / "large_file.bin").stat().st_size == total_size,
            timeout=60.0,
            error_message="Large file was not synced within timeout",
        )

        # Verify file size matches
        assert (target / "large_file.bin").stat().st_size == total_size

        # Verify content integrity by checking first and last chunks
        with open(target / "large_file.bin", "rb") as f:
            first_chunk = f.read(chunk_size)
            assert first_chunk == bytes([0] * chunk_size)

            # Seek to last chunk
            f.seek(-chunk_size, 2)
            last_chunk = f.read(chunk_size)
            assert last_chunk == bytes([49 % 256] * chunk_size)
    finally:
        syncer.stop()
