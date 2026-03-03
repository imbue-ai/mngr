import subprocess
from pathlib import Path

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mng.primitives import ConflictMode
from imbue.mng.primitives import SyncDirection
from imbue.mng.testing.testing import init_git_repo_with_config
from imbue.mng.testing.testing import run_git_command
from imbue.mng_pair.api import GitSyncAction
from imbue.mng_pair.api import UnisonSyncer
from imbue.mng_pair.api import determine_git_sync_actions

# =============================================================================
# Test: UnisonSyncer
# =============================================================================


def test_unison_syncer_builds_basic_command(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that UnisonSyncer builds a valid unison command."""
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

    cmd = syncer._build_unison_command()

    assert "unison" in cmd
    assert str(source) in cmd
    assert str(target) in cmd
    assert "-repeat" in cmd
    assert "watch" in cmd
    assert "-auto" in cmd
    assert "-batch" in cmd


def test_unison_syncer_builds_command_with_forward_direction(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that UnisonSyncer adds force flag for forward direction."""
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()

    syncer = UnisonSyncer(
        source_path=source,
        target_path=target,
        sync_direction=SyncDirection.FORWARD,
        conflict_mode=ConflictMode.NEWER,
        cg=cg,
    )

    cmd = syncer._build_unison_command()

    assert "-force" in cmd
    force_idx = cmd.index("-force")
    assert cmd[force_idx + 1] == str(source)


def test_unison_syncer_builds_command_with_reverse_direction(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that UnisonSyncer adds force flag for reverse direction."""
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()

    syncer = UnisonSyncer(
        source_path=source,
        target_path=target,
        sync_direction=SyncDirection.REVERSE,
        conflict_mode=ConflictMode.NEWER,
        cg=cg,
    )

    cmd = syncer._build_unison_command()

    assert "-force" in cmd
    force_idx = cmd.index("-force")
    assert cmd[force_idx + 1] == str(target)


def test_unison_syncer_builds_command_with_exclude_patterns(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that UnisonSyncer adds exclude patterns to command."""
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()

    syncer = UnisonSyncer(
        source_path=source,
        target_path=target,
        sync_direction=SyncDirection.BOTH,
        conflict_mode=ConflictMode.NEWER,
        exclude_patterns=("*.pyc", "__pycache__"),
        cg=cg,
    )

    cmd = syncer._build_unison_command()

    # Check that exclude patterns are added
    cmd_str = " ".join(cmd)
    assert "*.pyc" in cmd_str
    assert "__pycache__" in cmd_str


def test_unison_syncer_always_excludes_git_directory(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that UnisonSyncer always excludes .git directory."""
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

    cmd = syncer._build_unison_command()
    cmd_str = " ".join(cmd)

    assert ".git" in cmd_str


def test_unison_syncer_is_not_running_initially(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that UnisonSyncer is_running is False before start."""
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

    assert syncer.is_running is False


# =============================================================================
# Test: determine_git_sync_actions
# =============================================================================


def test_determine_git_sync_returns_none_for_non_git_directories(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that determine_git_sync_actions returns None for non-git directories."""
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()

    result = determine_git_sync_actions(source, target, cg)

    assert result is None


def test_determine_git_sync_returns_none_when_only_source_is_git(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that returns None when only source is a git repo."""
    source = tmp_path / "source"
    target = tmp_path / "target"
    init_git_repo_with_config(source)
    target.mkdir()

    result = determine_git_sync_actions(source, target, cg)

    assert result is None


def test_determine_git_sync_returns_none_when_only_target_is_git(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that returns None when only target is a git repo."""
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    init_git_repo_with_config(target)

    result = determine_git_sync_actions(source, target, cg)

    assert result is None


def test_determine_git_sync_returns_no_action_when_both_in_sync(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that returns no action needed when repos have same commit."""
    source = tmp_path / "source"
    target = tmp_path / "target"

    # Create source repo
    init_git_repo_with_config(source)

    # Clone source to target (same commit)
    subprocess.run(
        ["git", "clone", str(source), str(target)],
        capture_output=True,
        check=True,
    )

    result = determine_git_sync_actions(source, target, cg)

    assert result is not None
    assert result.agent_is_ahead is False
    assert result.local_is_ahead is False


def test_determine_git_sync_detects_source_ahead(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that detects when source has commits not in target."""
    source = tmp_path / "source"
    target = tmp_path / "target"

    # Create source repo
    init_git_repo_with_config(source)

    # Clone source to target
    subprocess.run(
        ["git", "clone", str(source), str(target)],
        capture_output=True,
        check=True,
    )

    # Add a commit to source
    (source / "new_file.txt").write_text("new content")
    run_git_command(source, "add", "new_file.txt")
    run_git_command(source, "commit", "-m", "Add new file")

    result = determine_git_sync_actions(source, target, cg)

    assert result is not None
    assert result.agent_is_ahead is True
    assert result.local_is_ahead is False


def test_determine_git_sync_detects_target_ahead(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that detects when target has commits not in source."""
    source = tmp_path / "source"
    target = tmp_path / "target"

    # Create source repo
    init_git_repo_with_config(source)

    # Clone source to target
    subprocess.run(
        ["git", "clone", str(source), str(target)],
        capture_output=True,
        check=True,
    )
    run_git_command(target, "config", "user.email", "test@example.com")
    run_git_command(target, "config", "user.name", "Test User")

    # Add a commit to target
    (target / "new_file.txt").write_text("new content")
    run_git_command(target, "add", "new_file.txt")
    run_git_command(target, "commit", "-m", "Add new file")

    result = determine_git_sync_actions(source, target, cg)

    assert result is not None
    assert result.agent_is_ahead is False
    assert result.local_is_ahead is True


def test_determine_git_sync_detects_both_diverged(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that detects when both repos have diverged."""
    source = tmp_path / "source"
    target = tmp_path / "target"

    # Create source repo
    init_git_repo_with_config(source)

    # Clone source to target
    subprocess.run(
        ["git", "clone", str(source), str(target)],
        capture_output=True,
        check=True,
    )
    run_git_command(target, "config", "user.email", "test@example.com")
    run_git_command(target, "config", "user.name", "Test User")

    # Add a commit to source
    (source / "source_file.txt").write_text("source content")
    run_git_command(source, "add", "source_file.txt")
    run_git_command(source, "commit", "-m", "Add source file")

    # Add a different commit to target
    (target / "target_file.txt").write_text("target content")
    run_git_command(target, "add", "target_file.txt")
    run_git_command(target, "commit", "-m", "Add target file")

    result = determine_git_sync_actions(source, target, cg)

    assert result is not None
    assert result.agent_is_ahead is True
    assert result.local_is_ahead is True


# =============================================================================
# Test: GitSyncAction
# =============================================================================


def test_git_sync_action_default_values() -> None:
    """Test that GitSyncAction has correct default values."""
    action = GitSyncAction(
        agent_branch="main",
        local_branch="main",
    )

    assert action.agent_is_ahead is False
    assert action.local_is_ahead is False
    assert action.agent_branch == "main"
    assert action.local_branch == "main"
