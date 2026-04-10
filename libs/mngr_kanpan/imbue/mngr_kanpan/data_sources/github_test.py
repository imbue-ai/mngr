import json

from imbue.mngr.primitives import AgentName
from imbue.mngr_kanpan.data_source import CiStatus
from imbue.mngr_kanpan.data_source import FieldValue
from imbue.mngr_kanpan.data_source import PrState
from imbue.mngr_kanpan.data_source import RepoPathField
from imbue.mngr_kanpan.data_sources.github import GitHubDataSource
from imbue.mngr_kanpan.data_sources.github import GitHubDataSourceConfig
from imbue.mngr_kanpan.data_sources.github import _PrFieldInternal
from imbue.mngr_kanpan.data_sources.github import _build_create_pr_url
from imbue.mngr_kanpan.data_sources.github import _build_pr_branch_index
from imbue.mngr_kanpan.data_sources.github import _build_unresolved_query
from imbue.mngr_kanpan.data_sources.github import _get_cached_repo_path
from imbue.mngr_kanpan.data_sources.github import _lookup_pr
from imbue.mngr_kanpan.data_sources.github import _parse_conflicts
from imbue.mngr_kanpan.data_sources.github import _parse_unresolved
from imbue.mngr_kanpan.data_sources.github import _pr_priority


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
