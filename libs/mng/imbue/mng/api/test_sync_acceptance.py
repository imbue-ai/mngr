"""Acceptance tests for push/pull sync workflows with real local agents.

These tests exercise the full push/pull/pair CLI commands against real local
agents created by mng. They verify end-to-end behavior including agent
creation, file sync, git sync, and uncommitted changes handling.

To run these tests locally:

    just test libs/mng/imbue/mng/api/test_sync_acceptance.py

Note: These tests use 'bash' as the agent type since claude requires trust
dialogs and API keys. The sync behavior is identical regardless of agent type.
"""

import subprocess
from collections.abc import Generator
from pathlib import Path

import pytest

from imbue.mng.errors import MngError
from imbue.mng.utils.testing import get_short_random_string
from imbue.mng.utils.testing import init_git_repo_with_config
from imbue.mng.utils.testing import mng_agent_cleanup
from imbue.mng.utils.testing import run_git_command
from imbue.mng.utils.testing import run_mng_subprocess
from imbue.mng.utils.testing import setup_claude_trust_config_for_subprocess


@pytest.fixture
def sync_test_env(tmp_path: Path) -> dict[str, str]:
    """Create a git repo and subprocess env for sync acceptance tests.

    Returns env dict with Claude trust configured for the test repo.
    The test repo path is available as tmp_path / "repo".
    """
    repo = tmp_path / "repo"
    init_git_repo_with_config(repo)
    return setup_claude_trust_config_for_subprocess(
        trusted_paths=[repo],
        root_name="mng-sync-acceptance-test",
    )


@pytest.fixture
def repo_path(tmp_path: Path) -> Path:
    return tmp_path / "repo"


@pytest.fixture
def agent_name() -> str:
    return f"sync-test-{get_short_random_string()}"


@pytest.fixture
def created_agent(
    sync_test_env: dict[str, str],
    repo_path: Path,
    agent_name: str,
) -> Generator[str, None, None]:
    """Create a local bash agent from the test repo and yield its name.

    Destroys the agent after the test completes.
    """
    with mng_agent_cleanup(agent_name, env=sync_test_env, disable_plugins=["modal"]):
        result = run_mng_subprocess(
            "create",
            "--disable-plugin",
            "modal",
            agent_name,
            "bash",
            "--no-connect",
            "--project",
            str(repo_path),
            env=sync_test_env,
            cwd=repo_path,
        )
        assert result.returncode == 0, f"Failed to create agent: {result.stderr}"

        yield agent_name


def _get_agent_work_dir(repo_path: Path, agent_name: str) -> Path:
    """Find the agent's worktree directory by inspecting git worktree list."""
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=True,
    )
    # Parse porcelain output to find the worktree for this agent
    for block in result.stdout.strip().split("\n\n"):
        lines = block.strip().split("\n")
        worktree_path = lines[0].replace("worktree ", "")
        for line in lines[1:]:
            if line.startswith("branch ") and agent_name in line:
                return Path(worktree_path)
    raise MngError(f"Could not find worktree for agent {agent_name}")


# =============================================================================
# Test: Push files (--sync-mode=files)
# =============================================================================


@pytest.mark.acceptance
@pytest.mark.timeout(120)
def test_push_files_transfers_files_to_agent(
    sync_test_env: dict[str, str],
    repo_path: Path,
    created_agent: str,
) -> None:
    """Test that push --sync-mode=files copies files from local to agent."""
    # Add a new file to the local repo
    (repo_path / "pushed_file.txt").write_text("pushed content")
    run_git_command(repo_path, "add", "pushed_file.txt")
    run_git_command(repo_path, "commit", "-m", "Add pushed file")

    result = run_mng_subprocess(
        "push",
        "--disable-plugin",
        "modal",
        created_agent,
        str(repo_path),
        "--sync-mode=files",
        env=sync_test_env,
    )
    assert result.returncode == 0, f"Push failed: {result.stderr}"

    agent_dir = _get_agent_work_dir(repo_path, created_agent)
    assert (agent_dir / "pushed_file.txt").exists()
    assert (agent_dir / "pushed_file.txt").read_text() == "pushed content"


@pytest.mark.acceptance
@pytest.mark.timeout(120)
def test_push_files_dry_run_does_not_transfer(
    sync_test_env: dict[str, str],
    repo_path: Path,
    created_agent: str,
) -> None:
    """Test that push --dry-run previews without transferring."""
    (repo_path / "dry_run_file.txt").write_text("should not appear")
    run_git_command(repo_path, "add", "dry_run_file.txt")
    run_git_command(repo_path, "commit", "-m", "Add dry run file")

    result = run_mng_subprocess(
        "push",
        "--disable-plugin",
        "modal",
        created_agent,
        str(repo_path),
        "--sync-mode=files",
        "--dry-run",
        env=sync_test_env,
    )
    assert result.returncode == 0

    agent_dir = _get_agent_work_dir(repo_path, created_agent)
    assert not (agent_dir / "dry_run_file.txt").exists()


