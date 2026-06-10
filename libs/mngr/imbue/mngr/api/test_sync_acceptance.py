"""Acceptance tests for rsync/git push/git pull workflows with real local agents.

These tests exercise ``mngr rsync``, ``mngr git push``, and ``mngr git pull``
against real local agents created by mngr. They verify end-to-end behavior
including agent creation, file sync, git sync, and uncommitted changes handling.

To run these tests locally:

    just test libs/mngr/imbue/mngr/api/test_sync_acceptance.py

Note: These tests use the built-in ``command`` agent type running a long
``sleep`` so the agent stays alive for the test. The claude agent type is
avoided because it requires trust dialogs and API keys. The sync behavior
is identical regardless of agent type.
"""

import subprocess
from collections.abc import Generator
from pathlib import Path

import pytest

from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import mngr_agent_cleanup
from imbue.mngr.utils.testing import run_git_command
from imbue.mngr.utils.testing import run_mngr_subprocess
from imbue.mngr.utils.testing import setup_claude_trust_config_for_subprocess


@pytest.fixture
def sync_test_env(tmp_path: Path) -> dict[str, str]:
    """Create a git repo and subprocess env for sync acceptance tests."""
    repo = tmp_path / "repo"
    init_git_repo(repo)
    return setup_claude_trust_config_for_subprocess(
        trusted_paths=[repo],
        root_name="mngr-sync-acceptance-test",
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
    """Create a local long-running command agent and yield its name."""
    with mngr_agent_cleanup(agent_name, env=sync_test_env, disable_plugins=["modal"]):
        result = run_mngr_subprocess(
            "create",
            "--disable-plugin",
            "modal",
            agent_name,
            "--type",
            "command",
            "--no-connect",
            "--project",
            str(repo_path),
            "--",
            "sleep",
            "100200",
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
    for block in result.stdout.strip().split("\n\n"):
        lines = block.strip().split("\n")
        worktree_line = next((line for line in lines if line.startswith("worktree ")), None)
        if worktree_line and agent_name in worktree_line:
            return Path(worktree_line.removeprefix("worktree "))
    raise AssertionError(f"Could not find worktree for agent {agent_name}")


# =============================================================================
# mngr rsync (push direction: local -> agent)
# =============================================================================


@pytest.mark.acceptance
@pytest.mark.rsync
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_rsync_transfers_files_to_agent(
    sync_test_env: dict[str, str],
    repo_path: Path,
    created_agent: str,
) -> None:
    (repo_path / "pushed_file.txt").write_text("pushed content")
    run_git_command(repo_path, "add", "pushed_file.txt")
    run_git_command(repo_path, "commit", "-m", "Add pushed file")

    # Trailing slash on the source: copy contents of repo into agent's workdir
    # (rather than copying repo itself as a child).
    result = run_mngr_subprocess(
        "rsync",
        "--disable-plugin",
        "modal",
        f"{repo_path}/",
        created_agent,
        env=sync_test_env,
    )
    assert result.returncode == 0, f"Rsync failed: {result.stderr}"

    agent_dir = _get_agent_work_dir(repo_path, created_agent)
    assert (agent_dir / "pushed_file.txt").exists()
    assert (agent_dir / "pushed_file.txt").read_text() == "pushed content"


@pytest.mark.acceptance
@pytest.mark.rsync
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_rsync_dry_run_does_not_transfer(
    sync_test_env: dict[str, str],
    repo_path: Path,
    created_agent: str,
) -> None:
    (repo_path / "dry_run_file.txt").write_text("should not appear")
    run_git_command(repo_path, "add", "dry_run_file.txt")
    run_git_command(repo_path, "commit", "-m", "Add dry run file")

    result = run_mngr_subprocess(
        "rsync",
        "--disable-plugin",
        "modal",
        str(repo_path),
        created_agent,
        "--",
        "--dry-run",
        env=sync_test_env,
    )
    assert result.returncode == 0

    agent_dir = _get_agent_work_dir(repo_path, created_agent)
    assert not (agent_dir / "dry_run_file.txt").exists()


# =============================================================================
# mngr git push
# =============================================================================


@pytest.mark.acceptance
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_git_push_transfers_commits_to_agent(
    sync_test_env: dict[str, str],
    repo_path: Path,
    created_agent: str,
) -> None:
    (repo_path / "git_pushed.txt").write_text("git pushed content")
    run_git_command(repo_path, "add", "git_pushed.txt")
    run_git_command(repo_path, "commit", "-m", "Add git pushed file")

    result = run_mngr_subprocess(
        "git",
        "push",
        "--disable-plugin",
        "modal",
        created_agent,
        env=sync_test_env,
        cwd=repo_path,
    )
    assert result.returncode == 0, f"Git push failed: {result.stderr}"

    agent_dir = _get_agent_work_dir(repo_path, created_agent)
    assert (agent_dir / "git_pushed.txt").exists()
    assert (agent_dir / "git_pushed.txt").read_text() == "git pushed content"

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
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_git_push_force_overwrites_diverged_remote(
    sync_test_env: dict[str, str],
    repo_path: Path,
    created_agent: str,
) -> None:
    """``mngr git push -- --force`` passes through and overwrites a diverged agent branch."""
    agent_dir = _get_agent_work_dir(repo_path, created_agent)

    (agent_dir / "agent_change.txt").write_text("agent work")
    run_git_command(agent_dir, "add", "agent_change.txt")
    run_git_command(agent_dir, "commit", "-m", "Agent commit")

    (repo_path / "local_change.txt").write_text("local work")
    run_git_command(repo_path, "add", "local_change.txt")
    run_git_command(repo_path, "commit", "-m", "Local commit")

    result = run_mngr_subprocess(
        "git",
        "push",
        "--disable-plugin",
        "modal",
        created_agent,
        "--",
        "--force",
        env=sync_test_env,
        cwd=repo_path,
    )
    assert result.returncode == 0, f"Push --force failed: {result.stderr}"

    assert (agent_dir / "local_change.txt").exists()
    assert not (agent_dir / "agent_change.txt").exists()


# =============================================================================
# mngr rsync (pull direction: agent -> local)
# =============================================================================


@pytest.mark.acceptance
@pytest.mark.rsync
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_rsync_transfers_files_from_agent(
    sync_test_env: dict[str, str],
    repo_path: Path,
    created_agent: str,
    tmp_path: Path,
) -> None:
    agent_dir = _get_agent_work_dir(repo_path, created_agent)

    (agent_dir / "agent_file.txt").write_text("from agent")

    pull_dest = tmp_path / "pulled"
    pull_dest.mkdir()
    init_git_repo(pull_dest)

    result = run_mngr_subprocess(
        "rsync",
        "--disable-plugin",
        "modal",
        created_agent,
        str(pull_dest),
        env=sync_test_env,
    )
    assert result.returncode == 0, f"Rsync failed: {result.stderr}"
    assert (pull_dest / "agent_file.txt").exists()
    assert (pull_dest / "agent_file.txt").read_text() == "from agent"


# =============================================================================
# mngr git pull
# =============================================================================


@pytest.mark.acceptance
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_git_pull_merges_agent_commits(
    sync_test_env: dict[str, str],
    repo_path: Path,
    created_agent: str,
) -> None:
    agent_dir = _get_agent_work_dir(repo_path, created_agent)

    (agent_dir / "agent_change.txt").write_text("agent work")
    run_git_command(agent_dir, "add", "agent_change.txt")
    run_git_command(agent_dir, "commit", "-m", "Agent commit")

    # No refspec: ``git pull <url>`` fetches the agent's HEAD branch
    # (``mngr/<agent>``) and merges into the local current branch (main).
    result = run_mngr_subprocess(
        "git",
        "pull",
        "--disable-plugin",
        "modal",
        created_agent,
        "--",
        "--no-edit",
        env=sync_test_env,
        cwd=repo_path,
    )
    assert result.returncode == 0, f"Git pull failed: {result.stderr}"

    assert (repo_path / "agent_change.txt").exists()
    assert (repo_path / "agent_change.txt").read_text() == "agent work"


# =============================================================================
# Round trip: rsync push then pull
# =============================================================================


@pytest.mark.acceptance
@pytest.mark.rsync
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_rsync_round_trips_files(
    sync_test_env: dict[str, str],
    repo_path: Path,
    created_agent: str,
    tmp_path: Path,
) -> None:
    (repo_path / "round_trip.txt").write_text("round trip content")
    run_git_command(repo_path, "add", "round_trip.txt")
    run_git_command(repo_path, "commit", "-m", "Round trip file")

    push_result = run_mngr_subprocess(
        "rsync",
        "--disable-plugin",
        "modal",
        f"{repo_path}/",
        created_agent,
        env=sync_test_env,
    )
    assert push_result.returncode == 0

    pull_dest = tmp_path / "pulled"
    pull_dest.mkdir()
    init_git_repo(pull_dest)

    pull_result = run_mngr_subprocess(
        "rsync",
        "--disable-plugin",
        "modal",
        created_agent,
        str(pull_dest),
        env=sync_test_env,
    )
    assert pull_result.returncode == 0

    assert (pull_dest / "round_trip.txt").read_text() == "round trip content"
