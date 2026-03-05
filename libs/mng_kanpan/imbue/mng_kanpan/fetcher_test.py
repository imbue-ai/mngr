import subprocess
from datetime import datetime
from datetime import timezone
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

from urwid.widget.attr_map import AttrMap
from urwid.widget.text import Text

from imbue.mng.interfaces.data_types import AgentDetails
from imbue.mng.interfaces.data_types import HostDetails
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import HostId
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.utils.testing import init_git_repo_with_config
from imbue.mng_kanpan.data_types import AgentBoardEntry
from imbue.mng_kanpan.data_types import BoardSnapshot
from imbue.mng_kanpan.data_types import CheckStatus
from imbue.mng_kanpan.data_types import PrInfo
from imbue.mng_kanpan.data_types import PrState
from imbue.mng_kanpan.fetcher import _build_pr_branch_index
from imbue.mng_kanpan.fetcher import _find_git_cwd
from imbue.mng_kanpan.fetcher import _pr_priority
from imbue.mng_kanpan.fetcher import _resolve_agent_branch
from imbue.mng_kanpan.fetcher import fetch_board_snapshot
from imbue.mng_kanpan.github import FetchPrsResult
from imbue.mng_kanpan.tui import _build_board_widgets
from imbue.mng_kanpan.tui import _carry_forward_pr_data


def _make_host_details(provider_name: str = "local") -> HostDetails:
    """Create a minimal HostDetails for testing."""
    return HostDetails(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName(provider_name),
    )


def _make_agent_details(
    name: str = "test-agent",
    state: AgentLifecycleState = AgentLifecycleState.RUNNING,
    work_dir: Path = Path("/tmp/test-work-dir"),
    provider_name: str = "local",
) -> AgentDetails:
    """Create a minimal AgentDetails for testing."""
    return AgentDetails(
        id=AgentId.generate(),
        name=AgentName(name),
        type="claude",
        command=CommandString("claude"),
        work_dir=work_dir,
        create_time=datetime.now(tz=timezone.utc),
        start_on_boot=False,
        state=state,
        host=_make_host_details(provider_name),
    )


def _make_pr_info(
    number: int = 1,
    head_branch: str = "mng/test",
    state: PrState = PrState.OPEN,
    is_draft: bool = False,
) -> PrInfo:
    """Create a minimal PrInfo for testing."""
    return PrInfo(
        number=number,
        title=f"PR #{number}",
        state=state,
        url=f"https://github.com/org/repo/pull/{number}",
        head_branch=head_branch,
        check_status=CheckStatus.PASSING,
        is_draft=is_draft,
    )


# === _pr_priority ===


def test_pr_priority_open() -> None:
    assert _pr_priority(_make_pr_info(state=PrState.OPEN)) == 2


def test_pr_priority_merged() -> None:
    assert _pr_priority(_make_pr_info(state=PrState.MERGED)) == 1


def test_pr_priority_closed() -> None:
    assert _pr_priority(_make_pr_info(state=PrState.CLOSED)) == 0


# === _build_pr_branch_index ===


def test_build_pr_branch_index_empty() -> None:
    result = _build_pr_branch_index(())
    assert result == {}


def test_build_pr_branch_index_single_pr() -> None:
    pr = _make_pr_info(number=1, head_branch="mng/agent")
    result = _build_pr_branch_index((pr,))
    assert result == {"mng/agent": pr}


def test_build_pr_branch_index_different_branches() -> None:
    pr1 = _make_pr_info(number=1, head_branch="branch-a")
    pr2 = _make_pr_info(number=2, head_branch="branch-b")
    result = _build_pr_branch_index((pr1, pr2))
    assert len(result) == 2
    assert result["branch-a"] == pr1
    assert result["branch-b"] == pr2


def test_build_pr_branch_index_open_wins_over_closed() -> None:
    closed_pr = _make_pr_info(number=1, head_branch="branch-a", state=PrState.CLOSED)
    open_pr = _make_pr_info(number=2, head_branch="branch-a", state=PrState.OPEN)
    result = _build_pr_branch_index((closed_pr, open_pr))
    assert result["branch-a"].number == 2


def test_build_pr_branch_index_open_wins_over_merged() -> None:
    merged_pr = _make_pr_info(number=1, head_branch="branch-a", state=PrState.MERGED)
    open_pr = _make_pr_info(number=2, head_branch="branch-a", state=PrState.OPEN)
    result = _build_pr_branch_index((merged_pr, open_pr))
    assert result["branch-a"].number == 2


def test_build_pr_branch_index_merged_wins_over_closed() -> None:
    closed_pr = _make_pr_info(number=1, head_branch="branch-a", state=PrState.CLOSED)
    merged_pr = _make_pr_info(number=2, head_branch="branch-a", state=PrState.MERGED)
    result = _build_pr_branch_index((closed_pr, merged_pr))
    assert result["branch-a"].number == 2


