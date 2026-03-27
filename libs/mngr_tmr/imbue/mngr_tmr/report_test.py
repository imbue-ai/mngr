"""Unit tests for test-mapreduce HTML report generation."""

from pathlib import Path

from imbue.mngr.primitives import AgentName
from imbue.mngr_tmr.data_types import Change
from imbue.mngr_tmr.data_types import ChangeKind
from imbue.mngr_tmr.data_types import ChangeStatus
from imbue.mngr_tmr.data_types import IntegratorResult
from imbue.mngr_tmr.data_types import ReportSection
from imbue.mngr_tmr.data_types import TestMapReduceResult
from imbue.mngr_tmr.report import _build_grouped_tables
from imbue.mngr_tmr.report import _build_toc_sidebar
from imbue.mngr_tmr.report import _merged_status
from imbue.mngr_tmr.report import _render_markdown
from imbue.mngr_tmr.report import generate_html_report
from imbue.mngr_tmr.report import report_section_of
from imbue.mngr_tmr.testing import FAILED_FIX
from imbue.mngr_tmr.testing import SUCCEEDED_FIX
from imbue.mngr_tmr.testing import make_test_result

# --- report_section_of tests ---


def test_report_section_errored() -> None:
    assert report_section_of(make_test_result(errored=True)) == ReportSection.BLOCKED


def test_report_section_running() -> None:
    assert report_section_of(make_test_result()) == ReportSection.RUNNING


def test_report_section_clean_pass() -> None:
    assert report_section_of(make_test_result(before=True, after=True)) == ReportSection.CLEAN_PASS


def test_report_section_non_impl_fixes() -> None:
    assert (
        report_section_of(make_test_result(changes=SUCCEEDED_FIX, before=False, after=True))
        == ReportSection.NON_IMPL_FIXES
    )


def test_report_section_impl_fixes() -> None:
    impl_fix = {ChangeKind.FIX_IMPL: Change(status=ChangeStatus.SUCCEEDED, summary_markdown="fixed")}
    assert report_section_of(make_test_result(changes=impl_fix, before=False, after=True)) == ReportSection.IMPL_FIXES


def test_report_section_blocked_all_changes_blocked() -> None:
    blocked_changes = {ChangeKind.FIX_TEST: Change(status=ChangeStatus.BLOCKED, summary_markdown="blocked")}
    assert (
        report_section_of(make_test_result(changes=blocked_changes, before=False, after=False))
        == ReportSection.BLOCKED
    )


def test_report_section_failed_changes_are_non_impl() -> None:
    """FAILED (not BLOCKED) changes route to NON_IMPL_FIXES, not BLOCKED."""
    assert (
        report_section_of(make_test_result(changes=FAILED_FIX, before=False, after=False))
        == ReportSection.NON_IMPL_FIXES
    )


def test_report_section_blocked_no_changes_tests_failing() -> None:
    assert report_section_of(make_test_result(before=False, after=False)) == ReportSection.BLOCKED


# --- render_markdown tests ---


def test_render_markdown_bold() -> None:
    result = _render_markdown("**bold**")
    assert "<strong>bold</strong>" in result


def test_render_markdown_plain_text() -> None:
    result = _render_markdown("plain text")
    assert "plain text" in result


# --- _build_toc_sidebar tests ---


def test_build_toc_sidebar_empty() -> None:
    assert _build_toc_sidebar({}) == ""


def test_build_toc_sidebar_single_section() -> None:
    toc = _build_toc_sidebar({ReportSection.CLEAN_PASS: 5})
    assert "Clean pass (5)" in toc
    assert 'href="#sec-CLEAN_PASS"' in toc


def test_build_toc_sidebar_multiple_sections() -> None:
    toc = _build_toc_sidebar({ReportSection.CLEAN_PASS: 3, ReportSection.BLOCKED: 2})
    assert "Clean pass (3)" in toc
    assert "Blocked (2)" in toc


def test_build_toc_sidebar_running_section() -> None:
    toc = _build_toc_sidebar({ReportSection.RUNNING: 3})
    assert "Running (3)" in toc
    assert "rgb(3, 169, 244)" in toc


# --- _merged_status tests ---


def test_merged_status_no_integrator() -> None:
    r = make_test_result(before=True, after=True)
    assert _merged_status(r, None) == ""


def test_merged_status_no_branch() -> None:
    r = make_test_result(before=True, after=True)
    integrator = IntegratorResult(squashed_branches=("mngr-tmr/a",))
    assert _merged_status(r, integrator) == ""


