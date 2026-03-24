"""Unit tests for test-mapreduce HTML report generation."""

from pathlib import Path

from imbue.mng.primitives import AgentName
from imbue.mng_tmr.data_types import Change
from imbue.mng_tmr.data_types import ChangeKind
from imbue.mng_tmr.data_types import ChangeStatus
from imbue.mng_tmr.data_types import DisplayCategory
from imbue.mng_tmr.data_types import IntegratorResult
from imbue.mng_tmr.data_types import TestMapReduceResult
from imbue.mng_tmr.report import _build_category_nav
from imbue.mng_tmr.report import _build_grouped_tables
from imbue.mng_tmr.report import _render_markdown
from imbue.mng_tmr.report import display_category_of
from imbue.mng_tmr.report import generate_html_report
from imbue.mng_tmr.testing import FAILED_FIX
from imbue.mng_tmr.testing import SUCCEEDED_FIX
from imbue.mng_tmr.testing import make_test_result

# --- display_category_of tests ---


def test_display_category_errored() -> None:
    assert display_category_of(make_test_result(errored=True)) == DisplayCategory.ERRORED


def test_display_category_pending() -> None:
    assert display_category_of(make_test_result()) == DisplayCategory.PENDING


def test_display_category_clean_pass() -> None:
    assert display_category_of(make_test_result(before=True, after=True)) == DisplayCategory.CLEAN_PASS


def test_display_category_fixed() -> None:
    assert (
        display_category_of(make_test_result(changes=SUCCEEDED_FIX, before=False, after=True)) == DisplayCategory.FIXED
    )


def test_display_category_regressed() -> None:
    assert (
        display_category_of(make_test_result(changes=SUCCEEDED_FIX, before=True, after=False))
        == DisplayCategory.REGRESSED
    )


def test_display_category_stuck_failed_changes() -> None:
    assert (
        display_category_of(make_test_result(changes=FAILED_FIX, before=False, after=False)) == DisplayCategory.STUCK
    )


def test_display_category_stuck_no_changes_tests_failing() -> None:
    assert display_category_of(make_test_result(before=False, after=False)) == DisplayCategory.STUCK


# --- render_markdown tests ---


def test_render_markdown_bold() -> None:
    result = _render_markdown("**bold**")
    assert "<strong>bold</strong>" in result


def test_render_markdown_plain_text() -> None:
    result = _render_markdown("plain text")
    assert "plain text" in result


# --- HTML report tests ---


def test_build_category_nav_empty() -> None:
    assert _build_category_nav({}, 0) == ""


def test_build_category_nav_single_category() -> None:
    nav = _build_category_nav({DisplayCategory.CLEAN_PASS: 5}, 5)
    assert "CLEAN_PASS (5)" in nav
    assert 'href="#cat-CLEAN_PASS"' in nav
    assert "width: 100.0%" in nav


def test_build_category_nav_multiple_categories() -> None:
    nav = _build_category_nav({DisplayCategory.CLEAN_PASS: 3, DisplayCategory.STUCK: 2}, 5)
    assert "CLEAN_PASS (3)" in nav
    assert "STUCK (2)" in nav
    assert 'href="#cat-CLEAN_PASS"' in nav
    assert 'href="#cat-STUCK"' in nav


def test_build_category_nav_pending_category() -> None:
    nav = _build_category_nav({DisplayCategory.PENDING: 3}, 3)
    assert "PENDING (3)" in nav
    assert "rgb(3, 169, 244)" in nav


def test_build_grouped_tables_groups_by_category() -> None:
    results = [
        make_test_result(before=True, after=True),
        make_test_result(changes=SUCCEEDED_FIX, before=False, after=True),
    ]
    tables_html = _build_grouped_tables(results)
    assert tables_html.index("FIXED") < tables_html.index("CLEAN_PASS")


def test_build_grouped_tables_shows_branch() -> None:
    r = TestMapReduceResult(
        test_node_id="t::c",
        agent_name=AgentName("c"),
        changes=SUCCEEDED_FIX,
        tests_passing_before=False,
        tests_passing_after=True,
        summary_markdown="fixed",
        branch_name="mng-tmr/c-abc123",
    )
    assert "mng-tmr/c-abc123" in _build_grouped_tables([r])


def test_build_grouped_tables_shows_changes_column() -> None:
    changes = {
        ChangeKind.FIX_TEST: Change(status=ChangeStatus.SUCCEEDED, summary_markdown="fixed"),
        ChangeKind.IMPROVE_TEST: Change(status=ChangeStatus.BLOCKED, summary_markdown="blocked"),
    }
    r = TestMapReduceResult(
        test_node_id="t::d",
        agent_name=AgentName("d"),
        changes=changes,
        tests_passing_before=False,
        tests_passing_after=True,
        summary_markdown="Fixed test",
    )
    tables_html = _build_grouped_tables([r])
    assert "FIX_TEST/SUCCEEDED" in tables_html
    assert "IMPROVE_TEST/BLOCKED" in tables_html


