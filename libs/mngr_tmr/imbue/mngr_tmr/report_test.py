"""Unit tests for the test-mapreduce report module (data types + HTML generation)."""

import json
from pathlib import Path

from imbue.mngr.primitives import AgentName
from imbue.mngr_mapreduce.data_types import AgentKind
from imbue.mngr_mapreduce.data_types import AgentMetadata
from imbue.mngr_tmr.prompts import INTEGRATOR_OUTCOME_FILENAME
from imbue.mngr_tmr.prompts import TESTING_AGENT_OUTCOME_FILENAME
from imbue.mngr_tmr.report import Change
from imbue.mngr_tmr.report import ChangeKind
from imbue.mngr_tmr.report import ChangeStatus
from imbue.mngr_tmr.report import IntegratorResult
from imbue.mngr_tmr.report import ReportSection
from imbue.mngr_tmr.report import TestMapReduceResult
from imbue.mngr_tmr.report import TestResult
from imbue.mngr_tmr.report import _merged_status_html
from imbue.mngr_tmr.report import _render_markdown
from imbue.mngr_tmr.report import _report_section_of
from imbue.mngr_tmr.report import generate_html_report

SUCCEEDED_FIX = {ChangeKind.FIX_TEST: Change(status=ChangeStatus.SUCCEEDED, summary_markdown="fixed")}
FAILED_FIX = {ChangeKind.FIX_TEST: Change(status=ChangeStatus.FAILED, summary_markdown="failed")}


def make_test_result(
    changes: dict[ChangeKind, Change] | None = None,
    errored: bool = False,
    before: bool | None = None,
    after: bool | None = None,
) -> TestMapReduceResult:
    """Build a minimal TestMapReduceResult for testing render-internal helpers."""
    return TestMapReduceResult(
        test_node_id="t::t",
        agent_name=AgentName("a"),
        changes=changes if changes is not None else {},
        errored=errored,
        tests_passing_before=before,
        tests_passing_after=after,
    )


def _serialize_outcome(outcome: TestResult) -> dict[str, object]:
    return {
        "changes": {
            k.value: {"status": v.status.value, "summary_markdown": v.summary_markdown}
            for k, v in outcome.changes.items()
        },
        "errored": outcome.errored,
        "tests_passing_before": outcome.tests_passing_before,
        "tests_passing_after": outcome.tests_passing_after,
        "summary_markdown": outcome.summary_markdown,
        "test_runs": [
            {"run_name": r.run_name, "description_markdown": r.description_markdown} for r in outcome.test_runs
        ],
    }


def _write_test_outcome(output_dir: Path, agent_name: AgentName, outcome: TestResult) -> None:
    target = output_dir / str(agent_name) / "test_output" / TESTING_AGENT_OUTCOME_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(_serialize_outcome(outcome)))


def write_integrator_outcome(output_dir: Path, agent_name: AgentName, payload: dict[str, object]) -> None:
    """Write an integrator outcome JSON where the reporter expects it."""
    target = output_dir / str(agent_name) / "test_output" / INTEGRATOR_OUTCOME_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload))


def make_metadata_and_outcome(
    output_dir: Path,
    agent_name: str,
    *,
    test_node_id: str = "t::t",
    branch_name: str | None = None,
    error_summary: str | None = None,
    changes: dict[ChangeKind, Change] | None = None,
    errored: bool = False,
    tests_passing_before: bool | None = None,
    tests_passing_after: bool | None = None,
    summary_markdown: str = "",
    write_outcome: bool = True,
) -> AgentMetadata:
    """Build an ``AgentMetadata`` and (unless ``write_outcome`` is False) write
    its outcome JSON under ``output_dir/<agent_name>/test_output/``.

    Mirrors what orchestration would emit at runtime: errored agents have
    ``error_summary`` set and no outcome on disk; "running" agents have neither.
    """
    name = AgentName(agent_name)
    metadata = AgentMetadata(
        kind=AgentKind.MAPPER,
        agent_name=name,
        task_id=test_node_id,
        branch_name=branch_name,
        error_summary=error_summary,
    )
    if error_summary is None and write_outcome:
        outcome = TestResult(
            changes=changes if changes is not None else {},
            errored=errored,
            tests_passing_before=tests_passing_before,
            tests_passing_after=tests_passing_after,
            summary_markdown=summary_markdown,
        )
        _write_test_outcome(output_dir, name, outcome)
    return metadata


# --- enum + dataclass smoke tests ---