def test_merged_status_squashed() -> None:
    r = TestMapReduceResult(
        test_node_id="t::t",
        agent_name=AgentName("a"),
        branch_name="mngr-tmr/a",
        tests_passing_before=False,
        tests_passing_after=True,
        changes=SUCCEEDED_FIX,
    )
    integrator = IntegratorResult(squashed_branches=("mngr-tmr/a",))
    assert "10003" in _merged_status(r, integrator)


def test_merged_status_impl_priority() -> None:
    r = TestMapReduceResult(
        test_node_id="t::t",
        agent_name=AgentName("a"),
        branch_name="mngr-tmr/b",
        tests_passing_before=False,
        tests_passing_after=True,
        changes={ChangeKind.FIX_IMPL: Change(status=ChangeStatus.SUCCEEDED, summary_markdown="fixed")},
    )
    integrator = IntegratorResult(impl_priority=("mngr-tmr/b",), impl_commit_hashes={"mngr-tmr/b": "abc123def"})
    status = _merged_status(r, integrator)
    assert "abc123def" in status
    assert "<code>" in status


def test_merged_status_failed() -> None:
    r = TestMapReduceResult(
        test_node_id="t::t",
        agent_name=AgentName("a"),
        branch_name="mngr-tmr/c",
        tests_passing_before=False,
        tests_passing_after=True,
        changes=SUCCEEDED_FIX,
    )
    integrator = IntegratorResult(failed=("mngr-tmr/c",))
    assert "10007" in _merged_status(r, integrator)


def test_merged_status_not_in_integrator() -> None:
    r = TestMapReduceResult(
        test_node_id="t::t",
        agent_name=AgentName("a"),
        branch_name="mngr-tmr/d",
        tests_passing_before=False,
        tests_passing_after=True,
        changes=SUCCEEDED_FIX,
    )
    integrator = IntegratorResult(squashed_branches=("mngr-tmr/other",))
    assert _merged_status(r, integrator) == ""


# --- HTML report tests ---


def test_build_grouped_tables_groups_by_section() -> None:
    results = [
        make_test_result(before=True, after=True),
        make_test_result(changes=SUCCEEDED_FIX, before=False, after=True),
    ]
    tables_html = _build_grouped_tables(results)
    assert tables_html.index("Non-implementation fixes") < tables_html.index("Clean pass")


def test_build_grouped_tables_shows_branch() -> None:
    r = TestMapReduceResult(
        test_node_id="t::c",
        agent_name=AgentName("c"),
        changes=SUCCEEDED_FIX,
        tests_passing_before=False,
        tests_passing_after=True,
        summary_markdown="fixed",
        branch_name="mngr-tmr/c-abc123",
    )
    assert "mngr-tmr/c-abc123" in _build_grouped_tables([r])


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
    assert "FIX_TEST" in tables_html
    assert "IMPROVE_TEST" in tables_html
    assert "10003" in tables_html
    assert "9644" in tables_html


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


def test_build_grouped_tables_running_first() -> None:
    results = [make_test_result(), make_test_result(before=True, after=True)]
    tables_html = _build_grouped_tables(results)
    assert tables_html.index("Clean pass") < tables_html.index("Running")


def test_build_grouped_tables_has_merged_column() -> None:
    r = make_test_result(before=True, after=True)
    tables_html = _build_grouped_tables([r])
    assert "Merged?" in tables_html


def test_build_grouped_tables_impl_priority_sorting() -> None:
    impl_fix_a = {ChangeKind.FIX_IMPL: Change(status=ChangeStatus.SUCCEEDED, summary_markdown="fix a")}
    impl_fix_b = {ChangeKind.FIX_IMPL: Change(status=ChangeStatus.SUCCEEDED, summary_markdown="fix b")}
    r_a = TestMapReduceResult(
        test_node_id="t::a",
        agent_name=AgentName("a"),
        changes=impl_fix_a,
        tests_passing_before=False,
        tests_passing_after=True,
        branch_name="mngr-tmr/a",
    )
    r_b = TestMapReduceResult(
        test_node_id="t::b",
        agent_name=AgentName("b"),
        changes=impl_fix_b,
        tests_passing_before=False,
        tests_passing_after=True,
        branch_name="mngr-tmr/b",
    )
    integrator = IntegratorResult(impl_priority=("mngr-tmr/b", "mngr-tmr/a"))
    tables_html = _build_grouped_tables([r_a, r_b], integrator=integrator)
    assert tables_html.index("t::b") < tables_html.index("t::a")


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
            branch_name="mngr-tmr/test-fixed",
        ),
    ]
    output_path = tmp_path / "report.html"
    result_path = generate_html_report(results, output_path)
    assert result_path == output_path
    assert output_path.exists()
    content = output_path.read_text()
    assert "Test Map-Reduce Report" in content
    assert "Clean pass" in content
    assert "Non-implementation fixes" in content
    assert 'class="toc-sidebar"' in content


