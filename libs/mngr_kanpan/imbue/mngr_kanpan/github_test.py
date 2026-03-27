import json
from unittest.mock import MagicMock

from imbue.concurrency_group.errors import ProcessError
from imbue.mngr_kanpan.data_types import CheckStatus
from imbue.mngr_kanpan.data_types import PrState
from imbue.mngr_kanpan.github import _parse_check_status
from imbue.mngr_kanpan.github import _parse_gh_output
from imbue.mngr_kanpan.github import _parse_pr
from imbue.mngr_kanpan.github import _parse_pr_state
from imbue.mngr_kanpan.github import fetch_all_prs

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
    assert _parse_check_status(None) == CheckStatus.UNKNOWN


def test_parse_check_status_empty_list() -> None:
    assert _parse_check_status([]) == CheckStatus.UNKNOWN


def test_parse_check_status_all_success() -> None:
    rollup = [
        {"status": "COMPLETED", "conclusion": "SUCCESS"},
        {"status": "COMPLETED", "conclusion": "SUCCESS"},
    ]
    assert _parse_check_status(rollup) == CheckStatus.PASSING


def test_parse_check_status_any_failure() -> None:
    rollup = [
        {"status": "COMPLETED", "conclusion": "SUCCESS"},
        {"status": "COMPLETED", "conclusion": "FAILURE"},
    ]
    assert _parse_check_status(rollup) == CheckStatus.FAILING


def test_parse_check_status_error_conclusion() -> None:
    rollup = [{"status": "COMPLETED", "conclusion": "ERROR"}]
    assert _parse_check_status(rollup) == CheckStatus.FAILING


def test_parse_check_status_cancelled_conclusion() -> None:
    rollup = [{"status": "COMPLETED", "conclusion": "CANCELLED"}]
    assert _parse_check_status(rollup) == CheckStatus.FAILING


def test_parse_check_status_timed_out_conclusion() -> None:
    rollup = [{"status": "COMPLETED", "conclusion": "TIMED_OUT"}]
    assert _parse_check_status(rollup) == CheckStatus.FAILING


def test_parse_check_status_action_required_conclusion() -> None:
    rollup = [{"status": "COMPLETED", "conclusion": "ACTION_REQUIRED"}]
    assert _parse_check_status(rollup) == CheckStatus.FAILING


def test_parse_check_status_pending() -> None:
    rollup = [
        {"status": "COMPLETED", "conclusion": "SUCCESS"},
        {"status": "IN_PROGRESS", "conclusion": None},
    ]
    assert _parse_check_status(rollup) == CheckStatus.PENDING


def test_parse_check_status_queued() -> None:
    rollup = [{"status": "QUEUED", "conclusion": None}]
    assert _parse_check_status(rollup) == CheckStatus.PENDING


def test_parse_check_status_failure_takes_priority_over_pending() -> None:
    rollup = [
        {"status": "IN_PROGRESS", "conclusion": None},
        {"status": "COMPLETED", "conclusion": "FAILURE"},
    ]
    assert _parse_check_status(rollup) == CheckStatus.FAILING


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
    assert pr.check_status == CheckStatus.PASSING
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
    assert pr.check_status == CheckStatus.UNKNOWN


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
    # Open PR has CI status from statusCheckRollup
    assert prs_by_number[1].state == PrState.OPEN
    assert prs_by_number[1].check_status == CheckStatus.PASSING
    # Merged PR from all-states query has UNKNOWN check status (no statusCheckRollup)
    assert prs_by_number[2].state == PrState.MERGED
    assert prs_by_number[2].check_status == CheckStatus.UNKNOWN


def test_fetch_all_prs_open_data_preferred_over_all() -> None:
    """Open PR data (with CI status) is preferred over the all-states duplicate."""
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
    assert result.prs[0].check_status == CheckStatus.FAILING


def test_fetch_all_prs_open_query_fails_all_succeeds() -> None:
    """If the open query fails but all-states succeeds, we still get PRs."""
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
