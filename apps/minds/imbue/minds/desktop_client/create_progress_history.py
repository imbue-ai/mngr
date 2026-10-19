"""The durations the progress bars learn from: shipped seeds plus what this device has measured.

Every completed step of a create or a connection setup appends one event to an
append-only file under the data directory. The next operation of the same kind
weighs its bar by the median of the last few measurements per step, so the bar
calibrates itself to this machine and this network after the first run. The
shipped defaults (``create_progress_defaults.toml``, beside this module) only
seed a step the device has never measured.
"""

import statistics
import threading
import tomllib
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final
from uuid import uuid4

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError

from imbue.imbue_common.event_envelope import EventEnvelope
from imbue.imbue_common.event_envelope import EventId
from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import EventType
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import format_nanosecond_iso_timestamp
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.create_progress import OperationProgressTracker
from imbue.minds.desktop_client.create_progress import StepDuration
from imbue.minds.errors import ProgressSeedError
from imbue.minds.primitives import LaunchMode

PROGRESS_HISTORY_EVENT_SOURCE: Final[EventSource] = EventSource("create_progress")
PROGRESS_HISTORY_EVENT_TYPE: Final[EventType] = EventType("step_completed")
PROGRESS_HISTORY_SKIPPED_EVENT_TYPE: Final[EventType] = EventType("step_skipped")
# The events file, relative to the installation's data directory.
PROGRESS_HISTORY_RELATIVE_PATH: Final[Path] = Path("events") / "create_progress" / "events.jsonl"
# How many recent measurements of a step the expectation is the median of.
MEASUREMENTS_PER_STEP: Final[int] = 5
# Only this many of the newest lines are parsed: the last few measurements per step are all that matter.
_HISTORY_TAIL_LINES: Final[int] = 5000

SHIPPED_DEFAULTS_PATH: Final[Path] = Path(__file__).with_name("create_progress_defaults.toml")
_DEFAULT_STEP_SECONDS_KEY: Final[str] = "default_step_seconds"


class StepDurationEvent(EventEnvelope):
    """One step's outcome, as the history file records it: its duration, or that it was skipped."""

    scope: str = Field(description="The operation kind and target the step ran in, such as 'create:DOCKER'")
    step_id: str = Field(description="The step's id")
    duration_seconds: float | None = Field(
        default=None,
        description="Wall-clock seconds the step took; None for a skipped step (a cached build stage), which measured nothing",
    )


class ExpectedDurations(FrozenModel):
    """The expected seconds per step id for one scope, with the fallback for steps it does not name."""

    seconds_by_step_id: Mapping[str, float] = Field(description="Expected seconds per step id")
    default_seconds: float = Field(description="Expected seconds for a step the scope has never seen")

    def new_tracker(self) -> OperationProgressTracker:
        """A tracker that weighs its bar by these expectations."""
        return OperationProgressTracker(
            expected_seconds_by_step_id=self.seconds_by_step_id, default_expected_seconds=self.default_seconds
        )


@pure
def create_scope(launch_mode: LaunchMode) -> str:
    """The history scope of a workspace create on ``launch_mode``."""
    return f"create:{launch_mode.value}"


@pure
def connection_scope(backend_name: str) -> str:
    """The history scope of a connection setup for ``backend_name`` (``aws``, ``gcp``, ...)."""
    return f"connection:{backend_name.lower()}"


class ShippedProgressDefaults(FrozenModel):
    """The seed durations shipped with the app, per scope."""

    default_step_seconds: float = Field(description="Seed for any step no scope names")
    seconds_by_step_id_by_scope: Mapping[str, Mapping[str, float]] = Field(description="Seeds per scope and step id")

    def for_scope(self, scope: str) -> ExpectedDurations:
        return ExpectedDurations(
            seconds_by_step_id=dict(self.seconds_by_step_id_by_scope.get(scope, {})),
            default_seconds=self.default_step_seconds,
        )


@pure
def parse_shipped_defaults(toml_text: str) -> ShippedProgressDefaults:
    """Raises ``ProgressSeedError`` when the table is malformed: a bad seed file is a build error, not a fallback."""
    parsed = tomllib.loads(toml_text)
    default_seconds = parsed.get(_DEFAULT_STEP_SECONDS_KEY)
    if not isinstance(default_seconds, (int, float)) or default_seconds <= 0:
        raise ProgressSeedError(f"{_DEFAULT_STEP_SECONDS_KEY} must be a positive number")
    by_scope: dict[str, dict[str, float]] = {}
    for scope, table in parsed.items():
        if scope == _DEFAULT_STEP_SECONDS_KEY:
            continue
        if not isinstance(table, dict):
            raise ProgressSeedError(f"scope {scope!r} must be a table of step seconds")
        seconds_by_step: dict[str, float] = {}
        for step_id, seconds in table.items():
            if not isinstance(seconds, (int, float)) or seconds <= 0:
                raise ProgressSeedError(f"{scope}.{step_id} must be a positive number")
            seconds_by_step[step_id] = float(seconds)
        by_scope[scope] = seconds_by_step
    return ShippedProgressDefaults(default_step_seconds=float(default_seconds), seconds_by_step_id_by_scope=by_scope)


def load_shipped_defaults() -> ShippedProgressDefaults:
    return parse_shipped_defaults(SHIPPED_DEFAULTS_PATH.read_text())


