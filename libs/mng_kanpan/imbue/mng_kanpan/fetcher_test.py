import subprocess
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentName
from imbue.mng.utils.testing import init_git_repo_with_config
from imbue.mng_kanpan.data_types import PrState
from imbue.mng_kanpan.fetcher import _build_pr_branch_index
from imbue.mng_kanpan.fetcher import _find_git_cwd
from imbue.mng_kanpan.fetcher import _pr_priority
from imbue.mng_kanpan.fetcher import fetch_agent_snapshot
from imbue.mng_kanpan.fetcher import fetch_board_snapshot
from imbue.mng_kanpan.github import FetchPrsResult
from imbue.mng_kanpan.testing import make_agent_details
from imbue.mng_kanpan.testing import make_pr_info

# === _pr_priority ===


def test_pr_priority_open() -> None:
    assert _pr_priority(make_pr_info(state=PrState.OPEN)) == 2


def test_pr_priority_merged() -> None:
    assert _pr_priority(make_pr_info(state=PrState.MERGED)) == 1


def test_pr_priority_closed() -> None:
    assert _pr_priority(make_pr_info(state=PrState.CLOSED)) == 0


# === _build_pr_branch_index ===


def test_build_pr_branch_index_empty() -> None:
    result = _build_pr_branch_index(())
    assert result == {}


def test_build_pr_branch_index_single_pr() -> None:
    pr = make_pr_info(number=1, head_branch="mng/agent")
    result = _build_pr_branch_index((pr,))
    assert result == {"mng/agent": pr}


def test_build_pr_branch_index_different_branches() -> None:
    pr1 = make_pr_info(number=1, head_branch="branch-a")
    pr2 = make_pr_info(number=2, head_branch="branch-b")
    result = _build_pr_branch_index((pr1, pr2))
    assert len(result) == 2
    assert result["branch-a"] == pr1
    assert result["branch-b"] == pr2


def test_build_pr_branch_index_open_wins_over_closed() -> None:
    closed_pr = make_pr_info(number=1, head_branch="branch-a", state=PrState.CLOSED)
    open_pr = make_pr_info(number=2, head_branch="branch-a", state=PrState.OPEN)
    result = _build_pr_branch_index((closed_pr, open_pr))
    assert result["branch-a"].number == 2


def test_build_pr_branch_index_open_wins_over_merged() -> None:
    merged_pr = make_pr_info(number=1, head_branch="branch-a", state=PrState.MERGED)
    open_pr = make_pr_info(number=2, head_branch="branch-a", state=PrState.OPEN)
    result = _build_pr_branch_index((merged_pr, open_pr))
    assert result["branch-a"].number == 2


def test_build_pr_branch_index_merged_wins_over_closed() -> None:
    closed_pr = make_pr_info(number=1, head_branch="branch-a", state=PrState.CLOSED)
    merged_pr = make_pr_info(number=2, head_branch="branch-a", state=PrState.MERGED)
    result = _build_pr_branch_index((closed_pr, merged_pr))
    assert result["branch-a"].number == 2


# === fetch_board_snapshot ===


def test_find_git_cwd_returns_first_local_work_dir(tmp_path: Path) -> None:
    agent = make_agent_details(name="a", work_dir=tmp_path, provider_name="local")
    assert _find_git_cwd([agent]) == tmp_path


def test_find_git_cwd_skips_remote_agents() -> None:
    agent = make_agent_details(name="a", work_dir=Path("/remote"), provider_name="modal")
    assert _find_git_cwd([agent]) is None


def test_find_git_cwd_skips_nonexistent_dirs() -> None:
    agent = make_agent_details(name="a", work_dir=Path("/nonexistent"), provider_name="local")
    assert _find_git_cwd([agent]) is None


def test_find_git_cwd_empty_agents() -> None:
    assert _find_git_cwd([]) is None


# === fetch_board_snapshot ===


