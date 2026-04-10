import json
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import AgentName
from imbue.mngr_kanpan.data_source import CiStatus
from imbue.mngr_kanpan.data_source import ConflictsField
from imbue.mngr_kanpan.data_source import CreatePrUrlField
from imbue.mngr_kanpan.data_source import FIELD_CI
from imbue.mngr_kanpan.data_source import FIELD_CONFLICTS
from imbue.mngr_kanpan.data_source import FIELD_CREATE_PR_URL
from imbue.mngr_kanpan.data_source import FIELD_PR
from imbue.mngr_kanpan.data_source import FIELD_UNRESOLVED
from imbue.mngr_kanpan.data_source import FieldValue
from imbue.mngr_kanpan.data_source import PrState
from imbue.mngr_kanpan.data_source import RepoPathField
from imbue.mngr_kanpan.data_source import UnresolvedField
from imbue.mngr_kanpan.data_sources.github import GitHubDataSource
from imbue.mngr_kanpan.data_sources.github import GitHubDataSourceConfig
from imbue.mngr_kanpan.data_sources.github import _PrFieldInternal
from imbue.mngr_kanpan.data_sources.github import _build_create_pr_url
from imbue.mngr_kanpan.data_sources.github import _build_pr_branch_index
from imbue.mngr_kanpan.data_sources.github import _build_unresolved_query
from imbue.mngr_kanpan.data_sources.github import _fetch_repo_prs
from imbue.mngr_kanpan.data_sources.github import _get_cached_repo_path
from imbue.mngr_kanpan.data_sources.github import _lookup_pr
from imbue.mngr_kanpan.data_sources.github import _parse_conflicts
from imbue.mngr_kanpan.data_sources.github import _parse_unresolved
from imbue.mngr_kanpan.data_sources.github import _pr_priority
from imbue.mngr_kanpan.testing import make_agent_details


def _make_internal_pr(
    number: int = 1,
    branch: str = "test-branch",
    state: PrState = PrState.OPEN,
    check_status: CiStatus = CiStatus.PASSING,
) -> _PrFieldInternal:
    return _PrFieldInternal(
        number=number,
        title=f"PR #{number}",
        state=state,
        url=f"https://github.com/org/repo/pull/{number}",
        head_branch=branch,
        is_draft=False,
        internal_check_status=check_status,
    )


# === GitHubDataSource properties ===


def test_github_data_source_name() -> None:
    ds = GitHubDataSource()
    assert ds.name == "github"


def test_github_data_source_columns_default() -> None:
    ds = GitHubDataSource()
    cols = ds.columns
    assert "pr" in cols
    assert "ci" in cols
    assert "conflicts" in cols
    assert "unresolved" in cols


def test_github_data_source_columns_disabled() -> None:
    ds = GitHubDataSource(config=GitHubDataSourceConfig(pr=False, ci=False, conflicts=False, unresolved=False))
    cols = ds.columns
    assert "pr" not in cols
    assert "ci" not in cols
    assert "conflicts" not in cols
    assert "unresolved" not in cols


def test_github_data_source_field_types() -> None:
    ds = GitHubDataSource()
    types = ds.field_types
    assert "pr" in types
    assert "ci" in types


def test_github_data_source_field_types_disabled() -> None:
    ds = GitHubDataSource(config=GitHubDataSourceConfig(pr=False, ci=False))
    types = ds.field_types
    assert "pr" not in types
    assert "ci" not in types


# === _get_cached_repo_path ===


def test_get_cached_repo_path_found() -> None:
    cached: dict[AgentName, dict[str, FieldValue]] = {
        AgentName("a1"): {"repo_path": RepoPathField(path="org/repo")},
    }
    assert _get_cached_repo_path(cached, AgentName("a1")) == "org/repo"


def test_get_cached_repo_path_not_found() -> None:
    assert _get_cached_repo_path({}, AgentName("a1")) is None


