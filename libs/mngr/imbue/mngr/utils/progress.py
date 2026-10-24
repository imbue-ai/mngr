"""Structured progress reporting for long-running operations such as ``mngr create`` and provider ``prepare``.

An operation announces a plan (the ordered steps it intends to run), then reports
each step starting, optionally advancing through a counted sub-unit (Dockerfile
stages, commands), and completing, or skipping a step whose work was already
done (a cached build stage). A consumer that knows only the step ids can render
an honest progress bar from the plan, the completions, and whatever durations it
has learned for those ids; the human labels ride along so a step it has never
seen still has a caption, and a skip tells it the step took no time this run
without teaching it that the step is free.

The reporter lives on :class:`~imbue.mngr.config.data_types.MngrContext`, so
every layer that holds the context (the create API, hosts, providers, operator
commands) reports through the same object. The default is a no-op; the CLI
installs an emitting reporter only for ``--format jsonl``, where each event
becomes one ``{"event": "progress", ...}`` line on stdout.
"""

import hashlib
import re
import threading
import time
from abc import ABC
from abc import abstractmethod
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from enum import auto
from typing import Any
from typing import Final

from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mngr.primitives import ProgressStepId

# The ``event`` field value of every progress line in ``--format jsonl`` output.
PROGRESS_EVENT_NAME = "progress"


class ProgressEventKind(UpperCaseStrEnum):
    """What a progress event reports."""

    PLAN = auto()
    STARTED = auto()
    ADVANCED = auto()
    COMPLETED = auto()
    SKIPPED = auto()


class ProgressStep(FrozenModel):
    """One planned unit of work: a stable id plus the caption shown while it runs."""

    step_id: ProgressStepId = Field(description="Stable identifier consumers key captions and learned durations by")
    label: str = Field(description="Human caption for the step")


class ProgressEvent(FrozenModel):
    """One progress report, as emitted on the wire."""

    kind: ProgressEventKind = Field(
        description="Plan announcement, or a step starting, advancing, completing, or being skipped"
    )
    step_id: ProgressStepId | None = Field(
        default=None, description="The step this event is about; None for a plan with no parent"
    )
    parent_id: ProgressStepId | None = Field(
        default=None, description="The step this one (or this plan) nests under; None at the top level"
    )
    label: str | None = Field(default=None, description="The step's caption, sent when it starts or is skipped")
    steps: tuple[ProgressStep, ...] = Field(default=(), description="The planned steps, for a plan event")
    completed: int | None = Field(default=None, description="Sub-units finished so far, for an advance")
    total: int | None = Field(default=None, description="Sub-units in all, when the step can count them")
    elapsed_seconds: float | None = Field(default=None, description="How long the step took, for a completion")

    def to_wire(self) -> dict[str, Any]:
        """The event's fields as the JSONL line carries them, without the ``event`` envelope key."""
        wire: dict[str, Any] = {"kind": self.kind.value.lower()}
        if self.step_id is not None:
            wire["step"] = str(self.step_id)
        if self.parent_id is not None:
            wire["parent"] = str(self.parent_id)
        if self.label is not None:
            wire["label"] = self.label
        if self.kind is ProgressEventKind.PLAN:
            wire["steps"] = [{"step": str(step.step_id), "label": step.label} for step in self.steps]
        if self.completed is not None:
            wire["completed"] = self.completed
        if self.total is not None:
            wire["total"] = self.total
        if self.elapsed_seconds is not None:
            wire["elapsed_seconds"] = round(self.elapsed_seconds, 3)
        return wire


@pure
def parse_progress_event(data: Mapping[str, Any]) -> ProgressEvent | None:
    """A :class:`ProgressEvent` from the fields of one ``progress`` JSONL line, or None when they do not parse.

    Tolerant by design: a line from a newer mngr may carry a kind or fields this
    reader does not know, and a consumer must keep rendering the steps it does
    understand rather than reject the stream.
    """
    kind_raw = data.get("kind")
    if not isinstance(kind_raw, str):
        return None
    try:
        kind = ProgressEventKind(kind_raw.upper())
    except ValueError:
        return None
    step_id = _optional_step_id(data.get("step"))
    parent_id = _optional_step_id(data.get("parent"))
    label = data.get("label")
    steps: list[ProgressStep] = []
    for raw_step in data.get("steps", ()) if isinstance(data.get("steps"), list) else ():
        if not isinstance(raw_step, Mapping):
            continue
        planned_id = _optional_step_id(raw_step.get("step"))
        if planned_id is None:
            continue
        planned_label = raw_step.get("label")
        steps.append(ProgressStep(step_id=planned_id, label=planned_label if isinstance(planned_label, str) else ""))
    if kind is not ProgressEventKind.PLAN and step_id is None:
        return None
    return ProgressEvent(
        kind=kind,
        step_id=step_id,
        parent_id=parent_id,
        label=label if isinstance(label, str) else None,
        steps=tuple(steps),
        completed=_optional_count(data.get("completed")),
        total=_optional_count(data.get("total")),
        elapsed_seconds=_optional_seconds(data.get("elapsed_seconds")),
    )


