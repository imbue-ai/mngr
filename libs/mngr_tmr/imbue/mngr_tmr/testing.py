"""Shared test utilities for mngr-test-mapreduce tests."""

import json
from pathlib import Path

from imbue.mngr.primitives import AgentName
from imbue.mngr_tmr.data_types import AgentKind
from imbue.mngr_tmr.data_types import AgentMetadata
from imbue.mngr_tmr.data_types import Change
from imbue.mngr_tmr.data_types import ChangeKind
from imbue.mngr_tmr.data_types import ChangeStatus
from imbue.mngr_tmr.data_types import TestMapReduceResult
from imbue.mngr_tmr.data_types import TestResult
from imbue.mngr_tmr.prompts import INTEGRATOR_OUTCOME_FILENAME
from imbue.mngr_tmr.prompts import TESTING_AGENT_OUTCOME_FILENAME

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


def write_test_outcome(output_dir: Path, agent_name: AgentName, outcome: TestResult) -> None:
    """Write a testing-agent outcome JSON where the reporter expects it."""
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
    ``error_summary`` set and no outcome on disk; "running" agents have
    neither.
    """
    name = AgentName(agent_name)
    metadata = AgentMetadata(
        kind=AgentKind.TESTING_AGENT,
        agent_name=name,
        test_node_id=test_node_id,
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
        write_test_outcome(output_dir, name, outcome)
    return metadata