def test_fetch_board_snapshot_integrates_agents_and_prs() -> None:
    agent1 = make_agent_details(
        name="agent-1", state=AgentLifecycleState.RUNNING, provider_name="modal", initial_branch="mng/agent-1"
    )
    agent2 = make_agent_details(name="agent-2", state=AgentLifecycleState.DONE, provider_name="modal")

    pr1 = make_pr_info(number=42, head_branch="mng/agent-1", state=PrState.OPEN)
    pr_result = FetchPrsResult(prs=(pr1,), error=None)

    mock_list_result = MagicMock()
    mock_list_result.agents = [agent1, agent2]
    mock_list_result.errors = []

    mng_ctx = MagicMock()
    mng_ctx.concurrency_group = MagicMock()

    with (
        patch("imbue.mng_kanpan.fetcher.list_agents", return_value=mock_list_result),
        patch("imbue.mng_kanpan.fetcher.fetch_all_prs", return_value=pr_result),
    ):
        snapshot = fetch_board_snapshot(mng_ctx)

    assert len(snapshot.entries) == 2
    assert snapshot.entries[0].name == AgentName("agent-1")
    assert snapshot.entries[0].pr is not None
    assert snapshot.entries[0].pr.number == 42
    assert snapshot.entries[1].name == AgentName("agent-2")
    assert snapshot.entries[1].pr is None
    assert snapshot.errors == ()
    assert snapshot.prs_loaded is True
    assert snapshot.fetch_time_seconds > 0


def test_fetch_board_snapshot_with_list_errors() -> None:
    mock_error = MagicMock()
    mock_error.exception_type = "ConnectionError"
    mock_error.message = "host unreachable"

    mock_list_result = MagicMock()
    mock_list_result.agents = []
    mock_list_result.errors = [mock_error]

    pr_result = FetchPrsResult(prs=(), error=None)

    mng_ctx = MagicMock()
    mng_ctx.concurrency_group = MagicMock()

    with (
        patch("imbue.mng_kanpan.fetcher.list_agents", return_value=mock_list_result),
        patch("imbue.mng_kanpan.fetcher.fetch_all_prs", return_value=pr_result),
    ):
        snapshot = fetch_board_snapshot(mng_ctx)

    assert len(snapshot.entries) == 0
    assert len(snapshot.errors) == 1
    assert "ConnectionError" in snapshot.errors[0]


def test_fetch_agent_snapshot_entries_have_no_pr() -> None:
    """fetch_agent_snapshot should return entries with pr=None and create_pr_url=None."""
    agent1 = make_agent_details(name="agent-1", state=AgentLifecycleState.RUNNING, provider_name="modal")

    mock_list_result = MagicMock()
    mock_list_result.agents = [agent1]
    mock_list_result.errors = []

    mng_ctx = MagicMock()
    mng_ctx.concurrency_group = MagicMock()

    with patch("imbue.mng_kanpan.fetcher.list_agents", return_value=mock_list_result):
        snapshot = fetch_agent_snapshot(mng_ctx)

    assert len(snapshot.entries) == 1
    assert snapshot.entries[0].name == AgentName("agent-1")
    assert snapshot.entries[0].pr is None
    assert snapshot.entries[0].create_pr_url is None
    assert snapshot.prs_loaded is False
    assert snapshot.errors == ()
    assert snapshot.fetch_time_seconds > 0


def test_fetch_board_snapshot_surfaces_gh_errors_and_suppresses_create_pr_url(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    init_git_repo_with_config(repo_dir)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:org/repo.git"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )

    agent = make_agent_details(name="agent-1", work_dir=repo_dir, provider_name="local", initial_branch="mng/agent-1")

    pr_result = FetchPrsResult(prs=(), error="gh pr list failed: auth required")

    mock_list_result = MagicMock()
    mock_list_result.agents = [agent]
    mock_list_result.errors = []

    mng_ctx = MagicMock()
    mng_ctx.concurrency_group = MagicMock()

    with (
        patch("imbue.mng_kanpan.fetcher.list_agents", return_value=mock_list_result),
        patch("imbue.mng_kanpan.fetcher.fetch_all_prs", return_value=pr_result),
        patch("imbue.mng_kanpan.fetcher._get_github_repo_path", return_value="org/repo"),
    ):
        snapshot = fetch_board_snapshot(mng_ctx)

    assert len(snapshot.errors) == 1
    assert "gh pr list failed" in snapshot.errors[0]
    assert snapshot.prs_loaded is False
    assert snapshot.entries[0].branch == "mng/agent-1"
    # When PRs failed to load, create_pr_url should be suppressed even though
    # the agent has a branch and a valid GitHub remote
    assert snapshot.entries[0].create_pr_url is None