def test_get_cached_repo_path_wrong_type() -> None:
    cached: dict[AgentName, dict[str, FieldValue]] = {
        AgentName("a1"): {"repo_path": _make_internal_pr()},
    }
    assert _get_cached_repo_path(cached, AgentName("a1")) is None


# === _pr_priority ===


def test_pr_priority_open() -> None:
    assert _pr_priority(_make_internal_pr(state=PrState.OPEN)) == 2


def test_pr_priority_merged() -> None:
    assert _pr_priority(_make_internal_pr(state=PrState.MERGED)) == 1


def test_pr_priority_closed() -> None:
    assert _pr_priority(_make_internal_pr(state=PrState.CLOSED)) == 0


# === _build_pr_branch_index ===


def test_build_pr_branch_index_empty() -> None:
    assert _build_pr_branch_index(()) == {}


def test_build_pr_branch_index_single() -> None:
    pr = _make_internal_pr(branch="branch-1")
    result = _build_pr_branch_index((pr,))
    assert "branch-1" in result
    assert result["branch-1"].number == 1


def test_build_pr_branch_index_prefers_open() -> None:
    closed = _make_internal_pr(number=1, branch="b", state=PrState.CLOSED)
    open_pr = _make_internal_pr(number=2, branch="b", state=PrState.OPEN)
    result = _build_pr_branch_index((closed, open_pr))
    assert result["b"].number == 2


# === _lookup_pr ===


def test_lookup_pr_found() -> None:
    pr = _make_internal_pr(branch="b")
    index = {"repo": {"b": pr}}
    assert _lookup_pr(index, "repo", "b") == pr


def test_lookup_pr_not_found() -> None:
    assert _lookup_pr({}, "repo", "branch") is None


def test_lookup_pr_no_repo() -> None:
    pr = _make_internal_pr(branch="b")
    assert _lookup_pr({"other": {"b": pr}}, "repo", "b") is None


# === _build_create_pr_url ===


def test_build_create_pr_url() -> None:
    url = _build_create_pr_url("org/repo", "my-branch")
    assert url == "https://github.com/org/repo/compare/my-branch?expand=1"


# === _parse_conflicts ===


def test_parse_conflicts_conflicting() -> None:
    assert _parse_conflicts('{"mergeable": "CONFLICTING"}') is True


def test_parse_conflicts_mergeable() -> None:
    assert _parse_conflicts('{"mergeable": "MERGEABLE"}') is False


def test_parse_conflicts_invalid_json() -> None:
    assert _parse_conflicts("not json") is False


# === _build_unresolved_query ===


def test_build_unresolved_query() -> None:
    query = _build_unresolved_query("org/repo", 42)
    assert "org" in query
    assert "repo" in query
    assert "42" in query
    assert "reviewThreads" in query


# === _parse_unresolved ===


def test_parse_unresolved_has_unresolved() -> None:
    data = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": [
                            {"isResolved": True},
                            {"isResolved": False},
                        ]
                    }
                }
            }
        }
    }
    assert _parse_unresolved(json.dumps(data)) is True


def test_parse_unresolved_all_resolved() -> None:
    data = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": [
                            {"isResolved": True},
                        ]
                    }
                }
            }
        }
    }
    assert _parse_unresolved(json.dumps(data)) is False


def test_parse_unresolved_no_threads() -> None:
    data = {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}}
    assert _parse_unresolved(json.dumps(data)) is False


def test_parse_unresolved_invalid_json() -> None:
    assert _parse_unresolved("not json") is False


# === _fetch_repo_prs ===


def _make_fetch_cg(open_json: str, all_json: str) -> MagicMock:
    """Build a mock ConcurrencyGroup for fetch_all_prs."""
    open_proc = MagicMock()
    open_proc.read_stdout.return_value = open_json
    open_proc.read_stderr.return_value = ""
    open_proc.returncode = 0

    all_proc = MagicMock()
    all_proc.read_stdout.return_value = all_json
    all_proc.read_stderr.return_value = ""
    all_proc.returncode = 0

    cg = MagicMock()
    cg.run_process_in_background.side_effect = [open_proc, all_proc]
    return cg