# =============================================================================
# Test: Push git (--sync-mode=git)
# =============================================================================


@pytest.mark.acceptance
@pytest.mark.timeout(120)
def test_push_git_transfers_commits_to_agent(
    sync_test_env: dict[str, str],
    repo_path: Path,
    created_agent: str,
) -> None:
    """Test that push --sync-mode=git pushes commits to the agent worktree."""
    (repo_path / "git_pushed.txt").write_text("git pushed content")
    run_git_command(repo_path, "add", "git_pushed.txt")
    run_git_command(repo_path, "commit", "-m", "Add git pushed file")

    result = run_mng_subprocess(
        "push",
        "--disable-plugin",
        "modal",
        created_agent,
        str(repo_path),
        "--sync-mode=git",
        "--uncommitted-changes=clobber",
        env=sync_test_env,
    )
    assert result.returncode == 0, f"Git push failed: {result.stderr}"

    agent_dir = _get_agent_work_dir(repo_path, created_agent)
    assert (agent_dir / "git_pushed.txt").exists()
    assert (agent_dir / "git_pushed.txt").read_text() == "git pushed content"

    # Verify the commit history matches
    local_log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    agent_log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=str(agent_dir),
        capture_output=True,
        text=True,
    )
    assert local_log.stdout.strip().split("\n")[0] in agent_log.stdout


@pytest.mark.acceptance
@pytest.mark.timeout(120)
def test_push_git_uncommitted_changes_fail_mode_rejects(
    sync_test_env: dict[str, str],
    repo_path: Path,
    created_agent: str,
) -> None:
    """Test that push --uncommitted-changes=fail errors when agent has dirty files."""
    agent_dir = _get_agent_work_dir(repo_path, created_agent)

    # Dirty the agent worktree
    (agent_dir / "dirty.txt").write_text("uncommitted")

    # Make a commit to push
    (repo_path / "new.txt").write_text("new")
    run_git_command(repo_path, "add", "new.txt")
    run_git_command(repo_path, "commit", "-m", "New file")

    result = run_mng_subprocess(
        "push",
        "--disable-plugin",
        "modal",
        created_agent,
        str(repo_path),
        "--sync-mode=git",
        "--uncommitted-changes=fail",
        env=sync_test_env,
    )
    assert result.returncode != 0
    assert "Uncommitted changes" in result.stderr


@pytest.mark.acceptance
@pytest.mark.timeout(120)
def test_push_git_uncommitted_changes_stash_mode_preserves_changes(
    sync_test_env: dict[str, str],
    repo_path: Path,
    created_agent: str,
) -> None:
    """Test that push --uncommitted-changes=stash stashes agent changes."""
    agent_dir = _get_agent_work_dir(repo_path, created_agent)

    # Dirty the agent worktree
    (agent_dir / "stashed.txt").write_text("will be stashed")

    # Make a commit to push
    (repo_path / "new.txt").write_text("new")
    run_git_command(repo_path, "add", "new.txt")
    run_git_command(repo_path, "commit", "-m", "New file")

    result = run_mng_subprocess(
        "push",
        "--disable-plugin",
        "modal",
        created_agent,
        str(repo_path),
        "--sync-mode=git",
        "--uncommitted-changes=stash",
        env=sync_test_env,
    )
    assert result.returncode == 0, f"Push failed: {result.stderr}"

    # The pushed file should be there
    assert (agent_dir / "new.txt").exists()

    # The dirty file should be stashed, not in the working tree
    assert not (agent_dir / "stashed.txt").exists()

    # Verify stash exists
    stash_result = subprocess.run(
        ["git", "stash", "list"],
        cwd=str(agent_dir),
        capture_output=True,
        text=True,
    )
    assert "mng-sync-stash" in stash_result.stdout


@pytest.mark.acceptance
@pytest.mark.timeout(120)
def test_push_git_uncommitted_changes_clobber_mode_discards_changes(
    sync_test_env: dict[str, str],
    repo_path: Path,
    created_agent: str,
) -> None:
    """Test that push --uncommitted-changes=clobber discards agent changes."""
    agent_dir = _get_agent_work_dir(repo_path, created_agent)

    # Dirty the agent worktree
    (agent_dir / "clobbered.txt").write_text("will be discarded")

    # Make a commit to push
    (repo_path / "new.txt").write_text("new")
    run_git_command(repo_path, "add", "new.txt")
    run_git_command(repo_path, "commit", "-m", "New file")

    result = run_mng_subprocess(
        "push",
        "--disable-plugin",
        "modal",
        created_agent,
        str(repo_path),
        "--sync-mode=git",
        "--uncommitted-changes=clobber",
        env=sync_test_env,
    )
    assert result.returncode == 0, f"Push failed: {result.stderr}"

    assert (agent_dir / "new.txt").exists()
    assert not (agent_dir / "clobbered.txt").exists()