@pure
def format_shipped_defaults(defaults: ShippedProgressDefaults) -> str:
    """The seed table as the committed TOML file spells it, scopes and steps sorted so diffs are stable."""
    lines = [
        "# Seed durations for the create and connection progress bars, in seconds per step.",
        "# A device replaces these with its own measurements after its first run; this file",
        "# only decides the first bar it draws. Regenerated by scripts/sync_create_progress_defaults.py",
        "# from recorded histories; edit by hand only to seed a step no recording covers.",
        "",
        f"{_DEFAULT_STEP_SECONDS_KEY} = {defaults.default_step_seconds:g}",
    ]
    for scope in sorted(defaults.seconds_by_step_id_by_scope):
        lines.extend(("", f'["{scope}"]'))
        for step_id, seconds in sorted(defaults.seconds_by_step_id_by_scope[scope].items()):
            lines.append(f'"{step_id}" = {seconds:g}')
    return "\n".join(lines) + "\n"


@pure
def median_of_recent_measurements(samples: Sequence[float]) -> float:
    """The expectation one step's measurements (oldest first) give: the median of the last few."""
    return statistics.median(samples[-MEASUREMENTS_PER_STEP:])


@pure
def expected_durations_from_measurements(
    measurements_by_step_id: Mapping[str, Sequence[float]], shipped: ExpectedDurations
) -> ExpectedDurations:
    """The shipped expectations with every measured step replaced by the median of its recent measurements."""
    learned = {
        step_id: median_of_recent_measurements(samples)
        for step_id, samples in measurements_by_step_id.items()
        if samples
    }
    return ExpectedDurations(
        seconds_by_step_id={**shipped.seconds_by_step_id, **learned}, default_seconds=shipped.default_seconds
    )


@pure
def history_events_from_lines(lines: Sequence[str]) -> list[StepDurationEvent]:
    """The events in history lines, oldest first; unreadable lines are skipped."""
    events: list[StepDurationEvent] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            events.append(StepDurationEvent.model_validate_json(stripped))
        except ValidationError as exc:
            logger.warning("Skipping an unreadable progress history line: {}", exc)
    return events


@pure
def measurements_by_scope_from_events(events: Sequence[StepDurationEvent]) -> dict[str, dict[str, list[float]]]:
    """The measurements per scope and step id from history events, in their order; skips are left out."""
    measurements: dict[str, dict[str, list[float]]] = {}
    for event in events:
        if event.duration_seconds is None:
            continue
        measurements.setdefault(event.scope, {}).setdefault(event.step_id, []).append(event.duration_seconds)
    return measurements


@pure
def measurements_by_scope_from_lines(lines: Sequence[str]) -> dict[str, dict[str, list[float]]]:
    """The measurements per scope and step id from history lines, oldest first; skips and unreadable lines are left out."""
    return measurements_by_scope_from_events(history_events_from_lines(lines))


@pure
def parse_history_lines(lines: Sequence[str], scope: str) -> dict[str, list[float]]:
    """The measurements per step id for ``scope`` from history lines, oldest first; unreadable lines are skipped."""
    return measurements_by_scope_from_lines(lines).get(scope, {})


class ProgressHistoryStore(MutableModel):
    """Appends step durations to the history file and reads expectations back out of it."""

    events_path: Path = Field(frozen=True, description="The append-only history file")
    shipped_defaults: ShippedProgressDefaults = Field(frozen=True, description="The seeds for unmeasured steps")
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def record(self, scope: str, durations: Sequence[StepDuration], skipped_step_ids: Sequence[str] = ()) -> None:
        """Append one event per duration and per skipped step. Best effort: a history that cannot be written must not fail the operation.

        A skipped step is recorded so a reader restating seeds from this history
        can tell a stage the build still has (cached this time) from one that
        no longer exists.
        """
        if not durations and not skipped_step_ids:
            return
        now = format_nanosecond_iso_timestamp(datetime.now(timezone.utc))
        lines = [
            StepDurationEvent(
                timestamp=IsoTimestamp(now),
                type=PROGRESS_HISTORY_SKIPPED_EVENT_TYPE,
                event_id=EventId(f"cpr-{uuid4().hex}"),
                source=PROGRESS_HISTORY_EVENT_SOURCE,
                scope=scope,
                step_id=step_id,
            ).model_dump_json(exclude_none=True)
            for step_id in skipped_step_ids
        ]
        lines += [
            StepDurationEvent(
                timestamp=IsoTimestamp(now),
                type=PROGRESS_HISTORY_EVENT_TYPE,
                event_id=EventId(f"cpr-{uuid4().hex}"),
                source=PROGRESS_HISTORY_EVENT_SOURCE,
                scope=scope,
                step_id=duration.step_id,
                duration_seconds=duration.seconds,
            ).model_dump_json(exclude_none=True)
            for duration in durations
        ]
        with self._lock:
            try:
                self.events_path.parent.mkdir(parents=True, exist_ok=True)
                with self.events_path.open("a") as events_file:
                    events_file.write("\n".join(lines) + "\n")
            except OSError as exc:
                logger.warning("Could not record progress durations to {}: {}", self.events_path, exc)

    def expected_durations(self, scope: str) -> ExpectedDurations:
        """The expectations for ``scope``: this device's medians over the shipped seeds."""
        with self._lock:
            lines = self._read_tail_lines()
        return expected_durations_from_measurements(
            parse_history_lines(lines, scope), self.shipped_defaults.for_scope(scope)
        )

    def _read_tail_lines(self) -> list[str]:
        try:
            if not self.events_path.exists():
                return []
            return self.events_path.read_text().splitlines()[-_HISTORY_TAIL_LINES:]
        except OSError as exc:
            logger.warning("Could not read the progress history at {}: {}", self.events_path, exc)
            return []


def make_progress_history_store(data_dir: Path) -> ProgressHistoryStore:
    return ProgressHistoryStore(
        events_path=data_dir / PROGRESS_HISTORY_RELATIVE_PATH, shipped_defaults=load_shipped_defaults()
    )