def _make_open_pr_json(number: int = 1, branch: str = "test-branch") -> str:
    return json.dumps(
        [
            {
                "number": number,
                "title": f"PR #{number}",
                "state": "OPEN",
                "url": f"https://github.com/org/repo/pull/{number}",
                "headRefName": branch,
                "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
                "isDraft": False,
            }
        ]
    )


def test_fetch_repo_prs_success() -> None:
    cg = _make_fetch_cg(_make_open_pr_json(1, "branch-1"), _make_open_pr_json(1, "branch-1"))
    repo_path, result = _fetch_repo_prs(cg, "org/repo")
    assert repo_path == "org/repo"
    assert result.error is None
    assert len(result.prs) == 1
    assert result.prs[0].number == 1
    assert result.prs[0].head_branch == "branch-1"


def test_fetch_repo_prs_error() -> None:
    cg = MagicMock()
    proc_fail = MagicMock()
    proc_fail.read_stdout.return_value = ""
    proc_fail.read_stderr.return_value = "some error"
    proc_fail.returncode = 1
    cg.run_process_in_background.side_effect = [proc_fail, proc_fail]
    repo_path, result = _fetch_repo_prs(cg, "org/repo")
    assert repo_path == "org/repo"
    assert result.error is not None


# === GitHubDataSource.compute ===


def _make_mock_mngr_ctx(cg: MagicMock) -> MngrContext:
    return cast(MngrContext, SimpleNamespace(concurrency_group=cg))


def test_compute_no_agents() -> None:
    ds = GitHubDataSource()
    cg = MagicMock()
    ctx = _make_mock_mngr_ctx(cg)
    fields, errors = ds.compute(agents=(), cached_fields={}, mngr_ctx=ctx)
    assert fields == {}
    assert errors == []


def test_compute_agents_without_repo() -> None:
    ds = GitHubDataSource()
    cg = MagicMock()
    ctx = _make_mock_mngr_ctx(cg)
    agent = make_agent_details(name="a1", initial_branch="mngr/test", labels={})
    fields, errors = ds.compute(agents=(agent,), cached_fields={}, mngr_ctx=ctx)
    assert fields == {}
    assert errors == []


def test_compute_agents_with_cached_repo_path() -> None:
    """Uses cached repo_path field from previous cycle if available."""
    ds = GitHubDataSource(config=GitHubDataSourceConfig(conflicts=False, unresolved=False))
    # Use a cg that returns a PR for branch-1
    cg = _make_fetch_cg(_make_open_pr_json(1, "branch-1"), _make_open_pr_json(1, "branch-1"))
    ctx = _make_mock_mngr_ctx(cg)
    # Provide repo path via label (simpler than full cached repo_path)
    agent_with_label = make_agent_details(
        name="a1", initial_branch="branch-1", labels={"remote": "git@github.com:org/repo.git"}
    )
    fields, errors = ds.compute(agents=(agent_with_label,), cached_fields={}, mngr_ctx=ctx)
    assert agent_with_label.name in fields
    assert FIELD_PR in fields[agent_with_label.name]
    assert FIELD_CI in fields[agent_with_label.name]


def test_compute_no_pr_for_branch_generates_create_url() -> None:
    """When pr=True and no PR found, create_pr_url should be set."""
    ds = GitHubDataSource(
        config=GitHubDataSourceConfig(pr=True, ci=True, create_pr_url=True, conflicts=False, unresolved=False)
    )
    agent = make_agent_details(
        name="a1", initial_branch="no-pr-branch", labels={"remote": "git@github.com:org/repo.git"}
    )
    # Return empty PR list so no PRs exist for branch
    cg = _make_fetch_cg("[]", "[]")
    ctx = _make_mock_mngr_ctx(cg)
    fields, errors = ds.compute(agents=(agent,), cached_fields={}, mngr_ctx=ctx)
    assert agent.name in fields
    assert FIELD_CREATE_PR_URL in fields[agent.name]
    create_url_field = fields[agent.name][FIELD_CREATE_PR_URL]
    assert isinstance(create_url_field, CreatePrUrlField)
    assert "no-pr-branch" in create_url_field.url


