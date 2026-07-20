"""Unit tests for branch-bundle retrieval (shared by all map-reduce recipes)."""

import subprocess
from pathlib import Path

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import OutputFormat
from imbue.mngr_mapreduce.data_types import MapReduceContext
from imbue.mngr_mapreduce.data_types import ReducerInfo
from imbue.mngr_tmr.branch_bundles import BRANCH_BUNDLE_NAME
from imbue.mngr_tmr.branch_bundles import apply_agent_branch_bundle_if_present
from imbue.mngr_tmr.branch_bundles import finalize_reducer_branch
from imbue.mngr_tmr.branch_bundles import has_local_branch


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _make_repo_with_bundle(tmp_path: Path) -> tuple[Path, Path, str]:
    """Create a repo with a feature branch, bundle it, and delete the local branch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "--allow-empty", "-m", "base")
    _git(repo, "checkout", "-b", "feature-branch")
    _git(repo, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "--allow-empty", "-m", "work")
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    _git(repo, "bundle", "create", str(agent_dir / BRANCH_BUNDLE_NAME), "main..feature-branch")
    _git(repo, "checkout", "main")
    _git(repo, "branch", "-D", "feature-branch")
    return repo, agent_dir, "feature-branch"


def test_apply_agent_branch_bundle_fetches_the_branch(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    repo, agent_dir, branch_name = _make_repo_with_bundle(tmp_path)
    assert not has_local_branch(repo, branch_name, cg)

    applied = apply_agent_branch_bundle_if_present(repo, agent_dir, branch_name, "agent-1", cg)

    assert applied is True
    assert has_local_branch(repo, branch_name, cg)
    # Idempotent: applying the same bundle again still succeeds.
    assert apply_agent_branch_bundle_if_present(repo, agent_dir, branch_name, "agent-1", cg) is True


def test_apply_agent_branch_bundle_returns_false_without_a_bundle(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    repo, _, branch_name = _make_repo_with_bundle(tmp_path)
    empty_agent_dir = tmp_path / "empty-agent"
    empty_agent_dir.mkdir()

    applied = apply_agent_branch_bundle_if_present(repo, empty_agent_dir, branch_name, "agent-1", cg)

    assert applied is False
    assert not has_local_branch(repo, branch_name, cg)


def test_finalize_reducer_branch_applies_the_bundle(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    repo, agent_dir, branch_name = _make_repo_with_bundle(tmp_path)
    ctx = MapReduceContext(
        mngr_ctx=temp_mngr_ctx,
        source_dir=repo,
        run_name="20260101000000",
        output_dir=tmp_path / "out",
        output_opts=OutputOptions(output_format=OutputFormat.HUMAN),
    )
    info = ReducerInfo(
        agent_id=AgentId.generate(),
        agent_name=AgentName("reducer-1"),
        branch_name=branch_name,
    )

    finalize_reducer_branch(ctx, agent_dir, info)

    assert has_local_branch(repo, branch_name, ctx.cg)
