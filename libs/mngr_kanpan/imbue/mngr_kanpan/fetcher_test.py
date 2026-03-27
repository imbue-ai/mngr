from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.testing import init_git_repo_with_config
from imbue.mngr_kanpan.data_types import AgentBoardEntry
from imbue.mngr_kanpan.data_types import BoardSnapshot
from imbue.mngr_kanpan.data_types import ColumnData
from imbue.mngr_kanpan.data_types import GitHubData
from imbue.mngr_kanpan.data_types import PrState
from imbue.mngr_kanpan.data_types import RefreshHook
from imbue.mngr_kanpan.fetcher import _build_hook_env
from imbue.mngr_kanpan.fetcher import _build_pr_branch_index
from imbue.mngr_kanpan.fetcher import _pr_priority
from imbue.mngr_kanpan.fetcher import enrich_snapshot_with_github_data
from imbue.mngr_kanpan.fetcher import fetch_agent_snapshot
from imbue.mngr_kanpan.fetcher import fetch_board_snapshot
from imbue.mngr_kanpan.fetcher import fetch_github_data
from imbue.mngr_kanpan.fetcher import run_refresh_hooks
from imbue.mngr_kanpan.github import FetchPrsResult
from imbue.mngr_kanpan.testing import make_agent_details
from imbue.mngr_kanpan.testing import make_pr_info

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
    pr = make_pr_info(number=1, head_branch="mngr/agent")
    result = _build_pr_branch_index((pr,))
    assert result == {"mngr/agent": pr}


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


# === fetch_github_data ===


def test_fetch_github_data_skips_agents_without_remote_label(tmp_path: Path) -> None:
    """Agents without a 'remote' label are skipped; agents with one get PRs."""
    no_label_dir = tmp_path / "no-label"
    no_label_dir.mkdir()
    with_label_dir = tmp_path / "with-label"
    with_label_dir.mkdir()

    agent_no_label = make_agent_details(name="no-label", work_dir=no_label_dir, provider_name="local")
    agent_with_label = make_agent_details(
        name="with-label",
        work_dir=with_label_dir,
        provider_name="local",
        labels={"remote": "git@github.com:org/repo.git"},
    )

    pr = make_pr_info(number=1, head_branch="mngr/feature")
    pr_result = FetchPrsResult(prs=(pr,), error=None)

    mngr_ctx = MagicMock()

    with patch("imbue.mngr_kanpan.fetcher.fetch_all_prs", return_value=pr_result):
        result = fetch_github_data(mngr_ctx, [agent_no_label, agent_with_label])

    assert result.pr_by_repo_branch["org/repo"]["mngr/feature"] == pr


def test_fetch_github_data_fetches_per_repo(tmp_path: Path) -> None:
    """Agents in different repos trigger separate PR fetches."""
    dir_a = tmp_path / "repo-a"
    dir_a.mkdir()
    dir_b = tmp_path / "repo-b"
    dir_b.mkdir()

    agent_a = make_agent_details(
        name="agent-a",
        work_dir=dir_a,
        provider_name="local",
        labels={"remote": "git@github.com:org/repo-a.git"},
    )
    agent_b = make_agent_details(
        name="agent-b",
        work_dir=dir_b,
        provider_name="local",
        labels={"remote": "git@github.com:org/repo-b.git"},
    )

    pr_a = make_pr_info(number=1, head_branch="mngr/feature-a")
    pr_b = make_pr_info(number=2, head_branch="mngr/feature-b")

    call_count = 0

    def mock_fetch_prs(cg: object, cwd: Path | None = None, repo: str | None = None) -> FetchPrsResult:
        nonlocal call_count
        call_count += 1
        if repo == "org/repo-a":
            return FetchPrsResult(prs=(pr_a,), error=None)
        if repo == "org/repo-b":
            return FetchPrsResult(prs=(pr_b,), error=None)
        return FetchPrsResult(prs=(), error=f"unexpected repo: {repo}")

    mngr_ctx = MagicMock()

    with patch("imbue.mngr_kanpan.fetcher.fetch_all_prs", side_effect=mock_fetch_prs):
        result = fetch_github_data(mngr_ctx, [agent_a, agent_b])

    assert call_count == 2
    assert len(result.repo_pr_loaded) == 2
    assert result.pr_by_repo_branch["org/repo-a"]["mngr/feature-a"] == pr_a
    assert result.pr_by_repo_branch["org/repo-b"]["mngr/feature-b"] == pr_b