def test_generate_html_report_groups_clean_pass_before_running(tmp_path: Path) -> None:
    results = [
        make_test_result(before=True, after=True),
        make_test_result(changes=FAILED_FIX, before=False, after=False),
    ]
    tables_html = _build_grouped_tables(results)
    assert "Non-implementation fixes" in tables_html
    assert "Clean pass" in tables_html


def test_generate_html_report_creates_parent_dirs(tmp_path: Path) -> None:
    output_path = tmp_path / "subdir" / "nested" / "report.html"
    results = [make_test_result(before=True, after=True)]
    generate_html_report(results, output_path)
    assert output_path.exists()


def test_generate_html_report_all_report_sections(tmp_path: Path) -> None:
    impl_fix = {ChangeKind.FIX_IMPL: Change(status=ChangeStatus.SUCCEEDED, summary_markdown="fixed impl")}
    blocked_changes = {ChangeKind.FIX_TEST: Change(status=ChangeStatus.BLOCKED, summary_markdown="blocked")}
    results = [
        make_test_result(),
        make_test_result(changes=SUCCEEDED_FIX, before=False, after=True),
        make_test_result(changes=impl_fix, before=False, after=True),
        make_test_result(changes=blocked_changes, before=False, after=False),
        make_test_result(errored=True),
        make_test_result(before=True, after=True),
    ]
    output_path = tmp_path / "all_sections.html"
    generate_html_report(results, output_path)
    content = output_path.read_text()
    for sec in ReportSection:
        label = {
            ReportSection.NON_IMPL_FIXES: "Non-implementation fixes",
            ReportSection.IMPL_FIXES: "Implementation fixes",
            ReportSection.BLOCKED: "Blocked",
            ReportSection.CLEAN_PASS: "Clean pass",
            ReportSection.RUNNING: "Running",
        }[sec]
        assert label in content


def test_generate_html_report_empty_results(tmp_path: Path) -> None:
    output_path = tmp_path / "empty.html"
    generate_html_report([], output_path)
    assert "0 test(s)" in output_path.read_text()


def test_generate_html_report_with_integrator(tmp_path: Path) -> None:
    results = [make_test_result(changes=SUCCEEDED_FIX, before=False, after=True)]
    integrator = IntegratorResult(
        agent_name=AgentName("tmr-integrator-abc123"),
        squashed_branches=("mngr-tmr/a",),
        branch_name="mngr-tmr/integrated-abc123",
    )
    output_path = tmp_path / "integrator.html"
    generate_html_report(results, output_path, integrator=integrator)
    content = output_path.read_text()
    assert "Test Map-Reduce Report" in content
    assert "Merged?" in content


def test_generate_html_report_integrator_with_failures(tmp_path: Path) -> None:
    results = [make_test_result(before=True, after=True)]
    integrator = IntegratorResult(
        squashed_branches=("mngr-tmr/a",),
        failed=("mngr-tmr/b",),
        branch_name="mngr-tmr/integrated-abc123",
    )
    output_path = tmp_path / "integrator_partial.html"
    generate_html_report(results, output_path, integrator=integrator)
    assert output_path.exists()


def test_generate_html_report_without_integrator(tmp_path: Path) -> None:
    results = [make_test_result(before=True, after=True)]
    output_path = tmp_path / "no_integrator.html"
    generate_html_report(results, output_path)
    content = output_path.read_text()
    assert "Test Map-Reduce Report" in content


def test_generate_html_report_html_escaped(tmp_path: Path) -> None:
    xss_branch = "<script>alert('xss')</script>"
    results = [
        TestMapReduceResult(
            test_node_id="t::xss",
            agent_name=AgentName("xss-agent"),
            changes=SUCCEEDED_FIX,
            tests_passing_before=False,
            tests_passing_after=True,
            summary_markdown="<img onerror=alert(1)>",
            branch_name=xss_branch,
        )
    ]
    output_path = tmp_path / "escape.html"
    generate_html_report(results, output_path)
    content = output_path.read_text()
    assert "<script>alert" not in content
    assert "&lt;script&gt;" in content
