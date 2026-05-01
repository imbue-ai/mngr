import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from unittest.mock import MagicMock

from imbue.concurrency_group.errors import ProcessError
from imbue.mngr.primitives import AgentName
from imbue.mngr_kanpan.data_source import FIELD_CI
from imbue.mngr_kanpan.data_source import FIELD_CONFLICTS
from imbue.mngr_kanpan.data_source import FIELD_PR
from imbue.mngr_kanpan.data_source import FIELD_UNRESOLVED
from imbue.mngr_kanpan.data_source import FieldValue
from imbue.mngr_kanpan.data_sources.github import CiStatus
from imbue.mngr_kanpan.data_sources.github import ConflictsField
from imbue.mngr_kanpan.data_sources.github import CreatePrUrlField
from imbue.mngr_kanpan.data_sources.github import GitHubDataSource
from imbue.mngr_kanpan.data_sources.github import GitHubDataSourceConfig
from imbue.mngr_kanpan.data_sources.github import PrState
from imbue.mngr_kanpan.data_sources.github import UnresolvedField
from imbue.mngr_kanpan.data_sources.github import _PrFieldInternal
from imbue.mngr_kanpan.data_sources.github import _build_create_pr_url
from imbue.mngr_kanpan.data_sources.github import _build_pr_branch_index
from imbue.mngr_kanpan.data_sources.github import _build_unresolved_query
from imbue.mngr_kanpan.data_sources.github import _fetch_repo_prs
from imbue.mngr_kanpan.data_sources.github import _get_cached_repo_field
from imbue.mngr_kanpan.data_sources.github import _lookup_pr
from imbue.mngr_kanpan.data_sources.github import _parse_check_status
from imbue.mngr_kanpan.data_sources.github import _parse_conflicts
from imbue.mngr_kanpan.data_sources.github import _parse_gh_output
from imbue.mngr_kanpan.data_sources.github import _parse_pr
from imbue.mngr_kanpan.data_sources.github import _parse_pr_state
from imbue.mngr_kanpan.data_sources.github import _parse_unresolved
from imbue.mngr_kanpan.data_sources.github import _pr_priority
from imbue.mngr_kanpan.data_sources.github import fetch_all_prs
from imbue.mngr_kanpan.data_sources.repo_paths import RepoPathField
from imbue.mngr_kanpan.testing import make_agent_details
from imbue.mngr_kanpan.testing import make_mngr_ctx_with_cg

_NOW = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)


def _make_internal_pr(
    number: int = 1,
    branch: str = "test-branch",
    state: PrState = PrState.OPEN,
    check_status: CiStatus = CiStatus.PASSING,
    created: datetime | None = None,
) -> _PrFieldInternal:
    return _PrFieldInternal(
        number=number,
        title=f"PR {number}",
        state=state,
        url=f"https://github.com/org/repo/pull/{number}",
        head_branch=branch,
        is_draft=False,
        internal_check_status=check_status,
        created=created if created is not None else _NOW,
    )


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
    repo_field = RepoPathField(path="org/repo", created=_NOW)
    cached: dict[AgentName, dict[str, FieldValue]] = {
        AgentName("a1"): {"repo_path": repo_field},
    }
    result = _get_cached_repo_field(cached, AgentName("a1"))
    assert result == repo_field


def test_get_cached_repo_field_not_found() -> None:
    assert _get_cached_repo_field({}, AgentName("a1")) is None


def test_get_cached_repo_field_wrong_type() -> None:
    cached: dict[AgentName, dict[str, FieldValue]] = {
        AgentName("a1"): {"repo_path": _make_internal_pr()},
    }
    assert _get_cached_repo_field(cached, AgentName("a1")) is None


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
    assert "comments" in query
    assert "author" in query
    assert "login" in query


# === _parse_unresolved ===


def _make_unresolved_response(
    threads: list[dict[str, object]] | None = None,
    pr_comments: list[dict[str, object]] | None = None,
) -> str:
    """Build a GraphQL response JSON for _parse_unresolved tests."""
    pr_data: dict[str, object] = {
        "reviewThreads": {"nodes": threads or []},
    }
    if pr_comments is not None:
        pr_data["comments"] = {"nodes": pr_comments}
    return json.dumps({"data": {"repository": {"pullRequest": pr_data}}})


