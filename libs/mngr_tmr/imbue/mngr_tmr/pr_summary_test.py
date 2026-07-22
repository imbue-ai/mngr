"""Unit tests for the TMR pull-request summary builder."""

import json
from pathlib import Path

from imbue.mngr_tmr.pr_summary import build_escalations_table
from imbue.mngr_tmr.pr_summary import build_pr_title
from imbue.mngr_tmr.pr_summary import build_status_breakdown
from imbue.mngr_tmr.pr_summary import collect_results
from imbue.mngr_tmr.prompts import TESTING_AGENT_OUTCOME_FILENAME


def write_outcome(inputs_dir: Path, agent_name: str, payload: dict[str, object]) -> None:
    """Write a mapper outcome where the summary builder expects to find it."""
    target = inputs_dir / agent_name / "test_output" / TESTING_AGENT_OUTCOME_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload))


def _fix_outcome(kind: str = "FIX_TEST", **extra: object) -> dict[str, object]:
    return {
        "changes": {kind: {"status": "SUCCEEDED", "summary_markdown": "fixed"}},
        "errored": False,
        "tests_passing_before": False,
        "tests_passing_after": True,
        "summary_markdown": "ok",
        **extra,
    }


def test_collect_results_skips_agents_without_an_outcome(tmp_path: Path) -> None:
    write_outcome(tmp_path, "has-outcome", _fix_outcome())
    (tmp_path / "no-outcome").mkdir()
    results = collect_results(tmp_path)
    assert [str(r.agent_name) for r in results] == ["has-outcome"]


def test_collect_results_on_missing_directory(tmp_path: Path) -> None:
    assert collect_results(tmp_path / "nope") == []


def test_status_breakdown_counts_each_section(tmp_path: Path) -> None:
    write_outcome(tmp_path, "a", _fix_outcome())
    write_outcome(tmp_path, "b", _fix_outcome(kind="FIX_IMPL"))
    write_outcome(
        tmp_path,
        "c",
        {
            "changes": {"FIX_TEST": {"status": "FAILED", "summary_markdown": "no"}},
            "tests_passing_before": False,
            "tests_passing_after": False,
        },
    )
    breakdown = build_status_breakdown(collect_results(tmp_path))
    assert "### Mapper outcomes (3 total)" in breakdown
    assert "| Implementation fixes | 1 |" in breakdown
    assert "| Non-implementation fixes | 1 |" in breakdown
    assert "| Unresolved | 1 |" in breakdown


def test_status_breakdown_omits_empty_sections(tmp_path: Path) -> None:
    write_outcome(tmp_path, "a", _fix_outcome())
    assert "Unresolved" not in build_status_breakdown(collect_results(tmp_path))


def test_escalations_table_reports_none(tmp_path: Path) -> None:
    write_outcome(tmp_path, "a", _fix_outcome())
    assert build_escalations_table(collect_results(tmp_path)) == "### Escalations\n\nNone reported."


def test_escalations_table_lists_blockers_before_shared_patterns(tmp_path: Path) -> None:
    write_outcome(
        tmp_path,
        "a",
        _fix_outcome(escalations=[{"kind": "SHARED_PATTERN", "title": "pattern", "detail_markdown": "d"}]),
    )
    write_outcome(
        tmp_path,
        "b",
        _fix_outcome(escalations=[{"kind": "BLOCKER", "title": "blocker", "detail_markdown": "d"}]),
    )
    table = build_escalations_table(collect_results(tmp_path))
    assert "### Escalations (2)" in table
    assert table.index("| Blocker |") < table.index("| Shared pattern |")


def test_escalations_from_a_passing_test_are_still_reported(tmp_path: Path) -> None:
    """The whole point of the orthogonal field: a clean pass can escalate."""
    write_outcome(
        tmp_path,
        "clean",
        {
            "changes": {},
            "tests_passing_before": True,
            "tests_passing_after": True,
            "escalations": [{"kind": "SHARED_PATTERN", "title": "seen elsewhere", "detail_markdown": "d"}],
        },
    )
    assert "seen elsewhere" in build_escalations_table(collect_results(tmp_path))


def test_escalation_cells_are_escaped(tmp_path: Path) -> None:
    """Pipes would split the cell and newlines would end the row."""
    write_outcome(
        tmp_path,
        "a",
        _fix_outcome(
            escalations=[{"kind": "BLOCKER", "title": "a | b", "detail_markdown": "first line\nsecond line"}]
        ),
    )
    table = build_escalations_table(collect_results(tmp_path))
    assert r"a \| b" in table
    assert "second line" not in table
    assert len([line for line in table.splitlines() if line.startswith("| Blocker")]) == 1


def test_pr_title_summarizes_the_run(tmp_path: Path) -> None:
    write_outcome(tmp_path, "a", _fix_outcome())
    write_outcome(
        tmp_path,
        "b",
        _fix_outcome(escalations=[{"kind": "BLOCKER", "title": "t", "detail_markdown": "d"}]),
    )
    title = build_pr_title("tmr-mngr/20260721085455/reducer", collect_results(tmp_path))
    assert title == "TMR tmr-mngr 2026-07-21: 2 fixed, 1 escalated"


def test_pr_title_for_an_all_clean_run(tmp_path: Path) -> None:
    write_outcome(tmp_path, "a", {"changes": {}, "tests_passing_before": True, "tests_passing_after": True})
    title = build_pr_title("tmr-mngr/20260721085455/reducer", collect_results(tmp_path))
    assert title == "TMR tmr-mngr 2026-07-21: 1 tests clean"


def test_pr_title_tolerates_an_unexpected_branch_shape() -> None:
    """A non-timestamp run name is passed through rather than mangled."""
    assert build_pr_title("weird-branch", []) == "TMR weird-branch: 0 tests clean"
