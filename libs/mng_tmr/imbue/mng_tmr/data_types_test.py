"""Unit tests for test-mapreduce data types."""

from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentName
from imbue.mng_tmr.data_types import Change
from imbue.mng_tmr.data_types import ChangeKind
from imbue.mng_tmr.data_types import ChangeStatus
from imbue.mng_tmr.data_types import DisplayCategory
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


def test_display_category_values() -> None:
    assert DisplayCategory.PENDING == "PENDING"
    assert DisplayCategory.FIXED == "FIXED"
    assert DisplayCategory.CLEAN_PASS == "CLEAN_PASS"
    assert DisplayCategory.ERRORED == "ERRORED"


def test_change_construction() -> None:
    change = Change(kind=ChangeKind.FIX_TEST, status=ChangeStatus.SUCCEEDED, summary="Fixed assertion")
    assert change.kind == ChangeKind.FIX_TEST
    assert change.status == ChangeStatus.SUCCEEDED
    assert change.summary == "Fixed assertion"


def test_test_result_empty() -> None:
    result = TestResult(tests_passing_before=True, tests_passing_after=True, summary="All good")
    assert result.changes == ()
    assert result.errored is False
    assert result.tests_passing_before is True
    assert result.tests_passing_after is True


def test_test_result_with_changes() -> None:
    changes = (
        Change(kind=ChangeKind.FIX_TEST, status=ChangeStatus.SUCCEEDED, summary="Fixed"),
        Change(kind=ChangeKind.IMPROVE_TEST, status=ChangeStatus.BLOCKED, summary="Needs work"),
    )
    result = TestResult(
        changes=changes,
        tests_passing_before=False,
        tests_passing_after=True,
        summary="Fixed test",
    )
    assert len(result.changes) == 2
    assert result.changes[0].kind == ChangeKind.FIX_TEST


def test_test_result_from_json_compatible_dict() -> None:
    raw_changes = [{"kind": "FIX_IMPL", "status": "SUCCEEDED", "summary": "Fixed bug"}]
    changes = tuple(
        Change(kind=ChangeKind(c["kind"]), status=ChangeStatus(c["status"]), summary=c["summary"]) for c in raw_changes
    )
    result = TestResult(
        changes=changes,
        errored=False,
        tests_passing_before=False,
        tests_passing_after=True,
        summary="Fixed implementation bug",
    )
    assert result.changes[0].kind == ChangeKind.FIX_IMPL
    assert result.tests_passing_after is True


def test_test_agent_info_construction() -> None:
    info = TestAgentInfo(
        test_node_id="tests/test_foo.py::test_bar",
        agent_id=AgentId.generate(),
        agent_name=AgentName("tmr-test-bar"),
    )
    assert info.test_node_id == "tests/test_foo.py::test_bar"
    assert str(info.agent_name) == "tmr-test-bar"


def test_test_map_reduce_result_with_branch() -> None:
    result = TestMapReduceResult(
        test_node_id="tests/test_foo.py::test_baz",
        agent_name=AgentName("tmr-test-baz"),
        changes=(Change(kind=ChangeKind.FIX_IMPL, status=ChangeStatus.SUCCEEDED, summary="Fixed null check"),),
        tests_passing_before=False,
        tests_passing_after=True,
        summary="Fixed missing null check",
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
        summary="Test passed on first run",
    )
    assert result.branch_name is None
    assert result.changes == ()