# === _resolve_agent_branch ===


def test_resolve_agent_branch_local_with_git(tmp_path: Path) -> None:
    agent = _make_agent_details(name="my-agent", work_dir=tmp_path, provider_name="local")
    cg = MagicMock()
    with patch("imbue.mng_kanpan.fetcher.get_current_git_branch", return_value="mng/my-agent"):
        branch = _resolve_agent_branch(agent, cg)
    assert branch == "mng/my-agent"


def test_resolve_agent_branch_local_nonexistent_dir() -> None:
    agent = _make_agent_details(name="my-agent", work_dir=Path("/nonexistent/path"), provider_name="local")
    cg = MagicMock()
    branch = _resolve_agent_branch(agent, cg)
    assert branch == "mng/my-agent"


def test_resolve_agent_branch_local_git_fails(tmp_path: Path) -> None:
    agent = _make_agent_details(name="my-agent", work_dir=tmp_path, provider_name="local")
    cg = MagicMock()
    with patch("imbue.mng_kanpan.fetcher.get_current_git_branch", return_value=None):
        branch = _resolve_agent_branch(agent, cg)
    assert branch == "mng/my-agent"


# === fetch_board_snapshot ===


def test_find_git_cwd_returns_first_local_work_dir(tmp_path: Path) -> None:
    agent = _make_agent_details(name="a", work_dir=tmp_path, provider_name="local")
    assert _find_git_cwd([agent]) == tmp_path


def test_find_git_cwd_skips_remote_agents() -> None:
    agent = _make_agent_details(name="a", work_dir=Path("/remote"), provider_name="modal")
    assert _find_git_cwd([agent]) is None


def test_find_git_cwd_skips_nonexistent_dirs() -> None:
    agent = _make_agent_details(name="a", work_dir=Path("/nonexistent"), provider_name="local")
    assert _find_git_cwd([agent]) is None


def test_find_git_cwd_empty_agents() -> None:
    assert _find_git_cwd([]) is None


# === fetch_board_snapshot ===


def test_fetch_board_snapshot_integrates_agents_and_prs() -> None:
    agent1 = _make_agent_details(name="agent-1", state=AgentLifecycleState.RUNNING, provider_name="modal")
    agent2 = _make_agent_details(name="agent-2", state=AgentLifecycleState.DONE, provider_name="modal")

    pr1 = _make_pr_info(number=42, head_branch="mng/agent-1", state=PrState.OPEN)
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