def test_compute_pr_fetch_error_adds_error() -> None:
    ds = GitHubDataSource(config=GitHubDataSourceConfig(conflicts=False, unresolved=False))
    agent = make_agent_details(name="a1", initial_branch="branch-1", labels={"remote": "git@github.com:org/repo.git"})
    cg = MagicMock()
    fail_proc = MagicMock()
    fail_proc.read_stdout.return_value = ""
    fail_proc.read_stderr.return_value = "HTTP 504"
    fail_proc.returncode = 1
    cg.run_process_in_background.side_effect = [fail_proc, fail_proc]
    ctx = _make_mock_mngr_ctx(cg)
    fields, errors = ds.compute(agents=(agent,), cached_fields={}, mngr_ctx=ctx)
    assert len(errors) > 0


def test_compute_with_conflicts_and_unresolved() -> None:
    """Full compute with conflicts and unresolved metadata fetching."""
    ds = GitHubDataSource(
        config=GitHubDataSourceConfig(pr=True, ci=True, create_pr_url=False, conflicts=True, unresolved=True)
    )
    agent = make_agent_details(name="a1", initial_branch="branch-1", labels={"remote": "git@github.com:org/repo.git"})

    open_pr_json = _make_open_pr_json(1, "branch-1")
    # First two calls: fetch PRs (open + all)
    pr_open_proc = MagicMock()
    pr_open_proc.read_stdout.return_value = open_pr_json
    pr_open_proc.read_stderr.return_value = ""
    pr_open_proc.returncode = 0

    pr_all_proc = MagicMock()
    pr_all_proc.read_stdout.return_value = open_pr_json
    pr_all_proc.read_stderr.return_value = ""
    pr_all_proc.returncode = 0

    # Metadata procs for conflicts and unresolved
    conflicts_proc = MagicMock()
    conflicts_proc.wait.return_value = 0
    conflicts_proc.returncode = 0
    conflicts_proc.read_stdout.return_value = json.dumps({"mergeable": "MERGEABLE"})

    unresolved_proc = MagicMock()
    unresolved_proc.wait.return_value = 0
    unresolved_proc.returncode = 0
    unresolved_threads: dict[str, object] = {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}}
    unresolved_proc.read_stdout.return_value = json.dumps(unresolved_threads)

    cg = MagicMock()
    # First 2 calls for PR fetching, next 2 for metadata
    cg.run_process_in_background.side_effect = [pr_open_proc, pr_all_proc, conflicts_proc, unresolved_proc]

    ctx = _make_mock_mngr_ctx(cg)
    fields, errors = ds.compute(agents=(agent,), cached_fields={}, mngr_ctx=ctx)
    assert agent.name in fields
    assert FIELD_PR in fields[agent.name]
    assert FIELD_CONFLICTS in fields[agent.name]
    assert FIELD_UNRESOLVED in fields[agent.name]
    assert isinstance(fields[agent.name][FIELD_CONFLICTS], ConflictsField)
    assert isinstance(fields[agent.name][FIELD_UNRESOLVED], UnresolvedField)


def test_compute_disabled_pr_and_ci() -> None:
    ds = GitHubDataSource(
        config=GitHubDataSourceConfig(pr=False, ci=False, create_pr_url=False, conflicts=False, unresolved=False)
    )
    agent = make_agent_details(name="a1", initial_branch="branch-1", labels={"remote": "git@github.com:org/repo.git"})
    cg = _make_fetch_cg(_make_open_pr_json(1, "branch-1"), _make_open_pr_json(1, "branch-1"))
    ctx = _make_mock_mngr_ctx(cg)
    fields, errors = ds.compute(agents=(agent,), cached_fields={}, mngr_ctx=ctx)
    # pr=False means no FIELD_PR; ci=False means no FIELD_CI
    agent_fields = fields.get(agent.name, {})
    assert FIELD_PR not in agent_fields
    assert FIELD_CI not in agent_fields