@pure
def _optional_step_id(value: Any) -> ProgressStepId | None:
    if not isinstance(value, str):
        return None
    try:
        return ProgressStepId(value)
    except ValueError:
        return None


@pure
def _optional_count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


@pure
def _optional_seconds(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


class ProgressReporterInterface(MutableModel, ABC):
    """Receives the progress of an operation as it runs."""

    @abstractmethod
    def announce_plan(self, steps: Sequence[ProgressStep], parent_id: ProgressStepId | None) -> None:
        """Declare the ordered steps about to run, under ``parent_id`` or at the top level when None."""

    @abstractmethod
    def start_step(
        self, step_id: ProgressStepId, label: str, parent_id: ProgressStepId | None, total: int | None
    ) -> None:
        """Report that a step began; ``total`` names its countable sub-units when it has them."""

    @abstractmethod
    def advance_step(self, step_id: ProgressStepId, completed: int, total: int | None) -> None:
        """Report that ``completed`` of the step's sub-units are done."""

    @abstractmethod
    def complete_step(self, step_id: ProgressStepId) -> None:
        """Report that a step finished successfully."""

    @abstractmethod
    def skip_step(self, step_id: ProgressStepId, label: str, parent_id: ProgressStepId | None) -> None:
        """Report that a step's work was already done, so it ran nothing this time (a cached build stage).

        A skipped step is behind the operation like a completed one, but it is
        not a measurement of the step: a consumer learning durations must not
        record it.
        """

    @contextmanager
    def step(
        self,
        step_id: ProgressStepId,
        label: str,
        parent_id: ProgressStepId | None = None,
        total: int | None = None,
    ) -> Iterator[None]:
        """Report a step around a block, completing it only when the block exits normally.

        A failure leaves the step started: the operation's error follows, and a
        consumer must not read a failed step as done.
        """
        self.start_step(step_id, label, parent_id, total)
        yield
        self.complete_step(step_id)


class NullProgressReporter(ProgressReporterInterface):
    """Discards every report; the default when no one is listening."""

    def announce_plan(self, steps: Sequence[ProgressStep], parent_id: ProgressStepId | None) -> None:
        pass

    def start_step(
        self, step_id: ProgressStepId, label: str, parent_id: ProgressStepId | None, total: int | None
    ) -> None:
        pass

    def advance_step(self, step_id: ProgressStepId, completed: int, total: int | None) -> None:
        pass

    def complete_step(self, step_id: ProgressStepId) -> None:
        pass

    def skip_step(self, step_id: ProgressStepId, label: str, parent_id: ProgressStepId | None) -> None:
        pass


class ProgressEventSinkReporter(ProgressReporterInterface):
    """Turns reports into :class:`ProgressEvent` records and hands each to ``emit``.

    Times each step from its start so completions carry an elapsed duration.
    Safe to call from several threads: provisioning fans work out across them.
    """

    emit: Callable[[ProgressEvent], None] = Field(frozen=True, description="Receives every event, in order")
    clock: Callable[[], float] = Field(
        default=time.monotonic, frozen=True, description="Monotonic clock the step timings are read from"
    )
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _started_at_by_step_id: dict[ProgressStepId, float] = PrivateAttr(default_factory=dict)

    def announce_plan(self, steps: Sequence[ProgressStep], parent_id: ProgressStepId | None) -> None:
        self._emit_locked(ProgressEvent(kind=ProgressEventKind.PLAN, parent_id=parent_id, steps=tuple(steps)))

    def start_step(
        self, step_id: ProgressStepId, label: str, parent_id: ProgressStepId | None, total: int | None
    ) -> None:
        with self._lock:
            self._started_at_by_step_id[step_id] = self.clock()
            self.emit(
                ProgressEvent(
                    kind=ProgressEventKind.STARTED, step_id=step_id, parent_id=parent_id, label=label, total=total
                )
            )

    def advance_step(self, step_id: ProgressStepId, completed: int, total: int | None) -> None:
        self._emit_locked(
            ProgressEvent(kind=ProgressEventKind.ADVANCED, step_id=step_id, completed=completed, total=total)
        )

    def complete_step(self, step_id: ProgressStepId) -> None:
        with self._lock:
            started_at = self._started_at_by_step_id.pop(step_id, None)
            elapsed = self.clock() - started_at if started_at is not None else None
            self.emit(ProgressEvent(kind=ProgressEventKind.COMPLETED, step_id=step_id, elapsed_seconds=elapsed))

    def skip_step(self, step_id: ProgressStepId, label: str, parent_id: ProgressStepId | None) -> None:
        with self._lock:
            self._started_at_by_step_id.pop(step_id, None)
            self.emit(ProgressEvent(kind=ProgressEventKind.SKIPPED, step_id=step_id, parent_id=parent_id, label=label))

    def _emit_locked(self, event: ProgressEvent) -> None:
        with self._lock:
            self.emit(event)


class NestedProgressReporter(ProgressReporterInterface):
    """Forwards every report to ``inner``, re-homing the steps reported under one parent beneath another.

    For a provider that realizes its host through another provider's machinery
    (imbue_cloud's slow path rebuilds the leased container through the VPS
    provider): that machinery reports its steps under the create's
    ``creating_host`` phase, and the delegating provider wants them nested
    under the step it is running instead, so its own step is not read as done
    the moment the delegate's first step starts.
    """

    inner: ProgressReporterInterface = Field(frozen=True, description="The reporter every report goes on to")
    outer_parent_id: ProgressStepId = Field(frozen=True, description="The parent the delegate reports its steps under")
    nested_parent_id: ProgressStepId = Field(frozen=True, description="The step those reports are re-homed under")

    def _re_home(self, parent_id: ProgressStepId | None) -> ProgressStepId | None:
        return self.nested_parent_id if parent_id == self.outer_parent_id else parent_id

    def announce_plan(self, steps: Sequence[ProgressStep], parent_id: ProgressStepId | None) -> None:
        self.inner.announce_plan(steps, self._re_home(parent_id))

    def start_step(
        self, step_id: ProgressStepId, label: str, parent_id: ProgressStepId | None, total: int | None
    ) -> None:
        self.inner.start_step(step_id, label, self._re_home(parent_id), total)

    def advance_step(self, step_id: ProgressStepId, completed: int, total: int | None) -> None:
        self.inner.advance_step(step_id, completed, total)

    def complete_step(self, step_id: ProgressStepId) -> None:
        self.inner.complete_step(step_id)

    def skip_step(self, step_id: ProgressStepId, label: str, parent_id: ProgressStepId | None) -> None:
        self.inner.skip_step(step_id, label, self._re_home(parent_id))


@pure
def summarize_command_for_label(command: str, max_length: int = 60) -> str:
    """A one-line caption for a shell command: whitespace collapsed, cut to ``max_length`` with an ellipsis."""
    collapsed = " ".join(command.split())
    if len(collapsed) <= max_length:
        return collapsed
    return collapsed[: max_length - 3].rstrip() + "..."


@pure
def command_step_id(prefix: ProgressStepId, idx: int) -> ProgressStepId:
    """The id of the ``idx``-th command under ``prefix`` (``provisioning.command_3``)."""
    return ProgressStepId(f"{prefix}.command_{idx}")


_STEP_ID_SLUG_WORDS: Final[int] = 3
_STEP_ID_SLUG_MAX_LENGTH: Final[int] = 24
_STEP_ID_HASH_LENGTH: Final[int] = 6


@pure
def text_step_id(prefix: ProgressStepId, text: str) -> ProgressStepId:
    """The id of the step that runs ``text`` (a Dockerfile instruction) under ``prefix``.

    The id is a slug of the text's first words for a reader plus a hash of the
    whole text for identity (``docker.build.run_bash_system_3f9a1c``): a
    consumer learning how long each stage takes keys by it, so the same
    instruction keeps its history when stages are added around it, and an
    edited instruction becomes a new step rather than inheriting the old one's.
    """
    words = re.sub(r"[^a-z0-9]+", " ", text.lower()).split()
    slug = "_".join(words[:_STEP_ID_SLUG_WORDS])[:_STEP_ID_SLUG_MAX_LENGTH].rstrip("_") or "step"
    digest = hashlib.sha1(text.encode(), usedforsecurity=False).hexdigest()[:_STEP_ID_HASH_LENGTH]
    return ProgressStepId(f"{prefix}.{slug}_{digest}")
