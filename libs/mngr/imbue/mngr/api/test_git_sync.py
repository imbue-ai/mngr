"""Integration tests for the thin ``git_push`` / ``git_pull`` wrappers.

These exercise end-state behavior: did the commit end up on the other side?
Branch-default and uncommitted-changes orchestration moved into ``mngr_pair``,
which has its own coverage.
"""

import subprocess
from pathlib import Path
from typing import cast

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.api.git import GitSyncError
from imbue.mngr.api.git import git_pull
from imbue.mngr.api.git import git_push
from imbue.mngr.api.testing import FakeAgent
from imbue.mngr.api.testing import FakeHost
from imbue.mngr.api.testing import SyncTestContext
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import run_git_command


@pytest.fixture
def local_git_ctx(tmp_path: Path) -> SyncTestContext:
    """Two git repos that share history (clone)."""
    agent_dir = tmp_path / "agent"
    local_dir = tmp_path / "host"

    init_git_repo(agent_dir)
    subprocess.run(["git", "clone", str(agent_dir), str(local_dir)], capture_output=True, check=True)
    run_git_command(local_dir, "config", "user.email", "test@example.com")
    run_git_command(local_dir, "config", "user.name", "Test User")

    return SyncTestContext(
        agent_dir=agent_dir,
        local_dir=local_dir,
        agent=cast(AgentInterface, FakeAgent(work_dir=agent_dir)),
        host=cast(OnlineHostInterface, FakeHost(is_local=True)),
    )


# =============================================================================
# git_pull
# =============================================================================


def test_git_pull_brings_remote_commit_into_local(
    local_git_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    (local_git_ctx.agent_dir / "agent_file.txt").write_text("from agent")
    run_git_command(local_git_ctx.agent_dir, "add", "agent_file.txt")
    run_git_command(local_git_ctx.agent_dir, "commit", "-m", "Agent commit")

    git_pull(
        local_path=local_git_ctx.local_dir,
        remote_host=local_git_ctx.host,
        remote_path=local_git_ctx.agent_dir,
        extra_args=("main", "--no-edit"),
        cg=cg,
    )

    assert (local_git_ctx.local_dir / "agent_file.txt").read_text() == "from agent"


def test_git_pull_with_dry_run_does_not_merge(
    local_git_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    (local_git_ctx.agent_dir / "f.txt").write_text("x")
    run_git_command(local_git_ctx.agent_dir, "add", "f.txt")
    run_git_command(local_git_ctx.agent_dir, "commit", "-m", "Agent commit")

    git_pull(
        local_path=local_git_ctx.local_dir,
        remote_host=local_git_ctx.host,
        remote_path=local_git_ctx.agent_dir,
        extra_args=("main", "--no-edit", "--dry-run"),
        cg=cg,
    )

    assert not (local_git_ctx.local_dir / "f.txt").exists()


def test_git_pull_raises_on_merge_conflict(
    local_git_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    (local_git_ctx.agent_dir / "README.md").write_text("agent version")
    run_git_command(local_git_ctx.agent_dir, "add", "README.md")
    run_git_command(local_git_ctx.agent_dir, "commit", "-m", "Agent change")

    (local_git_ctx.local_dir / "README.md").write_text("local version")
    run_git_command(local_git_ctx.local_dir, "add", "README.md")
    run_git_command(local_git_ctx.local_dir, "commit", "-m", "Local change")

    with pytest.raises(GitSyncError):
        git_pull(
            local_path=local_git_ctx.local_dir,
            remote_host=local_git_ctx.host,
            remote_path=local_git_ctx.agent_dir,
            extra_args=("main", "--no-edit"),
            cg=cg,
        )


# =============================================================================
# git_push
# =============================================================================


def test_git_push_brings_local_commit_to_remote(
    local_git_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    (local_git_ctx.local_dir / "local_file.txt").write_text("from local")
    run_git_command(local_git_ctx.local_dir, "add", "local_file.txt")
    run_git_command(local_git_ctx.local_dir, "commit", "-m", "Local commit")

    git_push(
        local_path=local_git_ctx.local_dir,
        remote_host=local_git_ctx.host,
        remote_path=local_git_ctx.agent_dir,
        extra_args=("main",),
        cg=cg,
    )

    assert (local_git_ctx.agent_dir / "local_file.txt").read_text() == "from local"


def test_git_push_with_dry_run_does_not_modify_remote(
    local_git_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    (local_git_ctx.local_dir / "x.txt").write_text("x")
    run_git_command(local_git_ctx.local_dir, "add", "x.txt")
    run_git_command(local_git_ctx.local_dir, "commit", "-m", "Local commit")

    git_push(
        local_path=local_git_ctx.local_dir,
        remote_host=local_git_ctx.host,
        remote_path=local_git_ctx.agent_dir,
        extra_args=("main", "--dry-run"),
        cg=cg,
    )

    assert not (local_git_ctx.agent_dir / "x.txt").exists()


def test_git_push_refspec_pushes_to_renamed_branch(
    local_git_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Refspec syntax SRC:TGT pushes local SRC to remote TGT branch."""
    (local_git_ctx.local_dir / "local_file.txt").write_text("from local")
    run_git_command(local_git_ctx.local_dir, "add", "local_file.txt")
    run_git_command(local_git_ctx.local_dir, "commit", "-m", "Local commit")

    git_push(
        local_path=local_git_ctx.local_dir,
        remote_host=local_git_ctx.host,
        remote_path=local_git_ctx.agent_dir,
        extra_args=("main:feature-branch",),
        cg=cg,
    )

    # The branch should now exist on the agent side
    result = subprocess.run(
        ["git", "-C", str(local_git_ctx.agent_dir), "rev-parse", "--verify", "refs/heads/feature-branch"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0


def test_git_push_force_overwrites_diverged_history(
    local_git_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """--force is passed through and allows overwriting diverged remote history."""
    (local_git_ctx.agent_dir / "agent_file.txt").write_text("agent")
    run_git_command(local_git_ctx.agent_dir, "add", "agent_file.txt")
    run_git_command(local_git_ctx.agent_dir, "commit", "-m", "Agent commit")

    (local_git_ctx.local_dir / "local_file.txt").write_text("local")
    run_git_command(local_git_ctx.local_dir, "add", "local_file.txt")
    run_git_command(local_git_ctx.local_dir, "commit", "-m", "Local commit")

    git_push(
        local_path=local_git_ctx.local_dir,
        remote_host=local_git_ctx.host,
        remote_path=local_git_ctx.agent_dir,
        extra_args=("--force", "main"),
        cg=cg,
    )

    assert (local_git_ctx.agent_dir / "local_file.txt").exists()
    assert not (local_git_ctx.agent_dir / "agent_file.txt").exists()
