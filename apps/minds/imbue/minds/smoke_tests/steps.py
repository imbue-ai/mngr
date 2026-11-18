"""Named steps of a smoke scenario, recorded as they happen so the run's report can say how far it got.

A scenario wraps each of its steps in :func:`recorded_step`, which appends one
line to the scenario's steps file when the step starts and another when its
block returns. Nothing is caught: a step that raises, or a process that dies
during one, leaves a start line with no completion after it, which is how the
reader tells an unfinished step from a completed one.
"""

from collections.abc import Generator
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final
from typing import assert_never

from loguru import logger
from pydantic import ValidationError

from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.smoke_tests.data_types import SmokeStepEvent
from imbue.minds.smoke_tests.data_types import SmokeStepEventKind
from imbue.minds.smoke_tests.data_types import SmokeStepOutcome
from imbue.minds.smoke_tests.data_types import SmokeStepRecord

# Beside the scenario's screenshots, under its output directory.
STEPS_FILE_NAME: Final[str] = "steps.jsonl"


@contextmanager
def recorded_step(steps_path: Path, label: str) -> Generator[None, None, None]:
    """Run the block as the step ``label``: its start is recorded on entry, its completion when the block returns."""
    _append_step_event(steps_path, label, SmokeStepEventKind.STARTED, detail="")
    yield
    _append_step_event(steps_path, label, SmokeStepEventKind.COMPLETED, detail="")


def record_skipped_step(steps_path: Path, label: str, reason: str) -> None:
    """Record that the scenario skipped the step ``label``, and why."""
    _append_step_event(steps_path, label, SmokeStepEventKind.SKIPPED, detail=reason)


def read_step_records(steps_path: Path) -> tuple[SmokeStepRecord, ...]:
    """Every step the steps file records, in the order they started; nothing when the scenario recorded none."""
    if not steps_path.is_file():
        return ()
    return step_records_from_events(parse_step_events(steps_path.read_text(encoding="utf-8")))


def parse_step_events(steps_text: str) -> tuple[SmokeStepEvent, ...]:
    """Parse a steps file's lines, skipping (with a warning) any that is not a step event.

    A line can be cut short by a process killed while writing it; the steps
    around it still say how far the scenario got.
    """
    events: list[SmokeStepEvent] = []
    for line_number, raw_line in enumerate(steps_text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            events.append(SmokeStepEvent.model_validate_json(line))
        except ValidationError as exc:
            logger.warning(
                "Skipped steps file line {} that is not a step event: {!r} ({})", line_number, line[:200], exc
            )
    return tuple(events)


def step_records_from_events(events: Sequence[SmokeStepEvent]) -> tuple[SmokeStepRecord, ...]:
    """Pair each step's start with its completion; a start with none after it is an unfinished step."""
    records: list[SmokeStepRecord] = []
    open_record_idx_by_label: dict[str, int] = {}
    for event in events:
        match event.kind:
            case SmokeStepEventKind.STARTED:
                open_record_idx_by_label[str(event.label)] = len(records)
                records.append(
                    SmokeStepRecord(
                        label=event.label,
                        outcome=SmokeStepOutcome.UNFINISHED,
                        started_at=event.recorded_at,
                        finished_at=None,
                        detail="",
                    )
                )
            case SmokeStepEventKind.COMPLETED:
                record_idx = open_record_idx_by_label.pop(str(event.label), None)
                if record_idx is None:
                    logger.warning("Skipped the completion of step {!r}, whose start is not recorded", event.label)
                    continue
                records[record_idx] = records[record_idx].model_copy_update(
                    to_update(records[record_idx].field_ref().outcome, SmokeStepOutcome.COMPLETED),
                    to_update(records[record_idx].field_ref().finished_at, event.recorded_at),
                )
            case SmokeStepEventKind.SKIPPED:
                records.append(
                    SmokeStepRecord(
                        label=event.label,
                        outcome=SmokeStepOutcome.SKIPPED,
                        started_at=event.recorded_at,
                        finished_at=event.recorded_at,
                        detail=event.detail,
                    )
                )
            case _ as unreachable:
                assert_never(unreachable)
    return tuple(records)


def _append_step_event(steps_path: Path, label: str, kind: SmokeStepEventKind, *, detail: str) -> None:
    event = SmokeStepEvent(label=NonEmptyStr(label), kind=kind, recorded_at=datetime.now(timezone.utc), detail=detail)
    steps_path.parent.mkdir(parents=True, exist_ok=True)
    with steps_path.open("a", encoding="utf-8") as steps_file:
        steps_file.write(event.model_dump_json() + "\n")