def test_fetch_github_data_deduplicates_repos(tmp_path: Path) -> None:
    """Multiple agents in the same repo trigger only one PR fetch."""
    wt1 = tmp_path / "wt1"
    wt1.mkdir()
    wt2 = tmp_path / "wt2"
    wt2.mkdir()

    remote_url = "git@github.com:org/repo.git"
    agent1 = make_agent_details(name="a1", work_dir=wt1, provider_name="local", labels={"remote": remote_url})
    agent2 = make_agent_details(name="a2", work_dir=wt2, provider_name="local", labels={"remote": remote_url})

    call_count = 0

    def mock_fetch_prs(cg: object, cwd: Path | None = None, repo: str | None = None) -> FetchPrsResult:
        nonlocal call_count
        call_count += 1
        return FetchPrsResult(prs=(), error=None)

    mngr_ctx = MagicMock()

    with patch("imbue.mngr_kanpan.fetcher.fetch_all_prs", side_effect=mock_fetch_prs):
        result = fetch_github_data(mngr_ctx, [agent1, agent2])

    assert call_count == 1
    assert result.repo_pr_loaded["org/repo"] is True


def test_fetch_github_data_partial_failure(tmp_path: Path) -> None:
    """If one repo fails to fetch PRs, others still succeed."""
    good_dir = tmp_path / "good"
    good_dir.mkdir()
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()

    agent_good = make_agent_details(
        name="good",
        work_dir=good_dir,
        provider_name="local",
        labels={"remote": "git@github.com:org/good.git"},
    )
    agent_bad = make_agent_details(
        name="bad",
        work_dir=bad_dir,
        provider_name="local",
        labels={"remote": "git@github.com:org/bad.git"},
    )

    pr = make_pr_info(number=1, head_branch="mngr/feature")

    def mock_fetch_prs(cg: object, cwd: Path | None = None, repo: str | None = None) -> FetchPrsResult:
        if repo == "org/good":
            return FetchPrsResult(prs=(pr,), error=None)
        return FetchPrsResult(prs=(), error="gh pr list failed: auth required")

    mngr_ctx = MagicMock()

    with patch("imbue.mngr_kanpan.fetcher.fetch_all_prs", side_effect=mock_fetch_prs):
        result = fetch_github_data(mngr_ctx, [agent_good, agent_bad])

    assert result.repo_pr_loaded["org/good"] is True
    assert result.repo_pr_loaded["org/bad"] is False
    assert result.pr_by_repo_branch["org/good"]["mngr/feature"] == pr
    assert len(result.errors) == 1
    assert "auth required" in result.errors[0]


def test_fetch_github_data_no_local_agents() -> None:
    """Remote-only agents use gh --repo to fetch PRs without a local cwd."""
    agent = make_agent_details(
        name="remote",
        work_dir=Path("/remote"),
        provider_name="modal",
        labels={"remote": "git@github.com:org/repo.git"},
    )
    pr = make_pr_info(number=1, head_branch="mngr/feature")
    pr_result = FetchPrsResult(prs=(pr,), error=None)

    mngr_ctx = MagicMock()

    def mock_fetch_prs(cg: object, cwd: Path | None = None, repo: str | None = None) -> FetchPrsResult:
        assert cwd is None, "Should not use cwd for remote-only agents"
        assert repo == "org/repo", "Should use --repo for remote-only agents"
        return pr_result

    with patch("imbue.mngr_kanpan.fetcher.fetch_all_prs", side_effect=mock_fetch_prs):
        result = fetch_github_data(mngr_ctx, [agent])
    assert result.repo_pr_loaded["org/repo"] is True
    assert result.pr_by_repo_branch["org/repo"]["mngr/feature"] == pr


# === enrich_snapshot_with_github_data ===


