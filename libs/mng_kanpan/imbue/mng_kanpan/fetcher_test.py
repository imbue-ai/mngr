from datetime import datetime
from datetime import timezone
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

from imbue.mng.interfaces.data_types import AgentInfo
from imbue.mng.interfaces.data_types import HostInfo
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import HostId
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng_kanpan.data_types import CheckStatus
from imbue.mng_kanpan.data_types import PrInfo
from imbue.mng_kanpan.data_types import PrState
from imbue.mng_kanpan.fetcher import _build_pr_branch_index
from imbue.mng_kanpan.fetcher import _find_git_cwd
from imbue.mng_kanpan.fetcher import _pr_priority
from imbue.mng_kanpan.fetcher import _resolve_agent_branch
from imbue.mng_kanpan.fetcher import fetch_board_snapshot
from imbue.mng_kanpan.github import FetchPrsResult


def _make_host_info(provider_name: str = "local") -> HostInfo:
    """Create a minimal HostInfo for testing."""
    return HostInfo(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName(provider_name),
    )


def _make_agent_info(
    name: str = "test-agent",
    state: AgentLifecycleState = AgentLifecycleState.RUNNING,
    work_dir: Path = Path("/tmp/test-work-dir"),
    provider_name: str = "local",
) -> AgentInfo:
    """Create a minimal AgentInfo for testing."""
    return AgentInfo(
        id=AgentId.generate(),
        name=AgentName(name),
        type="claude",
        command=CommandString("claude"),
        work_dir=work_dir,
        create_time=datetime.now(tz=timezone.utc),
        start_on_boot=False,
        state=state,
        host=_make_host_info(provider_name),
    )


def _make_pr_info(
    number: int = 1,
    head_branch: str = "mng/test-local",
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
    pr = _make_pr_info(number=1, head_branch="mng/agent-local")
    result = _build_pr_branch_index((pr,))
    assert result == {"mng/agent-local": pr}


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
    agent = _make_agent_info(name="my-agent", work_dir=tmp_path, provider_name="local")
    cg = MagicMock()
    with patch("imbue.mng_kanpan.fetcher.get_current_git_branch", return_value="mng/my-agent-local"):
        branch = _resolve_agent_branch(agent, cg)
    assert branch == "mng/my-agent-local"


def test_resolve_agent_branch_local_nonexistent_dir() -> None:
    agent = _make_agent_info(name="my-agent", work_dir=Path("/nonexistent/path"), provider_name="local")
    cg = MagicMock()
    branch = _resolve_agent_branch(agent, cg)
    assert branch == "mng/my-agent-local"


def test_resolve_agent_branch_local_git_fails(tmp_path: Path) -> None:
    agent = _make_agent_info(name="my-agent", work_dir=tmp_path, provider_name="local")
    cg = MagicMock()
    with patch("imbue.mng_kanpan.fetcher.get_current_git_branch", return_value=None):
        branch = _resolve_agent_branch(agent, cg)
    assert branch == "mng/my-agent-local"


def test_resolve_agent_branch_remote() -> None:
    agent = _make_agent_info(name="my-agent", work_dir=Path("/remote/path"), provider_name="modal")
    cg = MagicMock()
    branch = _resolve_agent_branch(agent, cg)
    assert branch == "mng/my-agent-modal"


# === fetch_board_snapshot ===


def test_find_git_cwd_returns_first_local_work_dir(tmp_path: Path) -> None:
    agent = _make_agent_info(name="a", work_dir=tmp_path, provider_name="local")
    assert _find_git_cwd([agent]) == tmp_path


def test_find_git_cwd_skips_remote_agents() -> None:
    agent = _make_agent_info(name="a", work_dir=Path("/remote"), provider_name="modal")
    assert _find_git_cwd([agent]) is None


def test_find_git_cwd_skips_nonexistent_dirs() -> None:
    agent = _make_agent_info(name="a", work_dir=Path("/nonexistent"), provider_name="local")
    assert _find_git_cwd([agent]) is None


def test_find_git_cwd_empty_agents() -> None:
    assert _find_git_cwd([]) is None


# === fetch_board_snapshot ===


def test_fetch_board_snapshot_integrates_agents_and_prs() -> None:
    agent1 = _make_agent_info(name="agent-1", state=AgentLifecycleState.RUNNING, provider_name="modal")
    agent2 = _make_agent_info(name="agent-2", state=AgentLifecycleState.DONE, provider_name="modal")

    pr1 = _make_pr_info(number=42, head_branch="mng/agent-1-modal", state=PrState.OPEN)
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


def test_fetch_board_snapshot_surfaces_gh_errors() -> None:
    mock_list_result = MagicMock()
    mock_list_result.agents = []
    mock_list_result.errors = []

    pr_result = FetchPrsResult(prs=(), error="gh pr list failed: not a git repository")

    mng_ctx = MagicMock()
    mng_ctx.concurrency_group = MagicMock()

    with (
        patch("imbue.mng_kanpan.fetcher.list_agents", return_value=mock_list_result),
        patch("imbue.mng_kanpan.fetcher.fetch_all_prs", return_value=pr_result),
    ):
        snapshot = fetch_board_snapshot(mng_ctx)

    assert len(snapshot.errors) == 1
    assert "gh pr list failed" in snapshot.errors[0]
