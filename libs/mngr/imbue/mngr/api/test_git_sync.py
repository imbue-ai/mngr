import subprocess
from pathlib import Path
from typing import cast

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.api.sync import GitSyncError
from imbue.mngr.api.sync import UncommittedChangesError
from imbue.mngr.api.sync import git_pull
from imbue.mngr.api.sync import git_push
from imbue.mngr.api.testing import FakeAgent
from imbue.mngr.api.testing import FakeHost
from imbue.mngr.api.testing import SyncTestContext
from imbue.mngr.api.testing import has_uncommitted_changes
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import UncommittedChangesMode
from imbue.mngr.utils.git_utils import get_current_branch
from imbue.mngr.utils.testing import get_stash_count
from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import run_git_command


@pytest.fixture
def local_git_ctx(tmp_path: Path) -> SyncTestContext:
    """Two git repos that share history (cloned from one another)."""
    agent_dir = tmp_path / "agent"
    local_dir = tmp_path / "host"

    init_git_repo(agent_dir)

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


@pytest.fixture
def remote_git_ctx(tmp_path: Path) -> SyncTestContext:
    """Same as local_git_ctx but with host marked is_local=False."""
    agent_dir = tmp_path / "agent"
    local_dir = tmp_path / "host"

    init_git_repo(agent_dir)

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


# =============================================================================
# git_pull
# =============================================================================


