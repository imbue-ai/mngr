"""Shared test utilities for mngr-test-mapreduce tests."""

from imbue.mngr.primitives import AgentName
from imbue.mngr_tmr.data_types import Change
from imbue.mngr_tmr.data_types import ChangeKind
from imbue.mngr_tmr.data_types import ChangeStatus
from imbue.mngr_tmr.data_types import TestMapReduceResult

SUCCEEDED_FIX = {ChangeKind.FIX_TEST: Change(status=ChangeStatus.SUCCEEDED, summary_markdown="fixed")}
FAILED_FIX = {ChangeKind.FIX_TEST: Change(status=ChangeStatus.FAILED, summary_markdown="failed")}
BLOCKED_FIX = {ChangeKind.FIX_IMPL: Change(status=ChangeStatus.BLOCKED, summary_markdown="blocked")}


def make_test_result(
    changes: dict[ChangeKind, Change] | None = None,
    errored: bool = False,
    before: bool | None = None,
    after: bool | None = None,
) -> TestMapReduceResult:
    """Build a minimal TestMapReduceResult for testing."""
    return TestMapReduceResult(
        test_node_id="t::t",
        agent_name=AgentName("a"),
        changes=changes if changes is not None else {},
        errored=errored,
        tests_passing_before=before,
        tests_passing_after=after,
    )
