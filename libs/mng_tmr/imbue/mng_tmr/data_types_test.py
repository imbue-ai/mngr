"""Unit tests for test-mapreduce data types."""

from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentName
from imbue.mng_tmr.data_types import Change
from imbue.mng_tmr.data_types import ChangeKind
from imbue.mng_tmr.data_types import ChangeStatus
from imbue.mng_tmr.data_types import ReportSection
from imbue.mng_tmr.data_types import TestAgentInfo
from imbue.mng_tmr.data_types import TestMapReduceResult
from imbue.mng_tmr.data_types import TestResult


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


def test_change_construction() -> None:
    change = Change(status=ChangeStatus.SUCCEEDED, summary_markdown="Fixed assertion")
    assert change.status == ChangeStatus.SUCCEEDED
    assert change.summary_markdown == "Fixed assertion"


def test_test_result_empty() -> None:
    result = TestResult(tests_passing_before=True, tests_passing_after=True, summary_markdown="All good")
    assert result.changes == {}
    assert result.errored is False
    assert result.tests_passing_before is True
    assert result.tests_passing_after is True


def test_test_result_with_changes() -> None:
    changes = {
        ChangeKind.FIX_TEST: Change(status=ChangeStatus.SUCCEEDED, summary_markdown="Fixed"),
        ChangeKind.IMPROVE_TEST: Change(status=ChangeStatus.BLOCKED, summary_markdown="Needs work"),
    }
    result = TestResult(
        changes=changes,
        tests_passing_before=False,
        tests_passing_after=True,
        summary_markdown="Fixed test",
    )
    assert len(result.changes) == 2
    assert ChangeKind.FIX_TEST in result.changes


def test_test_result_from_json_compatible_dict() -> None:
    raw_changes = {"FIX_IMPL": {"status": "SUCCEEDED", "summary_markdown": "Fixed bug"}}
    changes = {
        ChangeKind(kind_str): Change(
            status=ChangeStatus(entry["status"]),
            summary_markdown=entry["summary_markdown"],
        )
        for kind_str, entry in raw_changes.items()
    }
    result = TestResult(
        changes=changes,
        errored=False,
        tests_passing_before=False,
        tests_passing_after=True,
        summary_markdown="Fixed implementation bug",
    )
    assert ChangeKind.FIX_IMPL in result.changes
    assert result.tests_passing_after is True


def test_test_agent_info_construction() -> None:
    info = TestAgentInfo(
        test_node_id="tests/test_foo.py::test_bar",
        agent_id=AgentId.generate(),
        agent_name=AgentName("tmr-test-bar"),
        created_at=0.0,
    )
    assert info.test_node_id == "tests/test_foo.py::test_bar"
    assert str(info.agent_name) == "tmr-test-bar"


def test_test_map_reduce_result_with_branch() -> None:
    result = TestMapReduceResult(
        test_node_id="tests/test_foo.py::test_baz",
        agent_name=AgentName("tmr-test-baz"),
        changes={ChangeKind.FIX_IMPL: Change(status=ChangeStatus.SUCCEEDED, summary_markdown="Fixed null check")},
        tests_passing_before=False,
        tests_passing_after=True,
        summary_markdown="Fixed missing null check",
        branch_name="mng-tmr/test-baz",
    )
    assert result.branch_name == "mng-tmr/test-baz"
    assert len(result.changes) == 1


def test_test_map_reduce_result_without_branch() -> None:
    result = TestMapReduceResult(
        test_node_id="tests/test_foo.py::test_ok",
        agent_name=AgentName("tmr-test-ok"),
        tests_passing_before=True,
        tests_passing_after=True,
        summary_markdown="Test passed on first run",
    )
    assert result.branch_name is None
    assert result.changes == {}