def test_enrich_uses_per_agent_repo_for_create_pr_url() -> None:
    """create_pr_url uses the agent's own repo from the 'remote' label."""
    entry = AgentBoardEntry(
        name=AgentName("agent-1"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("local"),
        branch="mngr/feature",
        column_data=ColumnData(labels={"remote": "git@github.com:org/my-repo.git"}),
    )
    snapshot = BoardSnapshot(entries=(entry,), repo_pr_loaded={}, fetch_time_seconds=1.0)
    remote = GitHubData(
        repo_pr_loaded={"org/my-repo": True},
    )
    result = enrich_snapshot_with_github_data(snapshot, remote)
    assert result.entries[0].create_pr_url == "https://github.com/org/my-repo/compare/mngr/feature?expand=1"


def test_enrich_suppresses_create_pr_url_when_repo_pr_fetch_failed() -> None:
    """create_pr_url is suppressed for agents whose repo failed to load PRs."""
    entry = AgentBoardEntry(
        name=AgentName("agent-1"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("local"),
        branch="mngr/feature",
        column_data=ColumnData(labels={"remote": "git@github.com:org/my-repo.git"}),
    )
    snapshot = BoardSnapshot(entries=(entry,), repo_pr_loaded={}, fetch_time_seconds=1.0)
    remote = GitHubData(
        repo_pr_loaded={},
    )
    result = enrich_snapshot_with_github_data(snapshot, remote)
    assert result.entries[0].create_pr_url is None


# === fetch_board_snapshot ===


def test_fetch_board_snapshot_integrates_agents_and_prs() -> None:
    agent1 = make_agent_details(
        name="agent-1",
        state=AgentLifecycleState.RUNNING,
        provider_name="modal",
        initial_branch="mngr/agent-1",
        labels={"remote": "git@github.com:org/repo.git"},
    )
    agent2 = make_agent_details(name="agent-2", state=AgentLifecycleState.DONE, provider_name="modal")

    pr1 = make_pr_info(number=42, head_branch="mngr/agent-1", state=PrState.OPEN)

    mock_list_result = MagicMock()
    mock_list_result.agents = [agent1, agent2]
    mock_list_result.errors = []

    mngr_ctx = MagicMock()
    mngr_ctx.concurrency_group = MagicMock()

    # Modal agent has 'remote' label, so _get_agent_repo_path resolves its repo
    # and _lookup_pr matches it against the PR data.
    remote = GitHubData(
        pr_by_repo_branch={"org/repo": {"mngr/agent-1": pr1}},
        repo_pr_loaded={"org/repo": True},
    )

    with (
        patch("imbue.mngr_kanpan.fetcher.list_agents", return_value=mock_list_result),
        patch("imbue.mngr_kanpan.fetcher.fetch_github_data", return_value=remote),
    ):
        snapshot = fetch_board_snapshot(mngr_ctx, (), (), None, None, None)

    assert len(snapshot.entries) == 2
    assert snapshot.entries[0].name == AgentName("agent-1")
    assert snapshot.entries[0].pr is not None
    assert snapshot.entries[0].pr.number == 42
    assert snapshot.entries[1].name == AgentName("agent-2")
    assert snapshot.entries[1].pr is None
    assert snapshot.errors == ()
    assert snapshot.repo_pr_loaded["org/repo"] is True
    assert snapshot.fetch_time_seconds > 0


def test_fetch_board_snapshot_with_list_errors() -> None:
    mock_error = MagicMock()
    mock_error.exception_type = "ConnectionError"
    mock_error.message = "host unreachable"

    mock_list_result = MagicMock()
    mock_list_result.agents = []
    mock_list_result.errors = [mock_error]

    remote = GitHubData(repo_pr_loaded={})

    mngr_ctx = MagicMock()
    mngr_ctx.concurrency_group = MagicMock()

    with (
        patch("imbue.mngr_kanpan.fetcher.list_agents", return_value=mock_list_result),
        patch("imbue.mngr_kanpan.fetcher.fetch_github_data", return_value=remote),
    ):
        snapshot = fetch_board_snapshot(mngr_ctx, (), (), None, None, None)

    assert len(snapshot.entries) == 0
    assert len(snapshot.errors) == 1
    assert "ConnectionError" in snapshot.errors[0]


def test_fetch_agent_snapshot_entries_have_no_pr() -> None:
    """fetch_agent_snapshot should return entries with pr=None and create_pr_url=None."""
    agent1 = make_agent_details(name="agent-1", state=AgentLifecycleState.RUNNING, provider_name="modal")

    mock_list_result = MagicMock()
    mock_list_result.agents = [agent1]
    mock_list_result.errors = []

    mngr_ctx = MagicMock()
    mngr_ctx.concurrency_group = MagicMock()

    with patch("imbue.mngr_kanpan.fetcher.list_agents", return_value=mock_list_result):
        snapshot = fetch_agent_snapshot(mngr_ctx)

    assert len(snapshot.entries) == 1
    assert snapshot.entries[0].name == AgentName("agent-1")
    assert snapshot.entries[0].pr is None
    assert snapshot.entries[0].create_pr_url is None
    assert snapshot.repo_pr_loaded == {}
    assert snapshot.errors == ()
    assert snapshot.fetch_time_seconds > 0


def test_fetch_board_snapshot_passes_filters_to_list_agents() -> None:
    """Filters should be forwarded to list_agents."""
    mock_list_result = MagicMock()
    mock_list_result.agents = []
    mock_list_result.errors = []

    remote = GitHubData(repo_pr_loaded={})

    mngr_ctx = MagicMock()
    mngr_ctx.concurrency_group = MagicMock()

    with (
        patch("imbue.mngr_kanpan.fetcher.list_agents", return_value=mock_list_result) as mock_list,
        patch("imbue.mngr_kanpan.fetcher.fetch_github_data", return_value=remote),
    ):
        fetch_board_snapshot(
            mngr_ctx,
            ('state == "RUNNING"',),
            ('state == "DONE"',),
            None,
            None,
            None,
        )

    mock_list.assert_called_once()
    call_kwargs = mock_list.call_args
    assert call_kwargs.kwargs["include_filters"] == ('state == "RUNNING"',)
    assert call_kwargs.kwargs["exclude_filters"] == ('state == "DONE"',)


def test_fetch_agent_snapshot_passes_filters_to_list_agents() -> None:
    """Filters should be forwarded to list_agents."""
    mock_list_result = MagicMock()
    mock_list_result.agents = []
    mock_list_result.errors = []

    mngr_ctx = MagicMock()
    mngr_ctx.concurrency_group = MagicMock()

    with patch("imbue.mngr_kanpan.fetcher.list_agents", return_value=mock_list_result) as mock_list:
        fetch_agent_snapshot(
            mngr_ctx,
            include_filters=('labels.project == "mngr"',),
            exclude_filters=(),
        )

    mock_list.assert_called_once()
    call_kwargs = mock_list.call_args
    assert call_kwargs.kwargs["include_filters"] == ('labels.project == "mngr"',)
    assert call_kwargs.kwargs["exclude_filters"] == ()


def test_fetch_board_snapshot_passes_labels_and_plugin_data() -> None:
    """Labels and plugin data from AgentDetails should be passed to AgentBoardEntry."""
    agent = make_agent_details(
        name="agent-1",
        provider_name="modal",
        labels={"blocked": "yes"},
        plugin={"claude": {"waiting_reason": "PERMISSIONS"}},
    )

    mock_list_result = MagicMock()
    mock_list_result.agents = [agent]
    mock_list_result.errors = []

    remote = GitHubData(repo_pr_loaded={})
    mngr_ctx = MagicMock()
    mngr_ctx.concurrency_group = MagicMock()

    with (
        patch("imbue.mngr_kanpan.fetcher.list_agents", return_value=mock_list_result),
        patch("imbue.mngr_kanpan.fetcher.fetch_github_data", return_value=remote),
    ):
        snapshot = fetch_board_snapshot(mngr_ctx, (), (), None, None, None)

    assert snapshot.entries[0].column_data.labels == {"blocked": "yes"}
    assert snapshot.entries[0].column_data.plugin_data == {"claude": {"waiting_reason": "PERMISSIONS"}}


def test_fetch_agent_snapshot_passes_labels_and_plugin_data() -> None:
    """Labels and plugin data should also be passed in agent-only snapshots."""
    agent = make_agent_details(
        name="agent-1",
        provider_name="modal",
        labels={"project": "mngr"},
        plugin={"kanpan": {"muted": True}},
    )

    mock_list_result = MagicMock()
    mock_list_result.agents = [agent]
    mock_list_result.errors = []

    mngr_ctx = MagicMock()
    mngr_ctx.concurrency_group = MagicMock()

    with patch("imbue.mngr_kanpan.fetcher.list_agents", return_value=mock_list_result):
        snapshot = fetch_agent_snapshot(mngr_ctx)

    assert snapshot.entries[0].column_data.labels == {"project": "mngr"}
    assert snapshot.entries[0].column_data.plugin_data == {"kanpan": {"muted": True}}


def test_fetch_board_snapshot_surfaces_gh_errors_and_suppresses_create_pr_url(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    init_git_repo_with_config(repo_dir)

    agent = make_agent_details(
        name="agent-1",
        work_dir=repo_dir,
        provider_name="local",
        initial_branch="mngr/agent-1",
        labels={"remote": "git@github.com:org/repo.git"},
    )

    # Simulate fetch_github_data returning an error for this repo.
    remote = GitHubData(
        repo_pr_loaded={"org/repo": False},
        errors=("gh pr list failed: auth required",),
    )

    mock_list_result = MagicMock()
    mock_list_result.agents = [agent]
    mock_list_result.errors = []

    mngr_ctx = MagicMock()
    mngr_ctx.concurrency_group = MagicMock()

    with (
        patch("imbue.mngr_kanpan.fetcher.list_agents", return_value=mock_list_result),
        patch("imbue.mngr_kanpan.fetcher.fetch_github_data", return_value=remote),
    ):
        snapshot = fetch_board_snapshot(mngr_ctx, (), (), None, None, None)

    assert len(snapshot.errors) == 1
    assert "gh pr list failed" in snapshot.errors[0]
    assert snapshot.repo_pr_loaded == {"org/repo": False}
    assert snapshot.entries[0].branch == "mngr/agent-1"
    # When PRs failed to load, create_pr_url should be suppressed even though
    # the agent has a branch and a valid GitHub remote
    assert snapshot.entries[0].create_pr_url is None


# === _build_hook_env ===


def _make_entry(
    name: str = "test-agent",
    branch: str | None = "mngr/test",
    state: AgentLifecycleState = AgentLifecycleState.RUNNING,
) -> AgentBoardEntry:
    return AgentBoardEntry(
        name=AgentName(name),
        state=state,
        provider_name=ProviderInstanceName("local"),
        branch=branch,
    )


def test_build_hook_env_basic_fields() -> None:
    entry = _make_entry(name="my-agent", branch="mngr/feature")
    env = _build_hook_env(entry)
    assert env["MNGR_AGENT_NAME"] == "my-agent"
    assert env["MNGR_AGENT_BRANCH"] == "mngr/feature"
    assert env["MNGR_AGENT_STATE"] == "RUNNING"
    assert env["MNGR_AGENT_PROVIDER"] == "local"


def test_build_hook_env_no_pr() -> None:
    entry = _make_entry()
    env = _build_hook_env(entry)
    assert env["MNGR_AGENT_PR_NUMBER"] == ""
    assert env["MNGR_AGENT_PR_URL"] == ""
    assert env["MNGR_AGENT_PR_STATE"] == ""


def test_build_hook_env_with_pr() -> None:
    pr = make_pr_info(number=42, head_branch="mngr/test", state=PrState.OPEN)
    entry = AgentBoardEntry(
        name=AgentName("agent-with-pr"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("local"),
        branch="mngr/test",
        pr=pr,
    )
    env = _build_hook_env(entry)
    assert env["MNGR_AGENT_PR_NUMBER"] == "42"
    assert env["MNGR_AGENT_PR_URL"] == pr.url
    assert env["MNGR_AGENT_PR_STATE"] == "OPEN"


def test_build_hook_env_no_branch() -> None:
    entry = _make_entry(branch=None)
    env = _build_hook_env(entry)
    assert env["MNGR_AGENT_BRANCH"] == ""


# === run_refresh_hooks ===


def test_run_refresh_hooks_successful_command() -> None:
    hook = RefreshHook(name="Echo test", command="echo hello")
    entries = (_make_entry(name="agent-1"), _make_entry(name="agent-2"))
    with ConcurrencyGroup(name="test") as cg:
        errors = run_refresh_hooks(cg, [hook], entries)
    assert errors == []


def test_run_refresh_hooks_failing_command() -> None:
    hook = RefreshHook(name="Fail hook", command="exit 1")
    entries = (_make_entry(name="agent-1"),)
    with ConcurrencyGroup(name="test") as cg:
        errors = run_refresh_hooks(cg, [hook], entries)
    assert len(errors) == 1
    assert "Fail hook" in errors[0]
    assert "agent-1" in errors[0]
    assert "exit 1" in errors[0]


def test_run_refresh_hooks_passes_env_vars() -> None:
    """Verify hook commands receive MNGR_AGENT_NAME env var."""
    hook = RefreshHook(name="Env check", command='test "$MNGR_AGENT_NAME" = "my-agent"')
    entries = (_make_entry(name="my-agent"),)
    with ConcurrencyGroup(name="test") as cg:
        errors = run_refresh_hooks(cg, [hook], entries)
    assert errors == []


def test_run_refresh_hooks_empty_hooks() -> None:
    entries = (_make_entry(),)
    with ConcurrencyGroup(name="test") as cg:
        errors = run_refresh_hooks(cg, [], entries)
    assert errors == []


def test_run_refresh_hooks_empty_entries() -> None:
    hook = RefreshHook(name="Test", command="echo hello")
    with ConcurrencyGroup(name="test") as cg:
        errors = run_refresh_hooks(cg, [hook], ())
    assert errors == []


def test_run_refresh_hooks_multiple_hooks() -> None:
    hook1 = RefreshHook(name="Pass hook", command="true")
    hook2 = RefreshHook(name="Fail hook", command="exit 2")
    entries = (_make_entry(name="agent-1"),)
    with ConcurrencyGroup(name="test") as cg:
        errors = run_refresh_hooks(cg, [hook1, hook2], entries)
    assert len(errors) == 1
    assert "Fail hook" in errors[0]


def test_run_refresh_hooks_execute_in_order(tmp_path: Path) -> None:
    """Hooks execute sequentially in list order, with per-agent commands parallel within each hook."""
    log_file = tmp_path / "hook-order.log"
    hook1 = RefreshHook(name="First", command=f'echo "1-$MNGR_AGENT_NAME" >> {log_file}')
    hook2 = RefreshHook(name="Second", command=f'echo "2-$MNGR_AGENT_NAME" >> {log_file}')
    hook3 = RefreshHook(name="Third", command=f'echo "3-$MNGR_AGENT_NAME" >> {log_file}')
    entries = (_make_entry(name="alice"), _make_entry(name="bob"))
    with ConcurrencyGroup(name="test") as cg:
        errors = run_refresh_hooks(cg, [hook1, hook2, hook3], entries)
    assert errors == []
    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 6
    # All hook-1 lines come before all hook-2 lines, which come before all hook-3 lines.
    # Within a hook, agent order is non-deterministic (parallel), so just check the hook prefixes.
    prefixes = [line.split("-")[0] for line in lines]
    assert prefixes[:2] == ["1", "1"]
    assert prefixes[2:4] == ["2", "2"]
    assert prefixes[4:] == ["3", "3"]


# === fetch_board_snapshot with hooks ===


def test_fetch_board_snapshot_no_hooks() -> None:
    """With None hooks, no hook execution occurs."""
    agent = make_agent_details(name="agent-1", state=AgentLifecycleState.RUNNING, provider_name="modal")
    remote = GitHubData(repo_pr_loaded={})
    mock_list_result = MagicMock()
    mock_list_result.agents = [agent]
    mock_list_result.errors = []
    mngr_ctx = MagicMock()
    mngr_ctx.concurrency_group = MagicMock()

    with (
        patch("imbue.mngr_kanpan.fetcher.list_agents", return_value=mock_list_result),
        patch("imbue.mngr_kanpan.fetcher.fetch_github_data", return_value=remote),
    ):
        snapshot = fetch_board_snapshot(mngr_ctx, (), (), None, None, None)

    assert len(snapshot.entries) == 1
    assert snapshot.errors == ()


def test_fetch_board_snapshot_after_hooks_run() -> None:
    """After-hooks run against the new snapshot entries."""
    agent = make_agent_details(name="agent-1", state=AgentLifecycleState.RUNNING, provider_name="modal")
    remote = GitHubData(repo_pr_loaded={})
    mock_list_result = MagicMock()
    mock_list_result.agents = [agent]
    mock_list_result.errors = []
    mngr_ctx = MagicMock()

    after_hook = RefreshHook(name="After hook", command="exit 1")

    with (
        patch("imbue.mngr_kanpan.fetcher.list_agents", return_value=mock_list_result),
        patch("imbue.mngr_kanpan.fetcher.fetch_github_data", return_value=remote),
        patch(
            "imbue.mngr_kanpan.fetcher.run_refresh_hooks",
            return_value=["Hook 'After hook' failed for agent-1 (exit 1)"],
        ) as mock_hooks,
    ):
        snapshot = fetch_board_snapshot(mngr_ctx, (), (), None, [after_hook], None)

    assert len(snapshot.errors) == 1
    assert "After hook" in snapshot.errors[0]
    mock_hooks.assert_called_once()


def test_fetch_board_snapshot_before_hooks_skipped_on_first_refresh() -> None:
    """Before-hooks are skipped when there is no previous snapshot."""
    agent = make_agent_details(name="agent-1", state=AgentLifecycleState.RUNNING, provider_name="modal")
    remote = GitHubData(repo_pr_loaded={})
    mock_list_result = MagicMock()
    mock_list_result.agents = [agent]
    mock_list_result.errors = []
    mngr_ctx = MagicMock()

    before_hook = RefreshHook(name="Before hook", command="exit 1")

    with (
        patch("imbue.mngr_kanpan.fetcher.list_agents", return_value=mock_list_result),
        patch("imbue.mngr_kanpan.fetcher.fetch_github_data", return_value=remote),
        patch("imbue.mngr_kanpan.fetcher.run_refresh_hooks", return_value=[]) as mock_hooks,
    ):
        snapshot = fetch_board_snapshot(mngr_ctx, (), (), [before_hook], None, None)

    mock_hooks.assert_not_called()
    assert snapshot.errors == ()


def test_fetch_board_snapshot_before_hooks_run_with_prev_snapshot() -> None:
    """Before-hooks run against previous snapshot entries when available."""
    agent = make_agent_details(name="agent-1", state=AgentLifecycleState.RUNNING, provider_name="modal")
    remote = GitHubData(repo_pr_loaded={})
    mock_list_result = MagicMock()
    mock_list_result.agents = [agent]
    mock_list_result.errors = []
    mngr_ctx = MagicMock()

    prev_snapshot = BoardSnapshot(
        entries=(_make_entry(name="agent-1"),),
        repo_pr_loaded={},
        fetch_time_seconds=1.0,
    )
    before_hook = RefreshHook(name="Before hook", command="true")

    with (
        patch("imbue.mngr_kanpan.fetcher.list_agents", return_value=mock_list_result),
        patch("imbue.mngr_kanpan.fetcher.fetch_github_data", return_value=remote),
        patch("imbue.mngr_kanpan.fetcher.run_refresh_hooks", return_value=[]) as mock_hooks,
    ):
        fetch_board_snapshot(mngr_ctx, (), (), [before_hook], None, prev_snapshot)

    mock_hooks.assert_called_once()
    call_args = mock_hooks.call_args
    assert call_args[0][1] == [before_hook]
    assert call_args[0][2] == prev_snapshot.entries
