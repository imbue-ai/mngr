from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.smoke_tests.data_types import SmokeStepEvent
from imbue.minds.smoke_tests.data_types import SmokeStepEventKind
from imbue.minds.smoke_tests.data_types import SmokeStepOutcome
from imbue.minds.smoke_tests.steps import parse_step_events
from imbue.minds.smoke_tests.steps import read_step_records
from imbue.minds.smoke_tests.steps import record_skipped_step
from imbue.minds.smoke_tests.steps import recorded_step
from imbue.minds.smoke_tests.steps import step_records_from_events


def test_a_step_whose_block_returns_is_recorded_as_completed(tmp_path: Path) -> None:
    steps_path = tmp_path / "steps.jsonl"

    with recorded_step(steps_path, "Step 1: launch the app 30517"):
        pass

    records = read_step_records(steps_path)
    assert [(record.label, record.outcome) for record in records] == [
        ("Step 1: launch the app 30517", SmokeStepOutcome.COMPLETED)
    ]
    assert records[0].finished_at is not None
    assert records[0].finished_at >= records[0].started_at


def test_a_step_whose_block_raises_is_unfinished_and_the_error_still_propagates(tmp_path: Path) -> None:
    steps_path = tmp_path / "steps.jsonl"

    with recorded_step(steps_path, "Step 1: launch the app"):
        pass
    with pytest.raises(AssertionError, match="share link 61209"):
        with recorded_step(steps_path, "Step 2: open the share"):
            raise AssertionError("share link 61209 did not answer")

    records = read_step_records(steps_path)
    assert [(str(record.label), record.outcome, record.finished_at is None) for record in records] == [
        ("Step 1: launch the app", SmokeStepOutcome.COMPLETED, False),
        ("Step 2: open the share", SmokeStepOutcome.UNFINISHED, True),
    ]


def test_a_skipped_step_is_recorded_with_its_reason(tmp_path: Path) -> None:
    steps_path = tmp_path / "steps.jsonl"

    record_skipped_step(
        steps_path, "Step 3b: run the template's CI suites", "MINDS_SMOKE_SKIP_DWT_TESTS=1 skips 44021"
    )

    records = read_step_records(steps_path)
    assert [(str(record.label), record.outcome, record.detail) for record in records] == [
        ("Step 3b: run the template's CI suites", SmokeStepOutcome.SKIPPED, "MINDS_SMOKE_SKIP_DWT_TESTS=1 skips 44021")
    ]
    assert records[0].finished_at == records[0].started_at


def test_a_line_that_is_not_a_step_event_is_skipped_and_the_rest_are_read(tmp_path: Path) -> None:
    steps_path = tmp_path / "steps.jsonl"
    with recorded_step(steps_path, "Step 1: launch the app"):
        pass
    with steps_path.open("a") as steps_file:
        steps_file.write('{"truncated by a killed process\n')
    record_skipped_step(steps_path, "Step 3b: run the template's CI suites", "skipped 70114")

    records = read_step_records(steps_path)

    assert [record.outcome for record in records] == [SmokeStepOutcome.COMPLETED, SmokeStepOutcome.SKIPPED]


def test_a_steps_file_the_scenario_never_wrote_reads_as_no_steps(tmp_path: Path) -> None:
    assert read_step_records(tmp_path / "absent.jsonl") == ()


def test_a_completion_whose_start_line_was_lost_is_skipped() -> None:
    lost_start = '{"label": "Step 2: sign in", "kind": "STAR'
    completion = SmokeStepEvent(
        label=NonEmptyStr("Step 2: sign in"),
        kind=SmokeStepEventKind.COMPLETED,
        recorded_at=datetime(2026, 11, 16, 9, 30, tzinfo=timezone.utc),
        detail="",
    ).model_dump_json()

    assert step_records_from_events(parse_step_events(f"{lost_start}\n{completion}\n")) == ()