def _thread(*, resolved: bool = False, last_author: str | None = None) -> dict[str, object]:
    """Build a review thread node for _parse_unresolved tests."""
    node: dict[str, object] = {"isResolved": resolved}
    if last_author is not None:
        node["comments"] = {"nodes": [{"author": {"login": last_author}}]}
    return node


def test_parse_unresolved_has_unresolved() -> None:
    data = _make_unresolved_response(threads=[_thread(resolved=True), _thread(resolved=False)])
    assert _parse_unresolved(data) is True


def test_parse_unresolved_all_resolved() -> None:
    assert _parse_unresolved(_make_unresolved_response(threads=[_thread(resolved=True)])) is False


def test_parse_unresolved_no_threads() -> None:
    assert _parse_unresolved(_make_unresolved_response()) is False


def test_parse_unresolved_invalid_json() -> None:
    assert _parse_unresolved("not json") is False


def test_parse_unresolved_ignore_user_skips_matching_threads() -> None:
    data = _make_unresolved_response(threads=[_thread(last_author="myuser")])
    assert _parse_unresolved(data, ignore_user="myuser") is False


def test_parse_unresolved_ignore_user_keeps_other_threads() -> None:
    data = _make_unresolved_response(threads=[_thread(last_author="reviewer")])
    assert _parse_unresolved(data, ignore_user="myuser") is True


def test_parse_unresolved_ignore_user_none_counts_all() -> None:
    data = _make_unresolved_response(threads=[_thread(last_author="myuser")])
    assert _parse_unresolved(data, ignore_user=None) is True


def test_parse_unresolved_ignore_user_flags_thread_where_someone_else_responded_last() -> None:
    """I started the thread but someone else responded and I haven't replied yet."""
    data = _make_unresolved_response(threads=[_thread(last_author="reviewer")])
    assert _parse_unresolved(data, ignore_user="myuser") is True


def test_parse_unresolved_ignore_user_skips_thread_where_i_responded_last() -> None:
    """Someone else started the thread but I already replied."""
    data = _make_unresolved_response(threads=[_thread(last_author="myuser")])
    assert _parse_unresolved(data, ignore_user="myuser") is False


def test_parse_unresolved_ignore_user_empty_comments_counts_as_unresolved() -> None:
    node: dict[str, object] = {"isResolved": False, "comments": {"nodes": []}}
    assert _parse_unresolved(_make_unresolved_response(threads=[node]), ignore_user="myuser") is True


# --- PR conversation comments ---


def test_parse_unresolved_pr_comment_by_other_flags_unresolved() -> None:
    """Last PR conversation comment is by someone else -- needs response."""
    data = _make_unresolved_response(pr_comments=[{"author": {"login": "reviewer"}}])
    assert _parse_unresolved(data, ignore_user="myuser") is True


def test_parse_unresolved_pr_comment_by_me_not_flagged() -> None:
    """Last PR conversation comment is by me -- I already replied."""
    data = _make_unresolved_response(pr_comments=[{"author": {"login": "myuser"}}])
    assert _parse_unresolved(data, ignore_user="myuser") is False


def test_parse_unresolved_pr_comment_not_checked_without_ignore_user() -> None:
    """Without ignore_user, PR conversation comments are not checked."""
    data = _make_unresolved_response(pr_comments=[{"author": {"login": "reviewer"}}])
    assert _parse_unresolved(data, ignore_user=None) is False


def test_parse_unresolved_no_pr_comments_is_clean() -> None:
    """No PR conversation comments and no unresolved threads -- clean."""
    data = _make_unresolved_response(pr_comments=[])
    assert _parse_unresolved(data, ignore_user="myuser") is False


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
                "title": f"PR {number}",
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
    repo_path, result = _fetch_repo_prs(cg, "org/repo", _NOW)
    assert repo_path == "org/repo"
    assert result.error is None
    assert len(result.prs) == 1
    assert result.prs[0].number == 1
    assert result.prs[0].head_branch == "branch-1"
    assert result.prs[0].created == _NOW


