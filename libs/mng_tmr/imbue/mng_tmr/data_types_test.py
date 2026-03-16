"""Unit tests for test-mapreduce data types."""

from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentName
from imbue.mng_tmr.data_types import TestAgentInfo
from imbue.mng_tmr.data_types import TestMapReduceResult
from imbue.mng_tmr.data_types import TestOutcome
from imbue.mng_tmr.data_types import TestResult


def test_test_outcome_values() -> None:
    assert TestOutcome.PENDING == "PENDING"
    assert TestOutcome.RUN_SUCCEEDED == "RUN_SUCCEEDED"
    assert TestOutcome.FIX_TEST_SUCCEEDED == "FIX_TEST_SUCCEEDED"
    assert TestOutcome.FIX_IMPL_FAILED == "FIX_IMPL_FAILED"
    assert TestOutcome.AGENT_ERROR == "AGENT_ERROR"


def test_test_result_construction() -> None:
    result = TestResult(outcome=TestOutcome.RUN_SUCCEEDED, summary="Test passed")
    assert result.outcome == TestOutcome.RUN_SUCCEEDED
    assert result.summary == "Test passed"


def test_test_result_from_json_compatible_dict() -> None:
    data = {"outcome": "FIX_UNCERTAIN", "summary": "Could not determine cause"}
    result = TestResult(outcome=TestOutcome(data["outcome"]), summary=data["summary"])
    assert result.outcome == TestOutcome.FIX_UNCERTAIN


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
        outcome=TestOutcome.FIX_IMPL_SUCCEEDED,
        summary="Fixed missing null check",
        branch_name="mng-tmr/test-baz",
    )
    assert result.branch_name == "mng-tmr/test-baz"


def test_test_map_reduce_result_without_branch() -> None:
    result = TestMapReduceResult(
        test_node_id="tests/test_foo.py::test_ok",
        agent_name=AgentName("tmr-test-ok"),
        outcome=TestOutcome.RUN_SUCCEEDED,
        summary="Test passed on first run",
    )
    assert result.branch_name is None