def test_change_kind_values() -> None:
    assert ChangeKind.IMPROVE_TEST == "IMPROVE_TEST"
    assert ChangeKind.FIX_TEST == "FIX_TEST"
    assert ChangeKind.FIX_IMPL == "FIX_IMPL"
    assert ChangeKind.FIX_TUTORIAL == "FIX_TUTORIAL"


def test_change_status_values() -> None:
    assert ChangeStatus.SUCCEEDED == "SUCCEEDED"
    assert ChangeStatus.FAILED == "FAILED"
    assert ChangeStatus.BLOCKED == "BLOCKED"


def test_report_section_values() -> None:
    assert ReportSection.NON_IMPL_FIXES == "NON_IMPL_FIXES"
    assert ReportSection.IMPL_FIXES == "IMPL_FIXES"
    assert ReportSection.BLOCKED == "BLOCKED"
    assert ReportSection.CLEAN_PASS == "CLEAN_PASS"
    assert ReportSection.RUNNING == "RUNNING"


def test_test_result_empty() -> None:
    result = TestResult(tests_passing_before=True, tests_passing_after=True, summary_markdown="All good")
    assert result.changes == {}
    assert result.errored is False


def test_test_result_with_changes() -> None:
    changes = {
        ChangeKind.FIX_TEST: Change(status=ChangeStatus.SUCCEEDED, summary_markdown="Fixed"),
        ChangeKind.IMPROVE_TEST: Change(status=ChangeStatus.BLOCKED, summary_markdown="Needs work"),
    }
    result = TestResult(changes=changes, tests_passing_before=False, tests_passing_after=True)
    assert len(result.changes) == 2


def test_test_map_reduce_result_with_branch() -> None:
    result = TestMapReduceResult(
        test_node_id="tests/test_foo.py::test_baz",
        agent_name=AgentName("tmr-test-baz"),
        changes={ChangeKind.FIX_IMPL: Change(status=ChangeStatus.SUCCEEDED, summary_markdown="Fixed null check")},
        tests_passing_before=False,
        tests_passing_after=True,
        summary_markdown="Fixed missing null check",
        branch_name="tmr/20260101000000/test-baz",
    )
    assert result.branch_name == "tmr/20260101000000/test-baz"


def test_test_map_reduce_result_without_branch() -> None:
    result = TestMapReduceResult(
        test_node_id="tests/test_foo.py::test_ok",
        agent_name=AgentName("tmr-test-ok"),
        tests_passing_before=True,
        tests_passing_after=True,
    )
    assert result.branch_name is None


# --- report_section_of tests ---


def test_report_section_errored() -> None:
    assert _report_section_of(make_test_result(errored=True)) == ReportSection.FAILED


def test_report_section_running() -> None:
    assert _report_section_of(make_test_result()) == ReportSection.RUNNING


def test_report_section_clean_pass() -> None:
    assert _report_section_of(make_test_result(before=True, after=True)) == ReportSection.CLEAN_PASS


def test_report_section_non_impl_fixes() -> None:
    assert (
        _report_section_of(make_test_result(changes=SUCCEEDED_FIX, before=False, after=True))
        == ReportSection.NON_IMPL_FIXES
    )


def test_report_section_impl_fixes() -> None:
    impl_fix = {ChangeKind.FIX_IMPL: Change(status=ChangeStatus.SUCCEEDED, summary_markdown="fixed")}
    assert _report_section_of(make_test_result(changes=impl_fix, before=False, after=True)) == ReportSection.IMPL_FIXES


def test_report_section_blocked_all_changes_blocked() -> None:
    blocked_changes = {ChangeKind.FIX_TEST: Change(status=ChangeStatus.BLOCKED, summary_markdown="blocked")}
    assert (
        _report_section_of(make_test_result(changes=blocked_changes, before=False, after=False))
        == ReportSection.BLOCKED
    )


def test_report_section_failed_changes_are_non_impl() -> None:
    """FAILED (not BLOCKED) changes route to NON_IMPL_FIXES, not BLOCKED."""
    assert (
        _report_section_of(make_test_result(changes=FAILED_FIX, before=False, after=False))
        == ReportSection.NON_IMPL_FIXES
    )


def test_report_section_blocked_no_changes_tests_failing() -> None:
    assert _report_section_of(make_test_result(before=False, after=False)) == ReportSection.BLOCKED


# --- render_markdown tests ---


def test_render_markdown_bold() -> None:
    result = _render_markdown("**bold**")
    assert "<strong>bold</strong>" in result