@pytest.mark.acceptance
@pytest.mark.timeout(120)
def test_push_git_mirror_mode_overwrites_all_refs(
    sync_test_env: dict[str, str],
    repo_path: Path,
    created_agent: str,
) -> None:
    """Test that push --mirror overwrites all refs in the target."""
    # Create a branch on the source
    run_git_command(repo_path, "checkout", "-b", "feature-branch")
    (repo_path / "feature.txt").write_text("feature")
    run_git_command(repo_path, "add", "feature.txt")
    run_git_command(repo_path, "commit", "-m", "Feature commit")
    run_git_command(repo_path, "checkout", "main")

    result = run_mng_subprocess(
        "push",
        "--disable-plugin",
        "modal",
        created_agent,
        str(repo_path),
        "--sync-mode=git",
        "--mirror",
        "--uncommitted-changes=clobber",
        env=sync_test_env,
    )
    assert result.returncode == 0, f"Mirror push failed: {result.stderr}"

    # The feature branch should exist on the agent
    agent_dir = _get_agent_work_dir(repo_path, created_agent)
    branch_result = subprocess.run(
        ["git", "branch", "-a"],
        cwd=str(agent_dir),
        capture_output=True,
        text=True,
    )
    assert "feature-branch" in branch_result.stdout


# =============================================================================
# Test: Pull files (--sync-mode=files)
# =============================================================================


@pytest.mark.acceptance
@pytest.mark.timeout(120)
def test_pull_files_transfers_files_from_agent(
    sync_test_env: dict[str, str],
    repo_path: Path,
    created_agent: str,
    tmp_path: Path,
) -> None:
    """Test that pull --sync-mode=files copies files from agent to local."""
    agent_dir = _get_agent_work_dir(repo_path, created_agent)

    # Add a file on the agent side
    (agent_dir / "agent_file.txt").write_text("from agent")

    # Pull into a fresh directory
    pull_dest = tmp_path / "pulled"
    pull_dest.mkdir()
    init_git_repo_with_config(pull_dest)

    result = run_mng_subprocess(
        "pull",
        "--disable-plugin",
        "modal",
        created_agent,
        str(pull_dest),
        "--sync-mode=files",
        env=sync_test_env,
    )
    assert result.returncode == 0, f"Pull failed: {result.stderr}"
    assert (pull_dest / "agent_file.txt").exists()
    assert (pull_dest / "agent_file.txt").read_text() == "from agent"


# =============================================================================
# Test: Pull git (--sync-mode=git)
# =============================================================================


@pytest.mark.acceptance
@pytest.mark.timeout(120)
def test_pull_git_merges_agent_commits(
    sync_test_env: dict[str, str],
    repo_path: Path,
    created_agent: str,
    tmp_path: Path,
) -> None:
    """Test that pull --sync-mode=git merges commits from agent to local."""
    agent_dir = _get_agent_work_dir(repo_path, created_agent)

    # Make a commit on the agent side
    (agent_dir / "agent_change.txt").write_text("agent work")
    run_git_command(agent_dir, "add", "agent_change.txt")
    run_git_command(agent_dir, "commit", "-m", "Agent commit")

    # Pull into the original repo
    result = run_mng_subprocess(
        "pull",
        "--disable-plugin",
        "modal",
        created_agent,
        str(repo_path),
        "--sync-mode=git",
        "--uncommitted-changes=clobber",
        env=sync_test_env,
    )
    assert result.returncode == 0, f"Git pull failed: {result.stderr}"

    assert (repo_path / "agent_change.txt").exists()
    assert (repo_path / "agent_change.txt").read_text() == "agent work"


# =============================================================================
# Test: Round-trip (push then pull)
# =============================================================================


@pytest.mark.acceptance
@pytest.mark.timeout(120)
def test_push_then_pull_round_trips_files(
    sync_test_env: dict[str, str],
    repo_path: Path,
    created_agent: str,
    tmp_path: Path,
) -> None:
    """Test that pushing files to an agent and pulling them back produces the same content."""
    # Create content and push
    (repo_path / "round_trip.txt").write_text("round trip content")
    run_git_command(repo_path, "add", "round_trip.txt")
    run_git_command(repo_path, "commit", "-m", "Round trip file")

    push_result = run_mng_subprocess(
        "push",
        "--disable-plugin",
        "modal",
        created_agent,
        str(repo_path),
        "--sync-mode=files",
        env=sync_test_env,
    )
    assert push_result.returncode == 0

    # Pull into a fresh directory
    pull_dest = tmp_path / "pulled"
    pull_dest.mkdir()
    init_git_repo_with_config(pull_dest)

    pull_result = run_mng_subprocess(
        "pull",
        "--disable-plugin",
        "modal",
        created_agent,
        str(pull_dest),
        "--sync-mode=files",
        env=sync_test_env,
    )
    assert pull_result.returncode == 0

    assert (pull_dest / "round_trip.txt").read_text() == "round trip content"