def test_fetch_board_snapshot_surfaces_gh_errors_and_suppresses_create_pr_url(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    init_git_repo_with_config(repo_dir)
    # Add a GitHub remote so _get_github_repo_path succeeds
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:org/repo.git"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )

    agent = _make_agent_details(name="agent-1", work_dir=repo_dir, provider_name="local")

    pr_result = FetchPrsResult(prs=(), error="gh pr list failed: auth required")

    mock_list_result = MagicMock()
    mock_list_result.agents = [agent]
    mock_list_result.errors = []

    mng_ctx = MagicMock()
    mng_ctx.concurrency_group = MagicMock()

    with (
        patch("imbue.mng_kanpan.fetcher.list_agents", return_value=mock_list_result),
        patch("imbue.mng_kanpan.fetcher.fetch_all_prs", return_value=pr_result),
        patch("imbue.mng_kanpan.fetcher.get_current_git_branch", return_value="mng/agent-1"),
    ):
        snapshot = fetch_board_snapshot(mng_ctx)

    assert len(snapshot.errors) == 1
    assert "gh pr list failed" in snapshot.errors[0]
    assert snapshot.prs_loaded is False
    assert snapshot.entries[0].branch == "mng/agent-1"
    # When PRs failed to load, create_pr_url should be suppressed even though
    # the agent has a branch and a valid GitHub remote
    assert snapshot.entries[0].create_pr_url is None


# === _carry_forward_pr_data ===


def test_carry_forward_pr_data_preserves_old_prs() -> None:
    pr = _make_pr_info(number=42, head_branch="mng/agent-1")
    old_entry = AgentBoardEntry(
        name=AgentName("agent-1"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("modal"),
        branch="mng/agent-1",
        pr=pr,
        create_pr_url=None,
    )
    old = BoardSnapshot(entries=(old_entry,), prs_loaded=True, fetch_time_seconds=1.0)

    new_entry = AgentBoardEntry(
        name=AgentName("agent-1"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("modal"),
        branch="mng/agent-1",
        pr=None,
        create_pr_url=None,
    )
    new = BoardSnapshot(
        entries=(new_entry,),
        errors=("gh auth failed",),
        prs_loaded=False,
        fetch_time_seconds=2.0,
    )

    result = _carry_forward_pr_data(old, new)
    assert result.prs_loaded is True
    assert result.entries[0].pr is not None
    assert result.entries[0].pr.number == 42
    # Errors from the failed fetch are still preserved
    assert "gh auth failed" in result.errors[0]
    # Timing comes from the new snapshot
    assert result.fetch_time_seconds == 2.0


def test_carry_forward_pr_data_handles_new_agents() -> None:
    """New agents that weren't in the old snapshot get no PR data carried forward."""
    old = BoardSnapshot(entries=(), prs_loaded=True, fetch_time_seconds=1.0)

    new_entry = AgentBoardEntry(
        name=AgentName("agent-new"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("modal"),
        branch="mng/agent-new",
    )
    new = BoardSnapshot(entries=(new_entry,), prs_loaded=False, fetch_time_seconds=2.0)

    result = _carry_forward_pr_data(old, new)
    assert result.entries[0].pr is None


# === _build_board_widgets: first-load PR failure ===


def _extract_text(walker: list[object]) -> list[str]:
    """Extract plain text from all Text widgets in a walker."""
    texts: list[str] = []
    for widget in walker:
        inner = widget._original_widget if isinstance(widget, AttrMap) else widget
        if not isinstance(inner, Text):
            continue
        raw = inner.text
        if isinstance(raw, str):
            texts.append(raw)
        else:
            parts = []
            for seg in raw:
                if isinstance(seg, tuple):
                    parts.append(str(seg[1]))
                else:
                    parts.append(str(seg))
            texts.append("".join(parts))
    return texts


def _text_contains(texts: list[str], substring: str) -> bool:
    return any(substring in t for t in texts)


def test_first_load_pr_failure_shows_prs_not_loaded() -> None:
    """When the first load fails to fetch PRs, the heading should say 'PRs not loaded'
    and no create-PR links should appear."""
    entry = AgentBoardEntry(
        name=AgentName("agent-1"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("modal"),
        branch="mng/agent-1",
        pr=None,
        create_pr_url=None,
    )
    snapshot = BoardSnapshot(
        entries=(entry,),
        errors=("gh pr list failed: auth required",),
        prs_loaded=False,
        fetch_time_seconds=1.0,
    )
    walker, _ = _build_board_widgets(snapshot)

    texts = _extract_text(list(walker))
    assert _text_contains(texts, "PRs not loaded")
    assert not _text_contains(texts, "no PR yet")
    assert not _text_contains(texts, "create PR")
    assert _text_contains(texts, "gh pr list failed")


def test_first_load_pr_success_shows_normal_heading() -> None:
    """When PRs load successfully, agents without PRs show normal 'no PR yet' heading."""
    entry = AgentBoardEntry(
        name=AgentName("agent-1"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("modal"),
        branch="mng/agent-1",
        pr=None,
        create_pr_url="https://github.com/org/repo/compare/mng/agent-1?expand=1",
    )
    snapshot = BoardSnapshot(
        entries=(entry,),
        prs_loaded=True,
        fetch_time_seconds=1.0,
    )
    walker, _ = _build_board_widgets(snapshot)

    texts = _extract_text(list(walker))
    assert _text_contains(texts, "no PR yet")
    assert not _text_contains(texts, "PRs not loaded")


def test_second_load_pr_failure_shows_carried_forward_prs() -> None:
    """When the second load fails to fetch PRs, carry-forward preserves PR data
    and the TUI shows normal PR info (not 'PRs not loaded')."""
    pr = _make_pr_info(number=42, head_branch="mng/agent-1")
    old_entry = AgentBoardEntry(
        name=AgentName("agent-1"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("modal"),
        branch="mng/agent-1",
        pr=pr,
        create_pr_url=None,
    )
    old = BoardSnapshot(entries=(old_entry,), prs_loaded=True, fetch_time_seconds=1.0)

    new_entry = AgentBoardEntry(
        name=AgentName("agent-1"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("modal"),
        branch="mng/agent-1",
        pr=None,
        create_pr_url=None,
    )
    new = BoardSnapshot(
        entries=(new_entry,),
        errors=("gh pr list failed: network error",),
        prs_loaded=False,
        fetch_time_seconds=2.0,
    )

    carried = _carry_forward_pr_data(old, new)
    walker, _ = _build_board_widgets(carried)

    texts = _extract_text(list(walker))
    # Carried-forward PR data renders the same as a normal successful load
    assert _text_contains(texts, "github.com/org/repo/pull/42")
    assert not _text_contains(texts, "PRs not loaded")
    assert not _text_contains(texts, "no PR yet")
    assert not _text_contains(texts, "create PR")
    # Error from the failed fetch is still visible
    assert _text_contains(texts, "network error")