def test_render_markdown_plain_text() -> None:
    result = _render_markdown("plain text")
    assert "plain text" in result


# --- _merged_status tests ---


def test_merged_status_no_integrator() -> None:
    r = make_test_result(before=True, after=True)
    assert _merged_status_html(r, None) == ""


def test_merged_status_no_branch() -> None:
    r = make_test_result(before=True, after=True)
    integrator = IntegratorResult(squashed_branches=("mngr-tmr/a",))
    assert _merged_status_html(r, integrator) == ""


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
    assert "10003" in _merged_status_html(r, integrator)


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
    status = _merged_status_html(r, integrator)
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
    assert "10007" in _merged_status_html(r, integrator)


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
    assert _merged_status_html(r, integrator) == ""


# --- HTML report tests ---


def test_generate_html_report(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    agents = [
        make_metadata_and_outcome(
            output_dir,
            "tmr-test-pass",
            test_node_id="tests/test_a.py::test_pass",
            tests_passing_before=True,
            tests_passing_after=True,
            summary_markdown="Passed immediately",
        ),
        make_metadata_and_outcome(
            output_dir,
            "tmr-test-fixed",
            test_node_id="tests/test_b.py::test_fixed",
            branch_name="mngr-tmr/test-fixed",
            changes=SUCCEEDED_FIX,
            tests_passing_before=False,
            tests_passing_after=True,
            summary_markdown="Fixed missing import",
        ),
    ]
    result_path = generate_html_report(agents, output_dir)
    assert result_path == output_dir / "index.html"
    assert result_path.exists()
    content = result_path.read_text()
    assert "Test Map-Reduce Report" in content
    assert "Clean pass" in content
    assert "Non-implementation fixes" in content
    assert 'class="toc-sidebar"' in content


def test_generate_html_report_creates_output_dir(tmp_path: Path) -> None:
    output_dir = tmp_path / "subdir" / "nested"
    agents = [make_metadata_and_outcome(output_dir, "a", tests_passing_before=True, tests_passing_after=True)]
    generate_html_report(agents, output_dir)
    assert (output_dir / "index.html").exists()


def test_generate_html_report_all_report_sections(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    impl_fix = {ChangeKind.FIX_IMPL: Change(status=ChangeStatus.SUCCEEDED, summary_markdown="fixed impl")}
    blocked_changes = {ChangeKind.FIX_TEST: Change(status=ChangeStatus.BLOCKED, summary_markdown="blocked")}
    agents = [
        make_metadata_and_outcome(output_dir, "running-agent", write_outcome=False),
        make_metadata_and_outcome(
            output_dir, "non-impl", changes=SUCCEEDED_FIX, tests_passing_before=False, tests_passing_after=True
        ),
        make_metadata_and_outcome(
            output_dir, "impl-fix", changes=impl_fix, tests_passing_before=False, tests_passing_after=True
        ),
        make_metadata_and_outcome(
            output_dir, "blocked", changes=blocked_changes, tests_passing_before=False, tests_passing_after=False
        ),
        make_metadata_and_outcome(output_dir, "failed", error_summary="boom"),
        make_metadata_and_outcome(output_dir, "clean", tests_passing_before=True, tests_passing_after=True),
    ]
    result_path = generate_html_report(agents, output_dir)
    content = result_path.read_text()
    for sec in ReportSection:
        label = {
            ReportSection.NON_IMPL_FIXES: "Non-implementation fixes",
            ReportSection.IMPL_FIXES: "Implementation fixes",
            ReportSection.BLOCKED: "Blocked",
            ReportSection.FAILED: "Failed",
            ReportSection.CLEAN_PASS: "Clean pass",
            ReportSection.RUNNING: "Running",
        }[sec]
        assert label in content


def test_generate_html_report_empty_agents(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    result_path = generate_html_report([], output_dir)
    assert "0 test(s)" in result_path.read_text()


def test_generate_html_report_with_integrator(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    agents = [
        make_metadata_and_outcome(
            output_dir,
            "agent-a",
            branch_name="mngr-tmr/a",
            changes=SUCCEEDED_FIX,
            tests_passing_before=False,
            tests_passing_after=True,
        ),
    ]
    integrator_meta = AgentMetadata(
        kind=AgentKind.REDUCER,
        agent_name=AgentName("tmr-integrator-abc123"),
        branch_name="mngr-tmr/integrated-abc123",
    )
    write_integrator_outcome(
        output_dir,
        integrator_meta.agent_name,
        {"squashed_branches": ["mngr-tmr/a"], "squashed_commit_hash": "abc", "impl_priority": [], "failed": []},
    )
    result_path = generate_html_report(agents, output_dir, integrator_metadata=integrator_meta)
    content = result_path.read_text()
    assert "Test Map-Reduce Report" in content
    assert "Merged?" in content


def test_generate_html_report_integrator_with_failures(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    agents = [make_metadata_and_outcome(output_dir, "a", tests_passing_before=True, tests_passing_after=True)]
    integrator_meta = AgentMetadata(
        kind=AgentKind.REDUCER,
        agent_name=AgentName("tmr-integrator-abc123"),
        branch_name="mngr-tmr/integrated-abc123",
    )
    write_integrator_outcome(
        output_dir,
        integrator_meta.agent_name,
        {"squashed_branches": ["mngr-tmr/a"], "failed": ["mngr-tmr/b"]},
    )
    result_path = generate_html_report(agents, output_dir, integrator_metadata=integrator_meta)
    assert result_path.exists()


def test_generate_html_report_without_integrator(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    agents = [make_metadata_and_outcome(output_dir, "a", tests_passing_before=True, tests_passing_after=True)]
    result_path = generate_html_report(agents, output_dir)
    assert "Test Map-Reduce Report" in result_path.read_text()


def test_generate_html_report_renders_escalations_and_normalizations(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    agents = [make_metadata_and_outcome(output_dir, "a", tests_passing_before=True, tests_passing_after=True)]
    integrator_meta = AgentMetadata(
        kind=AgentKind.REDUCER,
        agent_name=AgentName("tmr-integrator-esc-render"),
        branch_name="mngr-tmr/integrated-esc-render",
    )
    write_integrator_outcome(
        output_dir,
        integrator_meta.agent_name,
        {
            "squashed_branches": ["mngr-tmr/a"],
            "normalizations": [{"summary_markdown": "Extracted **assert_agent_running** helper"}],
            "escalations": [{"title": "codex needs OpenAI creds", "detail_markdown": "Provide a fake-codex fixture"}],
        },
    )
    result_path = generate_html_report(agents, output_dir, integrator_metadata=integrator_meta)
    content = result_path.read_text()
    assert "Escalations (1)" in content
    assert "codex needs OpenAI creds" in content
    assert "Provide a fake-codex fixture" in content
    assert "Suite normalizations (1)" in content
    # Markdown in the normalization summary is rendered to HTML.
    assert "<strong>assert_agent_running</strong>" in content


def test_generate_html_report_no_escalations_section_when_empty(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    agents = [make_metadata_and_outcome(output_dir, "a", tests_passing_before=True, tests_passing_after=True)]
    integrator_meta = AgentMetadata(
        kind=AgentKind.REDUCER,
        agent_name=AgentName("tmr-integrator-esc-empty"),
        branch_name="mngr-tmr/integrated-esc-empty",
    )
    write_integrator_outcome(output_dir, integrator_meta.agent_name, {"squashed_branches": ["mngr-tmr/a"]})
    content = generate_html_report(agents, output_dir, integrator_metadata=integrator_meta).read_text()
    assert "Escalations" not in content
    assert "Suite normalizations" not in content


def test_generate_html_report_escalation_title_escaped(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    agents = [make_metadata_and_outcome(output_dir, "a", tests_passing_before=True, tests_passing_after=True)]
    integrator_meta = AgentMetadata(
        kind=AgentKind.REDUCER,
        agent_name=AgentName("tmr-integrator-esc-xss"),
        branch_name=None,
    )
    write_integrator_outcome(
        output_dir,
        integrator_meta.agent_name,
        {"escalations": [{"title": "<script>alert('xss')</script>", "detail_markdown": "x"}]},
    )
    content = generate_html_report(agents, output_dir, integrator_metadata=integrator_meta).read_text()
    assert "<script>alert" not in content
    assert "&lt;script&gt;" in content


def test_generate_html_report_html_escaped(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    xss_branch = "<script>alert('xss')</script>"
    agents = [
        make_metadata_and_outcome(
            output_dir,
            "xss-agent",
            test_node_id="t::xss",
            branch_name=xss_branch,
            changes=SUCCEEDED_FIX,
            tests_passing_before=False,
            tests_passing_after=True,
            summary_markdown="<img onerror=alert(1)>",
        )
    ]
    result_path = generate_html_report(agents, output_dir)
    content = result_path.read_text()
    assert "<script>alert" not in content
    assert "&lt;script&gt;" in content
