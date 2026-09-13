import subprocess
from pathlib import Path
from typing import cast

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.api.testing import FakeHost
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import ConflictMode
from imbue.mngr.primitives import SyncDirection
from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import run_git_command
from imbue.mngr_pair.api import GitSyncAction
from imbue.mngr_pair.api import UnisonSyncer
from imbue.mngr_pair.api import determine_git_sync_actions
from imbue.mngr_pair.remote import SshEndpoint
from imbue.mngr_pair.remote import UnisonRoot

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
        source_root=UnisonRoot(path=source),
        target_root=UnisonRoot(path=target),
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
        source_root=UnisonRoot(path=source),
        target_root=UnisonRoot(path=target),
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
        source_root=UnisonRoot(path=source),
        target_root=UnisonRoot(path=target),
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
        source_root=UnisonRoot(path=source),
        target_root=UnisonRoot(path=target),
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
        source_root=UnisonRoot(path=source),
        target_root=UnisonRoot(path=target),
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
        source_root=UnisonRoot(path=source),
        target_root=UnisonRoot(path=target),
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

    result = determine_git_sync_actions(source, target, cast(OnlineHostInterface, FakeHost()), cg)

    assert result is None


def test_determine_git_sync_returns_none_when_only_source_is_git(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that returns None when only source is a git repo."""
    source = tmp_path / "source"
    target = tmp_path / "target"
    init_git_repo(source)
    target.mkdir()

    result = determine_git_sync_actions(source, target, cast(OnlineHostInterface, FakeHost()), cg)

    assert result is None


def test_determine_git_sync_returns_none_when_only_target_is_git(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that returns None when only target is a git repo."""
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    init_git_repo(target)

    result = determine_git_sync_actions(source, target, cast(OnlineHostInterface, FakeHost()), cg)

    assert result is None


def test_determine_git_sync_returns_no_action_when_both_in_sync(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that returns no action needed when repos have same commit."""
    source = tmp_path / "source"
    target = tmp_path / "target"

    # Create source repo
    init_git_repo(source)

    # Clone source to target (same commit)
    subprocess.run(
        ["git", "clone", str(source), str(target)],
        capture_output=True,
        check=True,
    )

    result = determine_git_sync_actions(source, target, cast(OnlineHostInterface, FakeHost()), cg)

    assert result is not None
    assert result.agent_is_ahead is False
    assert result.local_is_ahead is False


def test_determine_git_sync_detects_source_ahead(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that detects when source has commits not in target."""
    source = tmp_path / "source"
    target = tmp_path / "target"

    # Create source repo
    init_git_repo(source)

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

    result = determine_git_sync_actions(source, target, cast(OnlineHostInterface, FakeHost()), cg)

    assert result is not None
    assert result.agent_is_ahead is True
    assert result.local_is_ahead is False


def test_determine_git_sync_detects_source_ahead_from_an_agent_subdirectory(
    tmp_path: Path, cg: ConcurrencyGroup
) -> None:
    """``mngr pair my-agent:/subdir`` pairs a subdirectory, which is not a repository.

    The reconciliation fetch has to name the agent's worktree root: ``git fetch``
    against a subdirectory fails outright, which would silently downgrade the whole
    comparison to "neither side is ahead".
    """
    source = tmp_path / "source"
    target = tmp_path / "target"

    init_git_repo(source)
    subprocess.run(
        ["git", "clone", str(source), str(target)],
        capture_output=True,
        check=True,
    )

    subdir = source / "subdir"
    subdir.mkdir()
    (subdir / "new_file.txt").write_text("new content")
    run_git_command(source, "add", "subdir/new_file.txt")
    run_git_command(source, "commit", "-m", "Add new file")

    result = determine_git_sync_actions(subdir, target, cast(OnlineHostInterface, FakeHost()), cg)

    assert result is not None
    assert result.agent_is_ahead is True
    assert result.local_is_ahead is False


def test_determine_git_sync_detects_target_ahead(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that detects when target has commits not in source."""
    source = tmp_path / "source"
    target = tmp_path / "target"

    # Create source repo
    init_git_repo(source)

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

    result = determine_git_sync_actions(source, target, cast(OnlineHostInterface, FakeHost()), cg)

    assert result is not None
    assert result.agent_is_ahead is False
    assert result.local_is_ahead is True


def test_determine_git_sync_detects_both_diverged(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that detects when both repos have diverged."""
    source = tmp_path / "source"
    target = tmp_path / "target"

    # Create source repo
    init_git_repo(source)

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

    result = determine_git_sync_actions(source, target, cast(OnlineHostInterface, FakeHost()), cg)

    assert result is not None
    assert result.agent_is_ahead is True
    assert result.local_is_ahead is True


# =============================================================================
# Test: GitSyncAction
# =============================================================================


def _make_syncer(
    tmp_path: Path,
    cg: ConcurrencyGroup,
    conflict_mode: ConflictMode = ConflictMode.NEWER,
    include_patterns: tuple[str, ...] = (),
) -> UnisonSyncer:
    """Create a UnisonSyncer with source/target dirs for testing."""
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir(exist_ok=True)
    target.mkdir(exist_ok=True)
    return UnisonSyncer(
        source_root=UnisonRoot(path=source),
        target_root=UnisonRoot(path=target),
        sync_direction=SyncDirection.BOTH,
        conflict_mode=conflict_mode,
        include_patterns=include_patterns,
        cg=cg,
    )


def test_unison_syncer_builds_command_with_source_conflict_mode(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that UnisonSyncer adds -prefer <source root> for SOURCE conflict mode."""
    syncer = _make_syncer(tmp_path, cg, conflict_mode=ConflictMode.SOURCE)
    cmd = syncer._build_unison_command()
    assert "-prefer" in cmd
    assert cmd[cmd.index("-prefer") + 1] == str(syncer.source_root.path)


def test_unison_syncer_builds_command_with_target_conflict_mode(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that UnisonSyncer adds -prefer <target root> for TARGET conflict mode."""
    syncer = _make_syncer(tmp_path, cg, conflict_mode=ConflictMode.TARGET)
    cmd = syncer._build_unison_command()
    assert "-prefer" in cmd
    assert cmd[cmd.index("-prefer") + 1] == str(syncer.target_root.path)


def test_unison_syncer_builds_command_with_include_patterns(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that UnisonSyncer adds include patterns as -path arguments."""
    syncer = _make_syncer(tmp_path, cg, include_patterns=("src", "lib"))
    cmd = syncer._build_unison_command()
    path_indices = [i for i, arg in enumerate(cmd) if arg == "-path"]
    assert len(path_indices) == 2
    assert cmd[path_indices[0] + 1] == "src"
    assert cmd[path_indices[1] + 1] == "lib"


def test_unison_syncer_wait_returns_zero_when_not_started(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that wait() returns 0 when no process has been started."""
    syncer = _make_syncer(tmp_path, cg)
    assert syncer.wait() == 0


def test_unison_syncer_stop_is_noop_when_not_started(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that stop() is a no-op when no process has been started."""
    syncer = _make_syncer(tmp_path, cg)
    syncer.stop()


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


# =============================================================================
# Test: UnisonSyncer against a remote replica
# =============================================================================


def _remote_syncer(
    tmp_path: Path, cg: ConcurrencyGroup, conflict_mode: ConflictMode = ConflictMode.NEWER
) -> UnisonSyncer:
    """A syncer whose source replica lives on a remote host."""
    local = tmp_path / "local"
    local.mkdir(exist_ok=True)
    endpoint = SshEndpoint(user="root", hostname="10.0.0.4", port=2222, key_path=tmp_path / "id")
    return UnisonSyncer(
        source_root=UnisonRoot(path=Path("/work/repo"), ssh=endpoint),
        target_root=UnisonRoot(path=local),
        ssh_wrapper_path=tmp_path / "ssh-wrapper.sh",
        remote_unison_path=Path("/root/.mngr/bin/unison"),
        sync_direction=SyncDirection.BOTH,
        conflict_mode=conflict_mode,
        cg=cg,
    )


def test_remote_syncer_uses_an_ssh_root_and_names_the_transport(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """A remote replica needs the ssh:// root plus both transport flags."""
    syncer = _remote_syncer(tmp_path, cg)

    cmd = syncer._build_unison_command()

    assert "ssh://root@10.0.0.4//work/repo" in cmd
    # -sshcmd carries the key and port, which the root syntax cannot express.
    assert cmd[cmd.index("-sshcmd") + 1] == str(tmp_path / "ssh-wrapper.sh")
    # -servercmd pins which unison answers, so a too-old one on PATH is never used.
    assert cmd[cmd.index("-servercmd") + 1] == "/root/.mngr/bin/unison"


def test_local_syncer_passes_no_transport_flags(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Both replicas local means a single unison process and no SSH at all."""
    syncer = _make_syncer(tmp_path, cg)

    cmd = syncer._build_unison_command()

    assert "-sshcmd" not in cmd
    assert "-servercmd" not in cmd


def test_remote_syncer_prefer_names_the_full_ssh_root(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """unison resolves -prefer against the roots it was given, so a bare path would not match."""
    syncer = _remote_syncer(tmp_path, cg, conflict_mode=ConflictMode.SOURCE)

    cmd = syncer._build_unison_command()

    assert cmd[cmd.index("-prefer") + 1] == "ssh://root@10.0.0.4//work/repo"


def test_remote_syncer_force_names_the_full_ssh_root(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Same for -force, which drives --sync-direction."""
    local = tmp_path / "local"
    local.mkdir(exist_ok=True)
    endpoint = SshEndpoint(user="root", hostname="10.0.0.4", port=2222, key_path=tmp_path / "id")
    syncer = UnisonSyncer(
        source_root=UnisonRoot(path=Path("/work/repo"), ssh=endpoint),
        target_root=UnisonRoot(path=local),
        sync_direction=SyncDirection.FORWARD,
        conflict_mode=ConflictMode.NEWER,
        cg=cg,
    )

    cmd = syncer._build_unison_command()

    assert cmd[cmd.index("-force") + 1] == "ssh://root@10.0.0.4//work/repo"