def test_git_pull_uses_remote_branch_as_default_source(
    local_git_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    run_git_command(local_git_ctx.agent_dir, "checkout", "-b", "feature-branch")
    (local_git_ctx.agent_dir / "feature.txt").write_text("feature")
    run_git_command(local_git_ctx.agent_dir, "add", "feature.txt")
    run_git_command(local_git_ctx.agent_dir, "commit", "-m", "Feature commit")

    run_git_command(local_git_ctx.local_dir, "checkout", "-b", "feature-branch")

    result = git_pull(
        local_path=local_git_ctx.local_dir,
        remote_host=local_git_ctx.host,
        remote_path=local_git_ctx.agent_dir,
        source_branch=None,
        target_branch=None,
        is_dry_run=True,
        uncommitted_changes=UncommittedChangesMode.FAIL,
        cg=cg,
    )

    assert result.source_branch == "feature-branch"


def test_git_pull_uses_local_branch_as_default_target(
    local_git_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    (local_git_ctx.agent_dir / "new.txt").write_text("new")
    run_git_command(local_git_ctx.agent_dir, "add", "new.txt")
    run_git_command(local_git_ctx.agent_dir, "commit", "-m", "New commit")

    result = git_pull(
        local_path=local_git_ctx.local_dir,
        remote_host=local_git_ctx.host,
        remote_path=local_git_ctx.agent_dir,
        source_branch=None,
        target_branch=None,
        is_dry_run=True,
        uncommitted_changes=UncommittedChangesMode.FAIL,
        cg=cg,
    )

    assert result.target_branch == "main"


def test_git_pull_dry_run_does_not_merge(
    local_git_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    for i in range(3):
        (local_git_ctx.agent_dir / f"file{i}.txt").write_text(f"content{i}")
        run_git_command(local_git_ctx.agent_dir, "add", f"file{i}.txt")
        run_git_command(local_git_ctx.agent_dir, "commit", "-m", f"Commit {i}")

    result = git_pull(
        local_path=local_git_ctx.local_dir,
        remote_host=local_git_ctx.host,
        remote_path=local_git_ctx.agent_dir,
        source_branch=None,
        target_branch=None,
        is_dry_run=True,
        uncommitted_changes=UncommittedChangesMode.FAIL,
        cg=cg,
    )

    assert result.is_dry_run is True
    assert result.commits_transferred == 3
    assert not (local_git_ctx.local_dir / "file0.txt").exists()


def test_git_pull_merge_mode_stashes_and_restores(
    local_git_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    (local_git_ctx.agent_dir / "agent_file.txt").write_text("from agent")
    run_git_command(local_git_ctx.agent_dir, "add", "agent_file.txt")
    run_git_command(local_git_ctx.agent_dir, "commit", "-m", "Agent commit")

    (local_git_ctx.local_dir / "README.md").write_text("uncommitted local change")

    git_pull(
        local_path=local_git_ctx.local_dir,
        remote_host=local_git_ctx.host,
        remote_path=local_git_ctx.agent_dir,
        source_branch=None,
        target_branch=None,
        is_dry_run=False,
        uncommitted_changes=UncommittedChangesMode.MERGE,
        cg=cg,
    )

    assert (local_git_ctx.local_dir / "agent_file.txt").exists()
    assert "uncommitted local change" in (local_git_ctx.local_dir / "README.md").read_text()
    assert get_stash_count(local_git_ctx.local_dir) == 0


def test_git_pull_stash_mode_leaves_stashed(
    local_git_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    (local_git_ctx.agent_dir / "agent_file.txt").write_text("from agent")
    run_git_command(local_git_ctx.agent_dir, "add", "agent_file.txt")
    run_git_command(local_git_ctx.agent_dir, "commit", "-m", "Agent commit")

    (local_git_ctx.local_dir / "README.md").write_text("uncommitted local change")

    git_pull(
        local_path=local_git_ctx.local_dir,
        remote_host=local_git_ctx.host,
        remote_path=local_git_ctx.agent_dir,
        source_branch=None,
        target_branch=None,
        is_dry_run=False,
        uncommitted_changes=UncommittedChangesMode.STASH,
        cg=cg,
    )

    assert (local_git_ctx.local_dir / "agent_file.txt").exists()
    assert get_stash_count(local_git_ctx.local_dir) == 1


def test_git_pull_raises_on_merge_failure(
    local_git_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    (local_git_ctx.agent_dir / "README.md").write_text("agent version of README")
    run_git_command(local_git_ctx.agent_dir, "add", "README.md")
    run_git_command(local_git_ctx.agent_dir, "commit", "-m", "Agent change to README")

    (local_git_ctx.local_dir / "README.md").write_text("host version of README")
    run_git_command(local_git_ctx.local_dir, "add", "README.md")
    run_git_command(local_git_ctx.local_dir, "commit", "-m", "Host change to README")

    with pytest.raises(GitSyncError):
        git_pull(
            local_path=local_git_ctx.local_dir,
            remote_host=local_git_ctx.host,
            remote_path=local_git_ctx.agent_dir,
            source_branch=None,
            target_branch=None,
            is_dry_run=False,
            uncommitted_changes=UncommittedChangesMode.FAIL,
            cg=cg,
        )


def test_git_pull_fails_with_uncommitted_changes_in_fail_mode(
    local_git_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    (local_git_ctx.local_dir / "uncommitted.txt").write_text("uncommitted")
    assert has_uncommitted_changes(local_git_ctx.local_dir, cg)

    with pytest.raises(UncommittedChangesError):
        git_pull(
            local_path=local_git_ctx.local_dir,
            remote_host=local_git_ctx.host,
            remote_path=local_git_ctx.agent_dir,
            source_branch=None,
            target_branch=None,
            is_dry_run=False,
            uncommitted_changes=UncommittedChangesMode.FAIL,
            cg=cg,
        )


def test_git_pull_merge_mode_restores_stash_on_original_branch(
    remote_git_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that git_pull with MERGE mode restores stash on original branch."""
    local_dir = remote_git_ctx.local_dir
    agent_dir = remote_git_ctx.agent_dir

    run_git_command(agent_dir, "checkout", "-b", "feature-branch")
    (agent_dir / "feature.txt").write_text("feature content")
    run_git_command(agent_dir, "add", "feature.txt")
    run_git_command(agent_dir, "commit", "-m", "Add feature")

    original_branch = get_current_branch(local_dir, cg)
    run_git_command(local_dir, "checkout", "-b", "feature-branch")
    run_git_command(local_dir, "checkout", original_branch)

    (local_dir / "README.md").write_text("uncommitted change")
    assert has_uncommitted_changes(local_dir, cg)

    result = git_pull(
        local_path=local_dir,
        remote_host=remote_git_ctx.host,
        remote_path=agent_dir,
        source_branch="feature-branch",
        target_branch="feature-branch",
        is_dry_run=False,
        uncommitted_changes=UncommittedChangesMode.MERGE,
        cg=cg,
    )

    assert result.commits_transferred > 0
    current_branch = get_current_branch(local_dir, cg)
    assert current_branch == original_branch
    assert (local_dir / "README.md").read_text() == "uncommitted change"


# =============================================================================
# git_push
# =============================================================================


def test_git_push_pushes_local_commits_to_remote(
    local_git_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    (local_git_ctx.local_dir / "new.txt").write_text("new")
    run_git_command(local_git_ctx.local_dir, "add", "new.txt")
    run_git_command(local_git_ctx.local_dir, "commit", "-m", "Local commit")

    result = git_push(
        local_path=local_git_ctx.local_dir,
        remote_host=local_git_ctx.host,
        remote_path=local_git_ctx.agent_dir,
        source_branch=None,
        target_branch=None,
        is_dry_run=False,
        uncommitted_changes=UncommittedChangesMode.FAIL,
        is_mirror=False,
        cg=cg,
    )

    assert result.commits_transferred == 1
    assert (local_git_ctx.agent_dir / "new.txt").read_text() == "new"


def test_git_push_uses_local_branch_as_default_source(
    local_git_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    (local_git_ctx.local_dir / "x.txt").write_text("x")
    run_git_command(local_git_ctx.local_dir, "add", "x.txt")
    run_git_command(local_git_ctx.local_dir, "commit", "-m", "Local commit")

    result = git_push(
        local_path=local_git_ctx.local_dir,
        remote_host=local_git_ctx.host,
        remote_path=local_git_ctx.agent_dir,
        source_branch=None,
        target_branch=None,
        is_dry_run=True,
        uncommitted_changes=UncommittedChangesMode.FAIL,
        is_mirror=False,
        cg=cg,
    )

    assert result.source_branch == "main"


def test_git_push_dry_run_does_not_modify_remote(
    local_git_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    (local_git_ctx.local_dir / "x.txt").write_text("x")
    run_git_command(local_git_ctx.local_dir, "add", "x.txt")
    run_git_command(local_git_ctx.local_dir, "commit", "-m", "Local commit")

    result = git_push(
        local_path=local_git_ctx.local_dir,
        remote_host=local_git_ctx.host,
        remote_path=local_git_ctx.agent_dir,
        source_branch=None,
        target_branch=None,
        is_dry_run=True,
        uncommitted_changes=UncommittedChangesMode.FAIL,
        is_mirror=False,
        cg=cg,
    )

    assert result.is_dry_run is True
    assert not (local_git_ctx.agent_dir / "x.txt").exists()


def test_git_push_rejects_non_fast_forward(
    local_git_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
    """Test that git_push refuses non-fast-forward pushes without --mirror."""
    (local_git_ctx.agent_dir / "agent_file.txt").write_text("agent")
    run_git_command(local_git_ctx.agent_dir, "add", "agent_file.txt")
    run_git_command(local_git_ctx.agent_dir, "commit", "-m", "Agent commit")

    (local_git_ctx.local_dir / "local_file.txt").write_text("local")
    run_git_command(local_git_ctx.local_dir, "add", "local_file.txt")
    run_git_command(local_git_ctx.local_dir, "commit", "-m", "Local commit")

    with pytest.raises(GitSyncError, match="diverged"):
        git_push(
            local_path=local_git_ctx.local_dir,
            remote_host=local_git_ctx.host,
            remote_path=local_git_ctx.agent_dir,
            source_branch=None,
            target_branch=None,
            is_dry_run=False,
            uncommitted_changes=UncommittedChangesMode.FAIL,
            is_mirror=False,
            cg=cg,
        )


def test_git_push_mirror_overwrites_diverged_history(
    local_git_ctx: SyncTestContext,
    cg: ConcurrencyGroup,
) -> None:
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
        source_branch=None,
        target_branch=None,
        is_dry_run=False,
        uncommitted_changes=UncommittedChangesMode.FAIL,
        is_mirror=True,
        cg=cg,
    )

    assert (local_git_ctx.agent_dir / "local_file.txt").exists()
    assert not (local_git_ctx.agent_dir / "agent_file.txt").exists()