def test_build_grouped_tables_renders_markdown_summary() -> None:
    r = TestMapReduceResult(
        test_node_id="t::d",
        agent_name=AgentName("d"),
        tests_passing_before=True,
        tests_passing_after=True,
        summary_markdown="Test **passed** with `no issues`.",
    )
    tables_html = _build_grouped_tables([r])
    assert "<strong>passed</strong>" in tables_html
    assert "<code>no issues</code>" in tables_html


def test_build_grouped_tables_pending_first() -> None:
    results = [make_test_result(), make_test_result(before=True, after=True)]
    tables_html = _build_grouped_tables(results)
    assert tables_html.index("PENDING") < tables_html.index("CLEAN_PASS")


def test_generate_html_report(tmp_path: Path) -> None:
    results = [
        TestMapReduceResult(
            test_node_id="tests/test_a.py::test_pass",
            agent_name=AgentName("tmr-test-pass"),
            tests_passing_before=True,
            tests_passing_after=True,
            summary_markdown="Passed immediately",
        ),
        TestMapReduceResult(
            test_node_id="tests/test_b.py::test_fixed",
            agent_name=AgentName("tmr-test-fixed"),
            changes=SUCCEEDED_FIX,
            tests_passing_before=False,
            tests_passing_after=True,
            summary_markdown="Fixed missing import",
            branch_name="mng-tmr/test-fixed",
        ),
    ]
    output_path = tmp_path / "report.html"
    result_path = generate_html_report(results, output_path)
    assert result_path == output_path
    assert output_path.exists()
    content = output_path.read_text()
    assert "Test Map-Reduce Report" in content
    assert "CLEAN_PASS" in content
    assert "FIXED" in content
    assert 'class="nav"' in content


def test_generate_html_report_groups_clean_pass_last(tmp_path: Path) -> None:
    results = [
        make_test_result(before=True, after=True),
        make_test_result(changes=FAILED_FIX, before=False, after=False),
    ]
    tables_html = _build_grouped_tables(results)
    assert tables_html.index("STUCK") < tables_html.index("CLEAN_PASS")


def test_generate_html_report_creates_parent_dirs(tmp_path: Path) -> None:
    output_path = tmp_path / "subdir" / "nested" / "report.html"
    results = [make_test_result(before=True, after=True)]
    generate_html_report(results, output_path)
    assert output_path.exists()


def test_generate_html_report_all_display_categories(tmp_path: Path) -> None:
    results = [
        make_test_result(),
        make_test_result(changes=SUCCEEDED_FIX, before=False, after=True),
        make_test_result(changes=SUCCEEDED_FIX, before=True, after=False),
        make_test_result(changes=FAILED_FIX, before=False, after=False),
        make_test_result(errored=True),
        make_test_result(before=True, after=True),
    ]
    output_path = tmp_path / "all_categories.html"
    generate_html_report(results, output_path)
    content = output_path.read_text()
    for cat in DisplayCategory:
        assert cat.value in content


def test_generate_html_report_empty_results(tmp_path: Path) -> None:
    output_path = tmp_path / "empty.html"
    generate_html_report([], output_path)
    assert "0 test(s)" in output_path.read_text()


def test_generate_html_report_with_integrator(tmp_path: Path) -> None:
    results = [make_test_result(changes=SUCCEEDED_FIX, before=False, after=True)]
    integrator = IntegratorResult(
        agent_name=AgentName("tmr-integrator-abc123"),
        merged=("mng-tmr/a",),
        branch_name="mng-tmr/integrated-abc123",
        summary_markdown="Merged 1 branch",
    )
    output_path = tmp_path / "integrator.html"
    generate_html_report(results, output_path, integrator=integrator)
    content = output_path.read_text()
    assert "Integrator" in content
    assert "mng-tmr/integrated-abc123" in content
    assert "mng-tmr/a" in content
    assert "tmr-integrator-abc123" in content


def test_generate_html_report_integrator_with_failures(tmp_path: Path) -> None:
    results = [make_test_result(before=True, after=True)]
    integrator = IntegratorResult(
        merged=("mng-tmr/a",),
        failed=("mng-tmr/b",),
        branch_name="mng-tmr/integrated-abc123",
        summary_markdown="Partial merge",
    )
    output_path = tmp_path / "integrator_partial.html"
    generate_html_report(results, output_path, integrator=integrator)
    content = output_path.read_text()
    assert "Failed to merge" in content
    assert "mng-tmr/b" in content


def test_generate_html_report_without_integrator(tmp_path: Path) -> None:
    results = [make_test_result(before=True, after=True)]
    output_path = tmp_path / "no_integrator.html"
    generate_html_report(results, output_path)
    content = output_path.read_text()
    assert "Integrated branch:" not in content


def test_generate_html_report_integrator_html_escaped(tmp_path: Path) -> None:
    results = [make_test_result(before=True, after=True)]
    integrator = IntegratorResult(
        branch_name="<script>alert('xss')</script>",
        summary_markdown="test",
    )
    output_path = tmp_path / "escape.html"
    generate_html_report(results, output_path, integrator=integrator)
    content = output_path.read_text()
    assert "<script>" not in content
    assert "&lt;script&gt;" in content
