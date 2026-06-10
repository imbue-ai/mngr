import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from unittest.mock import MagicMock

from tenacity import Retrying
from tenacity import retry_if_exception_type
from tenacity import stop_after_attempt
from tenacity import wait_none

from imbue.concurrency_group.errors import ProcessError
from imbue.mngr.primitives import AgentName
from imbue.mngr_kanpan.data_source import FIELD_CI
from imbue.mngr_kanpan.data_source import FIELD_CONFLICTS
from imbue.mngr_kanpan.data_source import FIELD_PR
from imbue.mngr_kanpan.data_source import FIELD_UNRESOLVED
from imbue.mngr_kanpan.data_source import FieldValue
from imbue.mngr_kanpan.data_sources.github import CiField
from imbue.mngr_kanpan.data_sources.github import CiStatus
from imbue.mngr_kanpan.data_sources.github import ConflictsField
from imbue.mngr_kanpan.data_sources.github import CreatePrUrlField
from imbue.mngr_kanpan.data_sources.github import FetchBoardResult
from imbue.mngr_kanpan.data_sources.github import GitHubBoardFetchError
from imbue.mngr_kanpan.data_sources.github import GitHubDataSource
from imbue.mngr_kanpan.data_sources.github import GitHubDataSourceConfig
from imbue.mngr_kanpan.data_sources.github import PrFetchFailedField
from imbue.mngr_kanpan.data_sources.github import PrInfo
from imbue.mngr_kanpan.data_sources.github import PrState
from imbue.mngr_kanpan.data_sources.github import UnresolvedField
from imbue.mngr_kanpan.data_sources.github import _MAX_SEARCH_PAGES
from imbue.mngr_kanpan.data_sources.github import _PAGE_FETCH_ATTEMPTS
from imbue.mngr_kanpan.data_sources.github import _build_board_graphql
from imbue.mngr_kanpan.data_sources.github import _build_create_pr_url
from imbue.mngr_kanpan.data_sources.github import _build_prs_from_nodes
from imbue.mngr_kanpan.data_sources.github import _check_unresolved_threads
from imbue.mngr_kanpan.data_sources.github import _get_cached_repo_field
from imbue.mngr_kanpan.data_sources.github import _parse_board_page
from imbue.mngr_kanpan.data_sources.github import _parse_pr_node
from imbue.mngr_kanpan.data_sources.github import _parse_pr_state
from imbue.mngr_kanpan.data_sources.github import _summarize_failed_response
from imbue.mngr_kanpan.data_sources.github import fetch_board
from imbue.mngr_kanpan.data_sources.repo_paths import RepoPathField
from imbue.mngr_kanpan.testing import make_agent_details
from imbue.mngr_kanpan.testing import make_mngr_ctx_with_cg
from imbue.mngr_kanpan.testing import make_pr_field

# --- shared builders for the combined-query response shape ---


def _make_pr_node(
    *,
    number: int = 1,
    title: str = "test pr",
    state: str = "OPEN",
    url: str | None = None,
    head_branch: str = "test-branch",
    is_draft: bool = False,
    mergeable: str = "MERGEABLE",
    rollup_state: str | None = "SUCCESS",
    review_threads: list[dict[str, Any]] | None = None,
    pr_comments: list[dict[str, Any]] | None = None,
    repo: str = "org/repo",
) -> dict[str, Any]:
    """Build a PullRequest node matching the shape returned by the kanpan board query."""
    return {
        "number": number,
        "title": title,
        "state": state,
        "url": url or f"https://github.com/{repo}/pull/{number}",
        "headRefName": head_branch,
        "isDraft": is_draft,
        "mergeable": mergeable,
        "statusCheckRollup": None if rollup_state is None else {"state": rollup_state},
        "reviewThreads": {"nodes": review_threads or []},
        "comments": {"nodes": pr_comments or []},
        "repository": {"nameWithOwner": repo},
    }