def test_fetch_repo_prs_error() -> None:
    cg = MagicMock()
    proc_fail = MagicMock()
    proc_fail.read_stdout.return_value = ""
    proc_fail.read_stderr.return_value = "some error"
    proc_fail.returncode = 1
    cg.run_process_in_background.side_effect = [proc_fail, proc_fail]
    repo_path, result = _fetch_repo_prs(cg, "org/repo", _NOW)
    assert repo_path == "org/repo"
    assert result.error is not None


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
    even when other agents in the same call have a repo. Regression test for
    a KeyError on agent_created[agent.name] when the per-agent staleness map
    was not populated for unlabeled agents.
    """
    ds = GitHubDataSource(config=GitHubDataSourceConfig(conflicts=False, unresolved=False))
    cg = _make_fetch_cg(_make_open_pr_json(1, "branch-1"), _make_open_pr_json(1, "branch-1"))
    ctx = make_mngr_ctx_with_cg(cg)
    agent_with = make_agent_details(
        name="a1", initial_branch="branch-1", labels={"remote": "git@github.com:org/repo.git"}
    )
    agent_without = make_agent_details(name="a2", initial_branch="branch-2", labels={})
    fields, _errors = ds.compute(agents=(agent_with, agent_without), cached_fields={}, mngr_ctx=ctx)
    assert agent_with.name in fields
    assert agent_without.name not in fields


def test_compute_agents_with_cached_repo_path() -> None:
    """Uses cached repo_path field from previous cycle if available."""
    ds = GitHubDataSource(config=GitHubDataSourceConfig(conflicts=False, unresolved=False))
    # Use a cg that returns a PR for branch-1
    cg = _make_fetch_cg(_make_open_pr_json(1, "branch-1"), _make_open_pr_json(1, "branch-1"))
    ctx = make_mngr_ctx_with_cg(cg)
    # Provide repo path via label (simpler than full cached repo_path)
    agent_with_label = make_agent_details(
        name="a1", initial_branch="branch-1", labels={"remote": "git@github.com:org/repo.git"}
    )
    fields, errors = ds.compute(agents=(agent_with_label,), cached_fields={}, mngr_ctx=ctx)
    assert agent_with_label.name in fields
    assert FIELD_PR in fields[agent_with_label.name]
    assert FIELD_CI in fields[agent_with_label.name]


def test_compute_no_pr_for_branch_generates_create_url_in_pr_slot() -> None:
    """When no PR found, CreatePrUrlField should be placed in the 'pr' slot."""
    ds = GitHubDataSource(config=GitHubDataSourceConfig(pr=True, ci=True, conflicts=False, unresolved=False))
    agent = make_agent_details(
        name="a1", initial_branch="no-pr-branch", labels={"remote": "git@github.com:org/repo.git"}
    )
    # Return empty PR list so no PRs exist for branch
    cg = _make_fetch_cg("[]", "[]")
    ctx = make_mngr_ctx_with_cg(cg)
    fields, errors = ds.compute(agents=(agent,), cached_fields={}, mngr_ctx=ctx)
    assert agent.name in fields
    assert FIELD_PR in fields[agent.name]
    create_url_field = fields[agent.name][FIELD_PR]
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
    ctx = make_mngr_ctx_with_cg(cg)
    fields, errors = ds.compute(agents=(agent,), cached_fields={}, mngr_ctx=ctx)
    assert len(errors) > 0


def test_compute_with_conflicts_and_unresolved() -> None:
    """Full compute with conflicts and unresolved metadata fetching."""
    ds = GitHubDataSource(config=GitHubDataSourceConfig(pr=True, ci=True, conflicts=True, unresolved=True))
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

    ctx = make_mngr_ctx_with_cg(cg)
    fields, errors = ds.compute(agents=(agent,), cached_fields={}, mngr_ctx=ctx)
    assert agent.name in fields
    assert FIELD_PR in fields[agent.name]
    assert FIELD_CONFLICTS in fields[agent.name]
    assert FIELD_UNRESOLVED in fields[agent.name]
    assert isinstance(fields[agent.name][FIELD_CONFLICTS], ConflictsField)
    assert isinstance(fields[agent.name][FIELD_UNRESOLVED], UnresolvedField)


def test_compute_falls_back_to_cached_repo_path_when_labels_lack_remote() -> None:
    """If labels don't carry a remote, we fall back to the cached repo_path
    and the derived PR/CI fields inherit the cached field's `created`.
    """
    ds = GitHubDataSource(config=GitHubDataSourceConfig(conflicts=False, unresolved=False))
    cg = _make_fetch_cg(_make_open_pr_json(1, "branch-1"), _make_open_pr_json(1, "branch-1"))
    ctx = make_mngr_ctx_with_cg(cg)
    agent = make_agent_details(name="a1", initial_branch="branch-1", labels={})
    cached_created = _NOW - timedelta(hours=2)
    cached_fields: dict[AgentName, dict[str, FieldValue]] = {
        AgentName("a1"): {"repo_path": RepoPathField(path="org/repo", created=cached_created)},
    }
    fields, _errors = ds.compute(agents=(agent,), cached_fields=cached_fields, mngr_ctx=ctx)
    pr = fields[AgentName("a1")][FIELD_PR]
    ci = fields[AgentName("a1")][FIELD_CI]
    assert pr.created == cached_created
    assert ci.created == cached_created


def test_compute_uses_now_when_labels_carry_remote() -> None:
    """Labels are world data and always at least as fresh as the cache, so
    when they carry a remote we use them and stamp `created=now` -- even if
    a (potentially stale) cached repo_path is also available.
    """
    ds = GitHubDataSource(config=GitHubDataSourceConfig(conflicts=False, unresolved=False))
    cg = _make_fetch_cg(_make_open_pr_json(1, "branch-1"), _make_open_pr_json(1, "branch-1"))
    ctx = make_mngr_ctx_with_cg(cg)
    agent = make_agent_details(name="a1", initial_branch="branch-1", labels={"remote": "git@github.com:org/repo.git"})
    # Even with a stale cache present, labels should win.
    stale_cached = _NOW - timedelta(hours=2)
    cached_fields: dict[AgentName, dict[str, FieldValue]] = {
        AgentName("a1"): {"repo_path": RepoPathField(path="org/repo", created=stale_cached)},
    }
    fields, _errors = ds.compute(agents=(agent,), cached_fields=cached_fields, mngr_ctx=ctx)
    pr = fields[AgentName("a1")][FIELD_PR]
    delta = datetime.now(timezone.utc) - pr.created
    assert delta.total_seconds() < 60


def test_compute_disabled_pr_and_ci() -> None:
    ds = GitHubDataSource(config=GitHubDataSourceConfig(pr=False, ci=False, conflicts=False, unresolved=False))
    agent = make_agent_details(name="a1", initial_branch="branch-1", labels={"remote": "git@github.com:org/repo.git"})
    cg = _make_fetch_cg(_make_open_pr_json(1, "branch-1"), _make_open_pr_json(1, "branch-1"))
    ctx = make_mngr_ctx_with_cg(cg)
    fields, errors = ds.compute(agents=(agent,), cached_fields={}, mngr_ctx=ctx)
    # pr=False means no FIELD_PR; ci=False means no FIELD_CI
    agent_fields = fields.get(agent.name, {})
    assert FIELD_PR not in agent_fields
    assert FIELD_CI not in agent_fields


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


# === _parse_check_status ===


def test_parse_check_status_none() -> None:
    assert _parse_check_status(None) == CiStatus.UNKNOWN


def test_parse_check_status_empty_list() -> None:
    assert _parse_check_status([]) == CiStatus.UNKNOWN


def test_parse_check_status_all_success() -> None:
    rollup = [
        {"status": "COMPLETED", "conclusion": "SUCCESS"},
        {"status": "COMPLETED", "conclusion": "SUCCESS"},
    ]
    assert _parse_check_status(rollup) == CiStatus.PASSING


def test_parse_check_status_any_failure() -> None:
    rollup = [
        {"status": "COMPLETED", "conclusion": "SUCCESS"},
        {"status": "COMPLETED", "conclusion": "FAILURE"},
    ]
    assert _parse_check_status(rollup) == CiStatus.FAILING


def test_parse_check_status_error_conclusion() -> None:
    rollup = [{"status": "COMPLETED", "conclusion": "ERROR"}]
    assert _parse_check_status(rollup) == CiStatus.FAILING


def test_parse_check_status_cancelled_conclusion() -> None:
    rollup = [{"status": "COMPLETED", "conclusion": "CANCELLED"}]
    assert _parse_check_status(rollup) == CiStatus.FAILING


def test_parse_check_status_timed_out_conclusion() -> None:
    rollup = [{"status": "COMPLETED", "conclusion": "TIMED_OUT"}]
    assert _parse_check_status(rollup) == CiStatus.FAILING


def test_parse_check_status_action_required_conclusion() -> None:
    rollup = [{"status": "COMPLETED", "conclusion": "ACTION_REQUIRED"}]
    assert _parse_check_status(rollup) == CiStatus.FAILING


def test_parse_check_status_pending() -> None:
    rollup = [
        {"status": "COMPLETED", "conclusion": "SUCCESS"},
        {"status": "IN_PROGRESS", "conclusion": None},
    ]
    assert _parse_check_status(rollup) == CiStatus.PENDING


def test_parse_check_status_queued() -> None:
    rollup = [{"status": "QUEUED", "conclusion": None}]
    assert _parse_check_status(rollup) == CiStatus.PENDING


def test_parse_check_status_failure_takes_priority_over_pending() -> None:
    rollup = [
        {"status": "IN_PROGRESS", "conclusion": None},
        {"status": "COMPLETED", "conclusion": "FAILURE"},
    ]
    assert _parse_check_status(rollup) == CiStatus.FAILING


# === _parse_pr ===


def test_parse_pr() -> None:
    raw = {
        "number": 42,
        "title": "Add feature X",
        "state": "OPEN",
        "url": "https://github.com/org/repo/pull/42",
        "headRefName": "mngr/my-agent",
        "statusCheckRollup": [
            {"status": "COMPLETED", "conclusion": "SUCCESS"},
        ],
    }
    pr = _parse_pr(raw)
    assert pr.number == 42
    assert pr.title == "Add feature X"
    assert pr.state == PrState.OPEN
    assert pr.url == "https://github.com/org/repo/pull/42"
    assert pr.head_branch == "mngr/my-agent"
    assert pr.check_status == CiStatus.PASSING
    assert pr.is_draft is False


def test_parse_pr_draft() -> None:
    raw = {
        "number": 99,
        "title": "WIP feature",
        "state": "OPEN",
        "url": "https://github.com/org/repo/pull/99",
        "headRefName": "mngr/wip",
        "statusCheckRollup": [],
        "isDraft": True,
    }
    pr = _parse_pr(raw)
    assert pr.is_draft is True
    assert pr.state == PrState.OPEN


def test_parse_pr_merged_with_no_checks() -> None:
    raw = {
        "number": 10,
        "title": "Fix bug",
        "state": "MERGED",
        "url": "https://github.com/org/repo/pull/10",
        "headRefName": "mngr/fix-bug",
        "statusCheckRollup": [],
    }
    pr = _parse_pr(raw)
    assert pr.state == PrState.MERGED
    assert pr.check_status == CiStatus.UNKNOWN


# === _parse_gh_output ===


def test_parse_gh_output_success() -> None:
    raw = [{"number": 1, "title": "PR 1"}]
    result = _parse_gh_output(json.dumps(raw), 0, "")
    assert result == raw


def test_parse_gh_output_nonzero_exit_returns_stderr() -> None:
    result = _parse_gh_output("", 1, "HTTP 504: Gateway Timeout")
    assert isinstance(result, str)
    assert "504" in result


def test_parse_gh_output_nonzero_exit_falls_back_to_stdout() -> None:
    result = _parse_gh_output("some output", 1, "")
    assert isinstance(result, str)
    assert "some output" in result


def test_parse_gh_output_nonzero_exit_falls_back_to_exit_code() -> None:
    result = _parse_gh_output("", 128, "")
    assert isinstance(result, str)
    assert "128" in result


def test_parse_gh_output_invalid_json() -> None:
    result = _parse_gh_output("not json", 0, "")
    assert isinstance(result, str)
    assert "parse error" in result


# === fetch_all_prs ===


def _make_mock_proc(stdout: str, returncode: int = 0, stderr: str = "") -> MagicMock:
    """Create a mock RunningProcess with the given output."""
    proc = MagicMock()
    proc.read_stdout.return_value = stdout
    proc.read_stderr.return_value = stderr
    proc.returncode = returncode
    return proc


def _make_mock_cg(open_stdout: str, all_stdout: str) -> MagicMock:
    """Create a mock ConcurrencyGroup returning two background processes."""
    cg = MagicMock()
    open_proc = _make_mock_proc(open_stdout)
    all_proc = _make_mock_proc(all_stdout)
    cg.run_process_in_background.side_effect = [open_proc, all_proc]
    return cg


def test_fetch_all_prs_success() -> None:
    open_prs = [
        {
            "number": 1,
            "title": "PR 1",
            "state": "OPEN",
            "url": "https://github.com/org/repo/pull/1",
            "headRefName": "branch-1",
            "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        },
    ]
    all_prs = [
        {
            "number": 1,
            "title": "PR 1",
            "state": "OPEN",
            "url": "https://github.com/org/repo/pull/1",
            "headRefName": "branch-1",
        },
        {
            "number": 2,
            "title": "PR 2",
            "state": "MERGED",
            "url": "https://github.com/org/repo/pull/2",
            "headRefName": "branch-2",
        },
    ]
    cg = _make_mock_cg(json.dumps(open_prs), json.dumps(all_prs))
    result = fetch_all_prs(cg)
    assert len(result.prs) == 2
    assert result.error is None
    prs_by_number = {pr.number: pr for pr in result.prs}
    assert prs_by_number[1].state == PrState.OPEN
    assert prs_by_number[1].check_status == CiStatus.PASSING
    assert prs_by_number[2].state == PrState.MERGED
    assert prs_by_number[2].check_status == CiStatus.UNKNOWN


def test_fetch_all_prs_open_data_preferred_over_all() -> None:
    open_prs = [
        {
            "number": 5,
            "title": "My PR",
            "state": "OPEN",
            "url": "https://github.com/org/repo/pull/5",
            "headRefName": "branch-5",
            "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}],
        },
    ]
    all_prs = [
        {
            "number": 5,
            "title": "My PR",
            "state": "OPEN",
            "url": "https://github.com/org/repo/pull/5",
            "headRefName": "branch-5",
        },
    ]
    cg = _make_mock_cg(json.dumps(open_prs), json.dumps(all_prs))
    result = fetch_all_prs(cg)
    assert len(result.prs) == 1
    assert result.prs[0].check_status == CiStatus.FAILING


def test_fetch_all_prs_open_query_fails_all_succeeds() -> None:
    all_prs = [
        {
            "number": 3,
            "title": "PR 3",
            "state": "MERGED",
            "url": "https://github.com/org/repo/pull/3",
            "headRefName": "branch-3",
        },
    ]
    cg = MagicMock()
    open_proc = _make_mock_proc("", returncode=1, stderr="HTTP 504")
    all_proc = _make_mock_proc(json.dumps(all_prs))
    cg.run_process_in_background.side_effect = [open_proc, all_proc]

    result = fetch_all_prs(cg)
    assert len(result.prs) == 1
    assert result.prs[0].number == 3
    assert result.error is None


def test_fetch_all_prs_both_queries_fail() -> None:
    cg = MagicMock()
    open_proc = _make_mock_proc("", returncode=1, stderr="timeout")
    all_proc = _make_mock_proc("", returncode=1, stderr="timeout")
    cg.run_process_in_background.side_effect = [open_proc, all_proc]

    result = fetch_all_prs(cg)
    assert result.prs == ()
    assert result.error is not None
    assert "gh pr list failed" in result.error


def test_fetch_all_prs_launch_error() -> None:
    cg = MagicMock()
    cg.run_process_in_background.side_effect = ProcessError(
        command=("gh", "pr", "list"),
        returncode=1,
        stdout="",
        stderr="gh: not found",
    )
    result = fetch_all_prs(cg)
    assert result.prs == ()
    assert result.error is not None
    assert "gh pr list failed" in result.error


def test_fetch_all_prs_invalid_json() -> None:
    cg = _make_mock_cg("not valid json", "not valid json")
    result = fetch_all_prs(cg)
    assert result.prs == ()
    assert result.error is not None
    assert "parse" in result.error.lower()


def test_fetch_all_prs_empty_list() -> None:
    cg = _make_mock_cg("[]", "[]")
    result = fetch_all_prs(cg)
    assert result.prs == ()
    assert result.error is None


def test_fetch_all_prs_passes_cwd(tmp_path: MagicMock) -> None:
    cg = _make_mock_cg("[]", "[]")
    fetch_all_prs(cg, cwd=tmp_path)
    assert cg.run_process_in_background.call_count == 2
    for call in cg.run_process_in_background.call_args_list:
        assert call.kwargs.get("cwd") == tmp_path or call[1].get("cwd") == tmp_path


def test_fetch_all_prs_passes_repo() -> None:
    cg = _make_mock_cg("[]", "[]")
    fetch_all_prs(cg, repo="org/repo")
    assert cg.run_process_in_background.call_count == 2
    for call in cg.run_process_in_background.call_args_list:
        cmd = call[0][0]
        assert "--repo" in cmd
        assert cmd[cmd.index("--repo") + 1] == "org/repo"