def _make_board_response(
    *,
    nodes: list[dict[str, Any]] | None = None,
    has_next_page: bool = False,
    end_cursor: str | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> str:
    """Build a JSON-encoded `gh api graphql` response for the kanpan board query."""
    body: dict[str, Any] = {
        "data": {
            "s": {
                "nodes": nodes or [],
                "pageInfo": {"hasNextPage": has_next_page, "endCursor": end_cursor},
            }
        }
    }
    if errors is not None:
        body["errors"] = errors
    return json.dumps(body)


def _make_board_proc(stdout: str, stderr: str = "", returncode: int = 0) -> MagicMock:
    """Mock process carrying the given stdout/stderr."""
    proc = MagicMock()
    proc.read_stdout.return_value = stdout
    proc.read_stderr.return_value = stderr
    proc.returncode = returncode
    return proc


def _make_board_cg(response_json: str, returncode: int = 0) -> MagicMock:
    """Mock ConcurrencyGroup that returns a single proc carrying the given stdout."""
    cg = MagicMock()
    cg.run_process_in_background.return_value = _make_board_proc(response_json, returncode=returncode)
    return cg


def _make_paginated_board_cg(*procs: MagicMock) -> MagicMock:
    """Mock ConcurrencyGroup that returns the given procs across successive page requests."""
    cg = MagicMock()
    cg.run_process_in_background.side_effect = list(procs)
    return cg


def _no_wait_page_retrying() -> Retrying:
    """The production per-page retry policy but with the backoff removed, so retry
    behavior can be exercised in tests without real sleeps.
    """
    return Retrying(
        retry=retry_if_exception_type(GitHubBoardFetchError),
        stop=stop_after_attempt(_PAGE_FETCH_ATTEMPTS),
        wait=wait_none(),
        reraise=True,
    )


def _make_thread(*, resolved: bool = False, last_author: str | None = None) -> dict[str, Any]:
    """Build a reviewThreads node for `_check_unresolved_threads` tests."""
    node: dict[str, Any] = {"isResolved": resolved}
    if last_author is not None:
        node["comments"] = {"nodes": [{"author": {"login": last_author}}]}
    return node


# === GitHubDataSource properties ===


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


def test_github_data_source_field_types_disabled() -> None:
    ds = GitHubDataSource(config=GitHubDataSourceConfig(pr=False, ci=False))
    types = ds.field_types
    assert "pr" not in types
    assert "ci" not in types


# === _get_cached_repo_field ===


def test_get_cached_repo_field_found() -> None:
    repo_field = RepoPathField(path="org/repo", created=datetime(2028, 1, 1, 0, 0, 1, tzinfo=timezone.utc))
    cached: dict[AgentName, dict[str, FieldValue]] = {AgentName("a1"): {"repo_path": repo_field}}
    assert _get_cached_repo_field(cached, AgentName("a1")) == repo_field


def test_get_cached_repo_field_not_found() -> None:
    assert _get_cached_repo_field({}, AgentName("a1")) is None


def test_get_cached_repo_field_wrong_type() -> None:
    cached: dict[AgentName, dict[str, FieldValue]] = {
        AgentName("a1"): {"repo_path": make_pr_field(created=datetime(2028, 1, 1, 0, 0, 2, tzinfo=timezone.utc))},
    }
    assert _get_cached_repo_field(cached, AgentName("a1")) is None


# === _build_create_pr_url ===


def test_build_create_pr_url() -> None:
    url = _build_create_pr_url("org/repo", "my-branch")
    assert url == "https://github.com/org/repo/compare/my-branch?expand=1"


# === _parse_pr_state ===


def test_parse_pr_state_open() -> None:
    assert _parse_pr_state("OPEN") == PrState.OPEN


def test_parse_pr_state_closed() -> None:
    assert _parse_pr_state("CLOSED") == PrState.CLOSED


def test_parse_pr_state_merged() -> None:
    assert _parse_pr_state("MERGED") == PrState.MERGED


def test_parse_pr_state_lowercase() -> None:
    assert _parse_pr_state("open") == PrState.OPEN
    assert _parse_pr_state("closed") == PrState.CLOSED
    assert _parse_pr_state("merged") == PrState.MERGED


def test_parse_pr_state_unknown_defaults_to_open() -> None:
    assert _parse_pr_state("DRAFT") == PrState.OPEN


# === CiStatus.from_rollup_state ===


def test_from_rollup_state_none() -> None:
    assert CiStatus.from_rollup_state(None) == CiStatus.UNKNOWN


def test_from_rollup_state_success() -> None:
    assert CiStatus.from_rollup_state("SUCCESS") == CiStatus.SUCCESS


def test_from_rollup_state_failure() -> None:
    assert CiStatus.from_rollup_state("FAILURE") == CiStatus.FAILURE


def test_from_rollup_state_pending() -> None:
    assert CiStatus.from_rollup_state("PENDING") == CiStatus.PENDING


def test_from_rollup_state_unrecognized_is_unknown() -> None:
    # Future-proofing: any new enum value we don't know about should
    # render as UNKNOWN rather than crash.
    assert CiStatus.from_rollup_state("EXPECTED") == CiStatus.UNKNOWN


# === _parse_pr_node ===


def test_parse_pr_node_minimal() -> None:
    node = _make_pr_node(number=42, head_branch="mngr/foo")
    pr = _parse_pr_node(node, unresolved_ignore_user=None)
    assert pr.number == 42
    assert pr.head_branch == "mngr/foo"
    assert pr.state == PrState.OPEN
    assert pr.check_status == CiStatus.SUCCESS
    assert pr.is_draft is False
    assert pr.has_conflicts is False
    assert pr.has_unresolved is False


def test_parse_pr_node_conflicting() -> None:
    node = _make_pr_node(mergeable="CONFLICTING")
    pr = _parse_pr_node(node, unresolved_ignore_user=None)
    assert pr.has_conflicts is True


def test_parse_pr_node_no_rollup_is_unknown() -> None:
    node = _make_pr_node(rollup_state=None)
    pr = _parse_pr_node(node, unresolved_ignore_user=None)
    assert pr.check_status == CiStatus.UNKNOWN


def test_parse_pr_node_draft_merged() -> None:
    node = _make_pr_node(state="MERGED", is_draft=True, rollup_state=None)
    pr = _parse_pr_node(node, unresolved_ignore_user=None)
    assert pr.state == PrState.MERGED
    assert pr.is_draft is True


# === _check_unresolved_threads ===


def test_check_unresolved_threads_unresolved() -> None:
    node = _make_pr_node(review_threads=[_make_thread(resolved=False)])
    assert _check_unresolved_threads(node, ignore_user=None) is True


def test_check_unresolved_threads_all_resolved() -> None:
    node = _make_pr_node(review_threads=[_make_thread(resolved=True)])
    assert _check_unresolved_threads(node, ignore_user=None) is False


def test_check_unresolved_threads_empty() -> None:
    node = _make_pr_node()
    assert _check_unresolved_threads(node, ignore_user=None) is False


def test_check_unresolved_threads_ignore_user_skips_my_last_reply() -> None:
    node = _make_pr_node(review_threads=[_make_thread(last_author="myuser")])
    assert _check_unresolved_threads(node, ignore_user="myuser") is False


def test_check_unresolved_threads_ignore_user_keeps_other_replies() -> None:
    node = _make_pr_node(review_threads=[_make_thread(last_author="reviewer")])
    assert _check_unresolved_threads(node, ignore_user="myuser") is True


def test_check_unresolved_threads_ignore_user_none_counts_my_reply() -> None:
    node = _make_pr_node(review_threads=[_make_thread(last_author="myuser")])
    assert _check_unresolved_threads(node, ignore_user=None) is True


def test_check_unresolved_threads_empty_comments_counts() -> None:
    thread = {"isResolved": False, "comments": {"nodes": []}}
    node = _make_pr_node(review_threads=[thread])
    assert _check_unresolved_threads(node, ignore_user="myuser") is True


def test_check_unresolved_threads_pr_comment_by_other_flags() -> None:
    node = _make_pr_node(pr_comments=[{"author": {"login": "reviewer"}}])
    assert _check_unresolved_threads(node, ignore_user="myuser") is True


def test_check_unresolved_threads_pr_comment_by_me_not_flagged() -> None:
    node = _make_pr_node(pr_comments=[{"author": {"login": "myuser"}}])
    assert _check_unresolved_threads(node, ignore_user="myuser") is False


def test_check_unresolved_threads_pr_comment_not_checked_without_ignore_user() -> None:
    node = _make_pr_node(pr_comments=[{"author": {"login": "reviewer"}}])
    assert _check_unresolved_threads(node, ignore_user=None) is False


# === _build_board_graphql ===


def test_build_board_graphql_includes_repos_and_branches() -> None:
    query = _build_board_graphql([("org/a", "feat-1"), ("org/b", "feat-2"), ("org/a", "feat-3")])
    # repos are deduped and OR'd
    assert "repo:org/a" in query
    assert "repo:org/b" in query
    assert "head:feat-1" in query
    assert "head:feat-2" in query
    assert "head:feat-3" in query
    # author and type filters present
    assert "author:@me" in query
    assert "type:pr" in query
    # response shape we depend on
    assert "statusCheckRollup" in query
    assert "mergeable" in query
    assert "reviewThreads" in query
    assert "nameWithOwner" in query
    # pagination cursor is requested so the caller can fetch further pages
    assert "endCursor" in query


def test_build_board_graphql_without_after_omits_after_clause() -> None:
    query = _build_board_graphql([("org/a", "feat-1")])
    assert "after:" not in query


def test_build_board_graphql_with_after_includes_cursor() -> None:
    query = _build_board_graphql([("org/a", "feat-1")], after="CURSOR_ABC")
    assert "after:" in query
    assert "CURSOR_ABC" in query


# === _parse_board_page ===


def test_parse_board_page_extracts_nodes_and_cursor() -> None:
    response = json.loads(
        _make_board_response(
            nodes=[_make_pr_node(head_branch="b1", repo="org/r")],
            has_next_page=True,
            end_cursor="CURSOR_1",
        )
    )
    page = _parse_board_page(response)
    assert len(page.nodes) == 1
    assert page.has_next_page is True
    assert page.end_cursor == "CURSOR_1"
    assert page.errors == ()


def test_parse_board_page_surfaces_error_messages() -> None:
    response = json.loads(
        _make_board_response(
            nodes=[_make_pr_node(head_branch="b1", repo="org/r")],
            errors=[{"message": "Something went wrong", "type": "INTERNAL"}],
        )
    )
    page = _parse_board_page(response)
    assert any("Something went wrong" in e for e in page.errors)
    # Successful nodes still pass through alongside the errors.
    assert len(page.nodes) == 1


def test_parse_board_page_null_node_skipped() -> None:
    # A null entry in nodes (defensive guard) should not crash the parser.
    response = {
        "data": {
            "s": {
                "nodes": [None, _make_pr_node(head_branch="b1", repo="org/r")],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
        }
    }
    page = _parse_board_page(response)
    assert len(page.nodes) == 1


# === _build_prs_from_nodes ===


def test_build_prs_from_nodes_success() -> None:
    nodes = [
        _make_pr_node(number=1, head_branch="b1", repo="org/r"),
        _make_pr_node(number=2, head_branch="b2", repo="org/r", state="MERGED"),
    ]
    prs = _build_prs_from_nodes(nodes, [("org/r", "b1"), ("org/r", "b2")], unresolved_ignore_user=None)
    assert set(prs.keys()) == {("org/r", "b1"), ("org/r", "b2")}
    assert prs[("org/r", "b1")].number == 1
    assert prs[("org/r", "b2")].state == PrState.MERGED


def test_build_prs_from_nodes_unrequested_pair_skipped() -> None:
    # Defensive: a node whose (repo, branch) we didn't ask about must be ignored.
    nodes = [_make_pr_node(head_branch="other-branch", repo="org/r")]
    prs = _build_prs_from_nodes(nodes, [("org/r", "b1")], unresolved_ignore_user=None)
    assert prs == {}


def test_build_prs_from_nodes_multiple_prs_per_branch_prefers_open() -> None:
    closed = _make_pr_node(number=10, head_branch="b1", state="CLOSED", repo="org/r")
    open_pr = _make_pr_node(number=11, head_branch="b1", state="OPEN", repo="org/r")
    merged = _make_pr_node(number=12, head_branch="b1", state="MERGED", repo="org/r")
    prs = _build_prs_from_nodes([closed, merged, open_pr], [("org/r", "b1")], unresolved_ignore_user=None)
    assert prs[("org/r", "b1")].number == 11


def test_build_prs_from_nodes_dedupes_across_pages() -> None:
    # The same branch can surface on different pages (e.g. closed-then-reopened);
    # the OPEN > MERGED > CLOSED preference must hold across the accumulated set.
    page1_closed = _make_pr_node(number=20, head_branch="b1", state="CLOSED", repo="org/r")
    page2_open = _make_pr_node(number=21, head_branch="b1", state="OPEN", repo="org/r")
    prs = _build_prs_from_nodes([page1_closed, page2_open], [("org/r", "b1")], unresolved_ignore_user=None)
    assert prs[("org/r", "b1")].number == 21


# === _summarize_failed_response ===


def test_summarize_failed_response_prefers_graphql_error_messages() -> None:
    response = {"data": None, "errors": [{"message": "API rate limit exceeded"}, {"message": "secondary thing"}]}
    assert _summarize_failed_response(response, stderr_excerpt="ignored") == "API rate limit exceeded; secondary thing"


def test_summarize_failed_response_falls_back_to_top_level_message() -> None:
    response = {"message": "You have exceeded a secondary rate limit"}
    assert _summarize_failed_response(response, stderr_excerpt="ignored") == "You have exceeded a secondary rate limit"


def test_summarize_failed_response_falls_back_to_stderr() -> None:
    assert _summarize_failed_response({}, stderr_excerpt="gh: HTTP 502") == "stderr: gh: HTTP 502"


def test_summarize_failed_response_no_information() -> None:
    assert _summarize_failed_response({}, stderr_excerpt="") == "no data returned"


def test_summarize_failed_response_non_dict() -> None:
    # A bare JSON array (or other non-object) still yields a usable reason.
    assert _summarize_failed_response([1, 2, 3], stderr_excerpt="gh: weird") == "stderr: gh: weird"


# === fetch_board ===


def test_fetch_board_empty_pairs() -> None:
    cg = MagicMock()
    result = fetch_board(cg, repo_branches=[])
    assert result == FetchBoardResult(prs={})
    cg.run_process_in_background.assert_not_called()


def test_fetch_board_success() -> None:
    response = _make_board_response(nodes=[_make_pr_node(head_branch="b1", repo="org/r", number=42)])
    cg = _make_board_cg(response)
    result = fetch_board(cg, repo_branches=[("org/r", "b1")])
    assert ("org/r", "b1") in result.prs
    assert result.prs[("org/r", "b1")].number == 42
    assert result.errors == ()


def test_fetch_board_launch_error() -> None:
    cg = MagicMock()
    cg.run_process_in_background.side_effect = ProcessError(
        command=("gh", "api", "graphql"),
        returncode=1,
        stdout="",
        stderr="gh: not found",
    )
    result = fetch_board(cg, repo_branches=[("org/r", "b1")])
    assert result.prs == {}
    assert len(result.errors) == 1
    assert "gh api graphql failed" in result.errors[0]


def test_fetch_board_passes_unresolved_ignore_user() -> None:
    # When ignore_user matches the last commenter, the PR is not flagged.
    response = _make_board_response(
        nodes=[
            _make_pr_node(
                head_branch="b1",
                repo="org/r",
                review_threads=[_make_thread(resolved=False, last_author="myuser")],
            )
        ]
    )
    cg = _make_board_cg(response)
    result = fetch_board(cg, repo_branches=[("org/r", "b1")], unresolved_ignore_user="myuser")
    assert result.prs[("org/r", "b1")].has_unresolved is False


def test_fetch_board_paginates_across_pages() -> None:
    """When the first page reports hasNextPage, fetch_board follows the cursor
    and merges PRs from every page into one result.
    """
    page1 = _make_board_proc(
        _make_board_response(
            nodes=[_make_pr_node(head_branch="b1", repo="org/r", number=1)],
            has_next_page=True,
            end_cursor="CURSOR_1",
        )
    )
    page2 = _make_board_proc(
        _make_board_response(
            nodes=[_make_pr_node(head_branch="b2", repo="org/r", number=2)],
            has_next_page=False,
        )
    )
    cg = _make_paginated_board_cg(page1, page2)
    result = fetch_board(cg, repo_branches=[("org/r", "b1"), ("org/r", "b2")])
    assert result.errors == ()
    assert result.prs[("org/r", "b1")].number == 1
    assert result.prs[("org/r", "b2")].number == 2
    # The second request must carry the first page's cursor as `after`.
    assert cg.run_process_in_background.call_count == 2
    second_query = next(a for a in cg.run_process_in_background.call_args_list[1][0][0] if a.startswith("query="))
    assert "CURSOR_1" in second_query


def test_fetch_board_keeps_earlier_pages_when_later_page_permanently_fails() -> None:
    """A transport failure on page 2 must not discard page 1: the PRs already
    fetched are kept and an error is surfaced. Page 1 is never re-fetched.
    """
    page1 = _make_board_proc(
        _make_board_response(
            nodes=[_make_pr_node(head_branch="b1", repo="org/r", number=1)],
            has_next_page=True,
            end_cursor="CURSOR_1",
        )
    )
    # Page 2 returns an HTTP-403 secondary-rate-limit body on every attempt. It
    # is valid JSON but carries no `data.s` search result, so it is retried and,
    # on exhaustion, surfaced as an error rather than silently dropped.
    rate_limited_body = json.dumps(
        {"message": "You have exceeded a secondary rate limit", "documentation_url": "https://docs.github.com"}
    )
    bad = _make_board_proc(rate_limited_body, stderr="gh: HTTP 403")
    cg = _make_paginated_board_cg(page1, bad, bad, bad)
    result = fetch_board(cg, repo_branches=[("org/r", "b1"), ("org/r", "b2")], page_retrying=_no_wait_page_retrying())
    assert result.prs[("org/r", "b1")].number == 1
    assert any("failed after" in e for e in result.errors)
    assert any("secondary rate limit" in e for e in result.errors)


def test_fetch_board_retries_transient_page_then_succeeds() -> None:
    """A page whose first attempt returns no parseable JSON body is retried, and
    the retry's result is used -- no error surfaces when the retry succeeds.
    """
    bad = _make_board_proc("", stderr="temporary failure")
    good = _make_board_proc(
        _make_board_response(nodes=[_make_pr_node(head_branch="b1", repo="org/r", number=7)], has_next_page=False)
    )
    cg = _make_paginated_board_cg(bad, good)
    result = fetch_board(cg, repo_branches=[("org/r", "b1")], page_retrying=_no_wait_page_retrying())
    assert result.errors == ()
    assert result.prs[("org/r", "b1")].number == 7


def test_fetch_board_page_limit_surfaces_error() -> None:
    """If GitHub keeps reporting hasNextPage past the page cap (its ~1000-result
    ceiling), surface an explicit error rather than silently dropping data.
    """
    procs = [
        _make_board_proc(
            _make_board_response(
                nodes=[_make_pr_node(head_branch=f"b{page_index}", repo="org/r", number=page_index)],
                has_next_page=True,
                end_cursor=f"CURSOR_{page_index}",
            )
        )
        for page_index in range(_MAX_SEARCH_PAGES)
    ]
    cg = _make_paginated_board_cg(*procs)
    result = fetch_board(cg, repo_branches=[("org/r", f"b{i}") for i in range(_MAX_SEARCH_PAGES)])
    assert cg.run_process_in_background.call_count == _MAX_SEARCH_PAGES
    assert any("too many matching PRs" in e for e in result.errors)


# === GitHubDataSource.compute ===


def test_compute_no_agents() -> None:
    ds = GitHubDataSource()
    cg = MagicMock()
    ctx = make_mngr_ctx_with_cg(cg)
    fields, errors = ds.compute(agents=(), cached_fields={}, mngr_ctx=ctx)
    assert fields == {}
    assert errors == []


def test_compute_agents_without_repo() -> None:
    ds = GitHubDataSource()
    cg = MagicMock()
    ctx = make_mngr_ctx_with_cg(cg)
    agent = make_agent_details(name="a1", initial_branch="mngr/test", labels={})
    fields, errors = ds.compute(agents=(agent,), cached_fields={}, mngr_ctx=ctx)
    assert fields == {}
    assert errors == []


def test_compute_mixed_agents_with_and_without_repo() -> None:
    """Agents lacking a repo (no labels, no cache) must not crash compute()
    even when other agents in the same call have a repo.
    """
    ds = GitHubDataSource(config=GitHubDataSourceConfig(conflicts=False, unresolved=False))
    response = _make_board_response(nodes=[_make_pr_node(head_branch="branch-1", repo="org/repo")])
    cg = _make_board_cg(response)
    ctx = make_mngr_ctx_with_cg(cg)
    agent_with = make_agent_details(
        name="a1", initial_branch="branch-1", labels={"remote": "git@github.com:org/repo.git"}
    )
    agent_without = make_agent_details(name="a2", initial_branch="branch-2", labels={})
    fields, _errors = ds.compute(agents=(agent_with, agent_without), cached_fields={}, mngr_ctx=ctx)
    assert agent_with.name in fields
    assert agent_without.name not in fields


def test_compute_pr_found_populates_all_fields() -> None:
    ds = GitHubDataSource()
    response = _make_board_response(
        nodes=[
            _make_pr_node(
                head_branch="branch-1",
                repo="org/repo",
                number=7,
                mergeable="CONFLICTING",
                review_threads=[_make_thread(resolved=False)],
            )
        ]
    )
    cg = _make_board_cg(response)
    ctx = make_mngr_ctx_with_cg(cg)
    agent = make_agent_details(name="a1", initial_branch="branch-1", labels={"remote": "git@github.com:org/repo.git"})
    fields, _errors = ds.compute(agents=(agent,), cached_fields={}, mngr_ctx=ctx)
    af = fields[agent.name]
    assert FIELD_PR in af and FIELD_CI in af and FIELD_CONFLICTS in af and FIELD_UNRESOLVED in af
    assert isinstance(af[FIELD_CONFLICTS], ConflictsField) and af[FIELD_CONFLICTS].has_conflicts is True  # ty: ignore[unresolved-attribute]
    assert isinstance(af[FIELD_UNRESOLVED], UnresolvedField) and af[FIELD_UNRESOLVED].has_unresolved is True  # ty: ignore[unresolved-attribute]


def test_compute_no_pr_for_branch_generates_create_url() -> None:
    """When the fetch succeeds but no PR matches, emit a CreatePrUrlField."""
    ds = GitHubDataSource(config=GitHubDataSourceConfig(conflicts=False, unresolved=False))
    cg = _make_board_cg(_make_board_response(nodes=[]))
    ctx = make_mngr_ctx_with_cg(cg)
    agent = make_agent_details(
        name="a1", initial_branch="no-pr-branch", labels={"remote": "git@github.com:org/repo.git"}
    )
    fields, _errors = ds.compute(agents=(agent,), cached_fields={}, mngr_ctx=ctx)
    pr_field = fields[agent.name][FIELD_PR]
    assert isinstance(pr_field, CreatePrUrlField)
    assert "no-pr-branch" in pr_field.url


def test_compute_pr_fetch_error_adds_error() -> None:
    ds = GitHubDataSource(config=GitHubDataSourceConfig(conflicts=False, unresolved=False))
    agent = make_agent_details(name="a1", initial_branch="branch-1", labels={"remote": "git@github.com:org/repo.git"})
    cg = MagicMock()
    cg.run_process_in_background.side_effect = ProcessError(
        command=("gh", "api", "graphql"), returncode=1, stdout="", stderr="boom"
    )
    ctx = make_mngr_ctx_with_cg(cg)
    fields, errors = ds.compute(agents=(agent,), cached_fields={}, mngr_ctx=ctx)
    assert len(errors) > 0
    # No cache to fall back to -- emit the fetch-failed sentinel.
    pr_field = fields[agent.name].get(FIELD_PR)
    assert isinstance(pr_field, PrFetchFailedField)
    assert pr_field.repo == "org/repo"


def test_compute_pr_fetch_failed_with_cached_pr_uses_cache() -> None:
    """Fetch fails but a cached PrField exists for the same branch -- silently
    fall back to the cached field.
    """
    ds = GitHubDataSource(config=GitHubDataSourceConfig(conflicts=False, unresolved=False))
    agent = make_agent_details(name="a1", initial_branch="branch-1", labels={"remote": "git@github.com:org/repo.git"})
    cached_pr = make_pr_field(
        number=42, head_branch="branch-1", created=datetime(2028, 1, 1, 0, 0, 13, tzinfo=timezone.utc)
    )
    cached_ci = CiField(status=CiStatus.SUCCESS, created=datetime(2028, 1, 1, 0, 0, 14, tzinfo=timezone.utc))
    cached: dict[AgentName, dict[str, FieldValue]] = {
        agent.name: {FIELD_PR: cached_pr, FIELD_CI: cached_ci},
    }
    cg = MagicMock()
    cg.run_process_in_background.side_effect = ProcessError(
        command=("gh", "api", "graphql"), returncode=1, stdout="", stderr="boom"
    )
    ctx = make_mngr_ctx_with_cg(cg)
    fields, _errors = ds.compute(agents=(agent,), cached_fields=cached, mngr_ctx=ctx)
    assert fields[agent.name].get(FIELD_PR) == cached_pr
    assert fields[agent.name].get(FIELD_CI) == cached_ci


def test_compute_pr_fetch_failed_with_cached_pr_for_different_branch_emits_fetch_failed_field() -> None:
    """Cache must NOT be reused when its head_branch doesn't match the agent's
    current branch -- otherwise we'd misattribute the old PR to the new branch.
    """
    ds = GitHubDataSource(config=GitHubDataSourceConfig(conflicts=False, unresolved=False))
    agent = make_agent_details(name="a1", initial_branch="branch-2", labels={"remote": "git@github.com:org/repo.git"})
    stale_cached_pr = make_pr_field(
        number=42, head_branch="branch-1", created=datetime(2028, 1, 1, 0, 0, 15, tzinfo=timezone.utc)
    )
    cached_ci = CiField(status=CiStatus.SUCCESS, created=datetime(2028, 1, 1, 0, 0, 16, tzinfo=timezone.utc))
    cached: dict[AgentName, dict[str, FieldValue]] = {
        agent.name: {FIELD_PR: stale_cached_pr, FIELD_CI: cached_ci},
    }
    cg = MagicMock()
    cg.run_process_in_background.side_effect = ProcessError(
        command=("gh", "api", "graphql"), returncode=1, stdout="", stderr="boom"
    )
    ctx = make_mngr_ctx_with_cg(cg)
    fields, _errors = ds.compute(agents=(agent,), cached_fields=cached, mngr_ctx=ctx)
    pr_field = fields[agent.name].get(FIELD_PR)
    assert isinstance(pr_field, PrFetchFailedField)
    assert pr_field.repo == "org/repo"
    assert FIELD_CI not in fields[agent.name]


def test_compute_conflicts_and_unresolved_not_emitted_for_closed_prs() -> None:
    """Closed/merged PRs shouldn't render conflict / unresolved columns."""
    ds = GitHubDataSource()
    response = _make_board_response(nodes=[_make_pr_node(head_branch="branch-1", repo="org/repo", state="MERGED")])
    cg = _make_board_cg(response)
    ctx = make_mngr_ctx_with_cg(cg)
    agent = make_agent_details(name="a1", initial_branch="branch-1", labels={"remote": "git@github.com:org/repo.git"})
    fields, _errors = ds.compute(agents=(agent,), cached_fields={}, mngr_ctx=ctx)
    af = fields[agent.name]
    assert FIELD_PR in af and FIELD_CI in af
    assert FIELD_CONFLICTS not in af
    assert FIELD_UNRESOLVED not in af


def test_compute_falls_back_to_cached_repo_path_when_labels_lack_remote() -> None:
    """Labels don't carry a remote: fall back to the cached repo_path and let
    the derived PR/CI fields inherit the cached field's `created`.
    """
    ds = GitHubDataSource(config=GitHubDataSourceConfig(conflicts=False, unresolved=False))
    response = _make_board_response(nodes=[_make_pr_node(head_branch="branch-1", repo="org/repo")])
    cg = _make_board_cg(response)
    ctx = make_mngr_ctx_with_cg(cg)
    agent = make_agent_details(name="a1", initial_branch="branch-1", labels={})
    cached_created = datetime(2028, 1, 1, 0, 0, 17, tzinfo=timezone.utc) - timedelta(hours=2)
    cached_fields: dict[AgentName, dict[str, FieldValue]] = {
        AgentName("a1"): {"repo_path": RepoPathField(path="org/repo", created=cached_created)},
    }
    fields, _errors = ds.compute(agents=(agent,), cached_fields=cached_fields, mngr_ctx=ctx)
    pr = fields[AgentName("a1")][FIELD_PR]
    ci = fields[AgentName("a1")][FIELD_CI]
    assert pr.created == cached_created
    assert ci.created == cached_created


def test_compute_uses_now_when_labels_carry_remote() -> None:
    """Labels are world data so when they carry a remote we use them and stamp
    `created=now`, even if a (potentially stale) cached repo_path is present.
    """
    ds = GitHubDataSource(config=GitHubDataSourceConfig(conflicts=False, unresolved=False))
    response = _make_board_response(nodes=[_make_pr_node(head_branch="branch-1", repo="org/repo")])
    cg = _make_board_cg(response)
    ctx = make_mngr_ctx_with_cg(cg)
    agent = make_agent_details(name="a1", initial_branch="branch-1", labels={"remote": "git@github.com:org/repo.git"})
    stale_cached = datetime(2028, 1, 1, 0, 0, 18, tzinfo=timezone.utc) - timedelta(hours=2)
    cached_fields: dict[AgentName, dict[str, FieldValue]] = {
        AgentName("a1"): {"repo_path": RepoPathField(path="org/repo", created=stale_cached)},
    }
    fields, _errors = ds.compute(agents=(agent,), cached_fields=cached_fields, mngr_ctx=ctx)
    pr = fields[AgentName("a1")][FIELD_PR]
    delta = datetime.now(timezone.utc) - pr.created
    assert delta.total_seconds() < 60


def test_compute_disabled_pr_and_ci() -> None:
    ds = GitHubDataSource(config=GitHubDataSourceConfig(pr=False, ci=False, conflicts=False, unresolved=False))
    response = _make_board_response(nodes=[_make_pr_node(head_branch="branch-1", repo="org/repo")])
    cg = _make_board_cg(response)
    ctx = make_mngr_ctx_with_cg(cg)
    agent = make_agent_details(name="a1", initial_branch="branch-1", labels={"remote": "git@github.com:org/repo.git"})
    fields, _errors = ds.compute(agents=(agent,), cached_fields={}, mngr_ctx=ctx)
    agent_fields = fields.get(agent.name, {})
    assert FIELD_PR not in agent_fields
    assert FIELD_CI not in agent_fields


def test_compute_one_graphql_call_per_refresh() -> None:
    """Across multiple agents on multiple repos, exactly one HTTP request is made."""
    ds = GitHubDataSource()
    response = _make_board_response(
        nodes=[
            _make_pr_node(head_branch="b1", repo="org/r1", number=1),
            _make_pr_node(head_branch="b2", repo="org/r2", number=2),
            _make_pr_node(head_branch="b3", repo="org/r1", number=3),
        ]
    )
    cg = _make_board_cg(response)
    ctx = make_mngr_ctx_with_cg(cg)
    agents = (
        make_agent_details(name="a1", initial_branch="b1", labels={"remote": "git@github.com:org/r1.git"}),
        make_agent_details(name="a2", initial_branch="b2", labels={"remote": "git@github.com:org/r2.git"}),
        make_agent_details(name="a3", initial_branch="b3", labels={"remote": "git@github.com:org/r1.git"}),
    )
    fields, _errors = ds.compute(agents=agents, cached_fields={}, mngr_ctx=ctx)
    assert {a.name for a in agents} <= set(fields.keys())
    # One single HTTP request covers all three agents across two repos.
    assert cg.run_process_in_background.call_count == 1


def test_compute_query_contains_all_repo_branch_pairs() -> None:
    """The single GraphQL query should mention every (repo, branch) pair the
    agents need -- so downstream there is enough information to answer all of
    them in one round trip.
    """
    ds = GitHubDataSource()
    cg = _make_board_cg(_make_board_response(nodes=[]))
    ctx = make_mngr_ctx_with_cg(cg)
    agents = (
        make_agent_details(name="a1", initial_branch="b1", labels={"remote": "git@github.com:org/r1.git"}),
        make_agent_details(name="a2", initial_branch="b2", labels={"remote": "git@github.com:org/r2.git"}),
    )
    ds.compute(agents=agents, cached_fields={}, mngr_ctx=ctx)
    sent_cmd = cg.run_process_in_background.call_args[0][0]
    # cmd is ["gh", "api", "graphql", "-f", "query=..."]
    assert sent_cmd[:3] == ["gh", "api", "graphql"]
    query_arg = next(a for a in sent_cmd if a.startswith("query="))
    assert "repo:org/r1" in query_arg
    assert "repo:org/r2" in query_arg
    assert "head:b1" in query_arg
    assert "head:b2" in query_arg


# === PrInfo dataclass sanity ===


def test_pr_info_construct() -> None:
    info = PrInfo(
        number=1,
        title="t",
        state=PrState.OPEN,
        url="https://example.com/pr/1",
        head_branch="b",
        is_draft=False,
        check_status=CiStatus.SUCCESS,
        has_conflicts=False,
        has_unresolved=False,
    )
    assert info.number == 1
    assert info.state == PrState.OPEN
