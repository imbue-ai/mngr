"""Honest progress for a long operation: a tree of steps, weighted by how long each is expected to take.

An operation (a workspace create, a connection's provider prepare) announces the
steps it will run, then reports each one starting, advancing through a counted
inner unit, and completing or being skipped; mngr's ``--format jsonl`` progress
stream feeds the same tracker through :meth:`OperationProgressTracker.apply_event`,
nested under the desktop's own step for the ``mngr create`` call. The bar's
percent is the expected-duration-weighted share of completed work, so a step
that usually takes a minute owns a minute's worth of bar; within a running step
the fraction comes from its counter when it has one and from the time elapsed
against its expected duration otherwise. The percent never moves backwards and
never reaches 100 before the operation is done.

The remaining time is said coarsely and errs long: a step that has run past
its expectation is assumed to need half again what it has already taken, a
counted step is never read as faster than its observed pace, and the wording
lengthens at once but only shortens once the shorter estimate has persisted.
"""

import json
import math
import threading
import time
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from enum import auto
from typing import Final
from typing import assert_never

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.errors import SwitchError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mngr.utils.progress import ProgressEvent
from imbue.mngr.utils.progress import ProgressEventKind
from imbue.mngr.utils.progress import ProgressStep

# A running step is assumed to need at least this fraction of the time it has
# already taken on top of it, so an overrunning step never reads as nearly done:
# its remaining time grows with its overrun instead of decaying to nothing.
_OVERRUN_MARGIN: Final[float] = 0.5
# A running step with no counter reads as done in proportion to its elapsed time
# up to this fraction of its expectation, which is where the overrun margin
# takes over, and holds there until it completes.
_LINEAR_FRACTION: Final[float] = 1 / (1 + _OVERRUN_MARGIN)
# No running step ever reads as more than this done: only its completion says so.
_MAX_RUNNING_FRACTION: Final[float] = 0.99
# The least any step weighs: a step this device measured at nothing (its reporter
# rounds a sub-millisecond step to 0) must still be a positive weight to divide by.
_MIN_EXPECTED_SECONDS: Final[float] = 0.1
# The remaining-time wording only moves to a shorter bucket once the estimate
# has sat there this long: a longer estimate is said at once, since the
# costly mistake is promising a minute and taking ten.
ETA_SHORTEN_HOLD_SECONDS: Final[float] = 8.0


class ProgressStepStatus(UpperCaseStrEnum):
    """Where one step is in its life."""

    PENDING = auto()
    RUNNING = auto()
    DONE = auto()


class ProgressStepSnapshot(FrozenModel):
    """One top-level step as the page lists it."""

    step_id: str = Field(description="The step's stable id")
    label: str = Field(description="The step's caption")
    status: ProgressStepStatus = Field(description="Pending, running, or done")


class OperationProgressSnapshot(FrozenModel):
    """What the page needs to draw the bar and say what is happening, taken at one instant."""

    percent: float = Field(description="Work done so far, 0 to 100, never decreasing between snapshots")
    percent_cap: float = Field(
        description="The percent the page may extrapolate up to before its next poll, never past the running step"
    )
    percent_per_second: float = Field(
        description="How fast the percent is expected to grow right now, for extrapolation between polls"
    )
    remaining_text: str = Field(description="The remaining time, said coarsely; empty while nothing is known")
    current_label: str = Field(description="The caption of the innermost running step; empty before the first")
    steps: tuple[ProgressStepSnapshot, ...] = Field(description="The top-level steps, in order")


class StepDuration(FrozenModel):
    """How long one completed step took, for the duration history."""

    step_id: str = Field(description="The completed step's id")
    seconds: float = Field(description="Wall-clock seconds from its start to its completion")


class _StepNode(MutableModel):
    """One step in the tracker's tree."""

    step_id: str = Field(frozen=True)
    label: str = Field(default="")
    parent_id: str | None = Field(default=None, frozen=True)
    status: ProgressStepStatus = Field(default=ProgressStepStatus.PENDING)
    started_at: float | None = Field(default=None)
    completed: int | None = Field(default=None)
    total: int | None = Field(default=None)
    children: list["_StepNode"] = Field(default_factory=list)


@pure
def _eta_bucket(remaining_seconds: float) -> int:
    """The rank of an estimate's wording, so hysteresis can tell longer from shorter.

    0, 1 and 2 are the three short phrases; from 3 up, the bucket is one more
    than the whole minutes the wording names, rounded up so the wording never
    promises less than the estimate.
    """
    if remaining_seconds < 15:
        return 0
    if remaining_seconds < 45:
        return 1
    if remaining_seconds < 75:
        return 2
    return max(2, math.ceil(remaining_seconds / 60)) + 1


@pure
def _text_for_bucket(bucket: int) -> str:
    """The coarse remaining-time wording for a bucket of :func:`_eta_bucket`."""
    if bucket == 0:
        return "Almost there..."
    if bucket == 1:
        return "Less than a minute remaining"
    if bucket == 2:
        return "About a minute remaining"
    return f"About {bucket - 1} minutes remaining"


@pure
def remaining_text_for(remaining_seconds: float) -> str:
    """The coarse remaining-time wording for an estimate."""
    return _text_for_bucket(_eta_bucket(remaining_seconds))


@pure
def _timed_remaining(elapsed_seconds: float, expected_seconds: float) -> float:
    """The seconds a step with no counter has left after ``elapsed_seconds`` of an expected ``expected_seconds``.

    What the expectation says is left, but never less than the overrun margin
    of what has elapsed: a step that has run long is assumed to run longer.
    """
    return max(expected_seconds - elapsed_seconds, _OVERRUN_MARGIN * elapsed_seconds)


@pure
def _timed_fraction(elapsed_seconds: float, expected_seconds: float) -> float:
    """How far along a step with no counter reads: its elapsed share of the time it is now expected to take."""
    remaining = _timed_remaining(elapsed_seconds, expected_seconds)
    return elapsed_seconds / (elapsed_seconds + remaining) if elapsed_seconds + remaining > 0 else 0.0


class OperationProgressTracker(MutableModel):
    """Folds step reports into a weighted percent, a rate, a remaining-time phrase, and the step list."""

    expected_seconds_by_step_id: Mapping[str, float] = Field(
        frozen=True, description="Expected duration per step id, learned or shipped; the weights of the bar"
    )
    default_expected_seconds: float = Field(
        frozen=True, description="The weight of a step whose id has no expected duration"
    )
    clock: Callable[[], float] = Field(default=time.monotonic, frozen=True, description="Monotonic clock")
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _roots: list[_StepNode] = PrivateAttr(default_factory=list)
    _node_by_id: dict[str, _StepNode] = PrivateAttr(default_factory=dict)
    _highest_percent: float = PrivateAttr(default=0.0)
    _shown_eta_bucket: int | None = PrivateAttr(default=None)
    _shorter_eta_since: float | None = PrivateAttr(default=None)
    _durations: list[StepDuration] = PrivateAttr(default_factory=list)
    _skipped_step_ids: list[str] = PrivateAttr(default_factory=list)

    def announce_plan(self, steps: Sequence[ProgressStep], parent_id: str | None) -> None:
        """Declare the steps under ``parent_id`` (the top level when None); the latest plan for a parent wins.

        Steps already known under the parent keep their status when the new
        plan names them again, so a reporter that plans the same parent twice
        (after a retry or a fallback) does not make the bar forget what is done.
        """
        with self._lock:
            siblings = self._roots if parent_id is None else self._parent_node(parent_id).children
            existing_by_id = {node.step_id: node for node in siblings}
            for node in siblings:
                self._forget_subtree(node)
            siblings.clear()
            for step in steps:
                node = existing_by_id.get(str(step.step_id))
                if node is None:
                    node = _StepNode(step_id=str(step.step_id), label=step.label, parent_id=parent_id)
                elif step.label:
                    node.label = step.label
                else:
                    # A re-announced step with no caption keeps the one it had.
                    pass
                siblings.append(node)
                self._remember_subtree(node)

    def start_step(self, step_id: str, label: str, parent_id: str | None, total: int | None) -> None:
        with self._lock:
            node = self._node_by_id.get(step_id)
            if node is None:
                node = _StepNode(step_id=step_id, label=label, parent_id=parent_id)
                siblings = self._roots if parent_id is None else self._parent_node(parent_id).children
                siblings.append(node)
                self._node_by_id[step_id] = node
            if label:
                node.label = label
            node.status = ProgressStepStatus.RUNNING
            node.started_at = self.clock()
            node.completed = None
            node.total = total if total is not None else node.total
            # A step starting means every step planned before it is behind us,
            # whether or not its completion was reported.
            self._complete_earlier_siblings(node)
            if parent_id is not None:
                self._ensure_running(self._parent_node(parent_id))

    def advance_step(self, step_id: str, completed: int, total: int | None) -> None:
        with self._lock:
            node = self._node_by_id.get(step_id)
            if node is None:
                return
            node.completed = completed
            if total is not None:
                node.total = total

    def complete_step(self, step_id: str, duration_seconds: float | None = None) -> None:
        """Mark a step done; ``duration_seconds`` is the step's own measurement when its reporter took one."""
        with self._lock:
            node = self._node_by_id.get(step_id)
            if node is None:
                return
            self._complete_node(node, duration_seconds)

    def skip_step(self, step_id: str, label: str, parent_id: str | None) -> None:
        """Mark a step behind us without measuring it: its work was already done (a cached build stage)."""
        with self._lock:
            node = self._node_by_id.get(step_id)
            if node is None:
                node = _StepNode(step_id=step_id, label=label, parent_id=parent_id)
                siblings = self._roots if parent_id is None else self._parent_node(parent_id).children
                siblings.append(node)
                self._node_by_id[step_id] = node
            if label:
                node.label = label
            if node.status is ProgressStepStatus.DONE:
                return
            self._complete_earlier_siblings(node)
            node.status = ProgressStepStatus.DONE
            self._skipped_step_ids.append(step_id)
            if parent_id is not None:
                self._ensure_running(self._parent_node(parent_id))

    def apply_event(self, event: ProgressEvent, default_parent_id: str | None) -> None:
        """Fold one of mngr's progress events in, nesting its top level under ``default_parent_id``."""
        parent_id = str(event.parent_id) if event.parent_id is not None else default_parent_id
        if event.kind is ProgressEventKind.PLAN:
            self.announce_plan(event.steps, parent_id)
            return
        assert event.step_id is not None
        step_id = str(event.step_id)
        match event.kind:
            case ProgressEventKind.STARTED:
                self.start_step(step_id, event.label or "", parent_id, event.total)
            case ProgressEventKind.ADVANCED:
                self.advance_step(step_id, event.completed if event.completed is not None else 0, event.total)
            case ProgressEventKind.COMPLETED:
                self.complete_step(step_id, event.elapsed_seconds)
            case ProgressEventKind.SKIPPED:
                self.skip_step(step_id, event.label or "", parent_id)
            case ProgressEventKind.PLAN:
                raise SwitchError(f"Plan events are handled above: {event.kind}")
            case _ as unreachable:
                assert_never(unreachable)

    def complete_all(self) -> None:
        """Mark everything done: the operation finished."""
        with self._lock:
            for root in self._roots:
                self._complete_node(root)

    def drain_durations(self) -> list[StepDuration]:
        """The durations of steps completed since the last drain, for the history."""
        with self._lock:
            drained = list(self._durations)
            self._durations.clear()
            return drained

    def drain_skipped_step_ids(self) -> list[str]:
        """The steps skipped since the last drain, for the history: they exist, but measured nothing."""
        with self._lock:
            drained = list(self._skipped_step_ids)
            self._skipped_step_ids.clear()
            return drained

    def snapshot(self) -> OperationProgressSnapshot:
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> OperationProgressSnapshot:
        now = self.clock()
        total_weight = sum(self._expected(root) for root in self._roots)
        if total_weight <= 0:
            return OperationProgressSnapshot(
                percent=self._highest_percent,
                percent_cap=self._highest_percent,
                percent_per_second=0.0,
                remaining_text="",
                current_label="",
                steps=(),
            )
        percent = 100.0 * sum(self._expected(root) * self._fraction(root, now) for root in self._roots) / total_weight
        is_done = all(root.status is ProgressStepStatus.DONE for root in self._roots)
        percent = min(percent, 100.0 if is_done else _MAX_RUNNING_FRACTION * 100.0)
        self._highest_percent = max(self._highest_percent, percent)
        leaf, leaf_share = self._running_leaf_and_share()
        rate, cap = self._rate_and_cap(leaf, leaf_share, now)
        remaining = sum(self._remaining(root, now) for root in self._roots)
        return OperationProgressSnapshot(
            percent=self._highest_percent,
            percent_cap=max(self._highest_percent, cap),
            percent_per_second=rate,
            remaining_text="" if is_done else self._remaining_text_with_hysteresis(remaining, now),
            current_label=leaf.label if leaf is not None else "",
            steps=tuple(
                ProgressStepSnapshot(step_id=root.step_id, label=root.label, status=root.status)
                for root in self._roots
            ),
        )

    def _expected(self, node: _StepNode) -> float:
        return max(
            _MIN_EXPECTED_SECONDS, self.expected_seconds_by_step_id.get(node.step_id, self.default_expected_seconds)
        )

    def _children_denominator(self, node: _StepNode) -> float:
        """The weight the children of ``node`` share: the parent's own expectation, or more when the known children exceed it."""
        return max(self._expected(node), sum(self._expected(child) for child in node.children))

    def _fraction(self, node: _StepNode, now: float) -> float:
        if node.status is ProgressStepStatus.DONE:
            return 1.0
        if node.status is ProgressStepStatus.PENDING or node.started_at is None:
            return 0.0
        if node.children:
            weighted = sum(self._expected(child) * self._fraction(child, now) for child in node.children)
            return min(_MAX_RUNNING_FRACTION, weighted / self._children_denominator(node))
        if node.total:
            return min(_MAX_RUNNING_FRACTION, max(0, node.completed or 0) / node.total)
        return min(_MAX_RUNNING_FRACTION, _timed_fraction(now - node.started_at, self._expected(node)))

    def _remaining(self, node: _StepNode, now: float) -> float:
        """The seconds ``node`` has left, erring long.

        A running step with children defers to them, plus whatever of its own
        expectation the known children do not account for; once no child is
        running it is read like a step with no counter again, so a gap after
        the last child (an export the stages do not count) lengthens the
        estimate instead of ending it.
        """
        expected = self._expected(node)
        if node.status is ProgressStepStatus.DONE:
            return 0.0
        if node.status is ProgressStepStatus.PENDING or node.started_at is None:
            return expected
        elapsed = now - node.started_at
        if node.children:
            unaccounted = max(0.0, expected - sum(self._expected(child) for child in node.children))
            from_children = sum(self._remaining(child, now) for child in node.children) + unaccounted
            if any(child.status is ProgressStepStatus.RUNNING for child in node.children):
                return from_children
            return max(from_children, _timed_remaining(elapsed, expected))
        if node.total and node.completed:
            left = max(0, node.total - node.completed)
            return max(expected * left / node.total, elapsed * left / node.completed)
        return _timed_remaining(elapsed, expected)

    def _running_leaf_and_share(self) -> tuple[_StepNode | None, float]:
        """The innermost running step and its share of the whole bar (0 to 1)."""
        return self._running_leaf_among(self._roots, sum(self._expected(root) for root in self._roots), 1.0)

    def _running_leaf_among(
        self, siblings: Sequence[_StepNode], denominator: float, share: float
    ) -> tuple[_StepNode | None, float]:
        """The innermost running step under ``siblings`` (which share ``denominator`` of the bar's ``share``)."""
        running = next((sibling for sibling in siblings if sibling.status is ProgressStepStatus.RUNNING), None)
        if running is None:
            return None, share
        own_share = share * (self._expected(running) / denominator if denominator > 0 else 0.0)
        if not running.children:
            return running, own_share
        leaf, leaf_share = self._running_leaf_among(running.children, self._children_denominator(running), own_share)
        return (running, own_share) if leaf is None else (leaf, leaf_share)

    def _rate_and_cap(self, leaf: _StepNode | None, leaf_share: float, now: float) -> tuple[float, float]:
        """How fast the bar should grow before the next poll, and where that growth must stop."""
        if leaf is None or leaf.started_at is None or leaf.total:
            return 0.0, self._highest_percent
        expected = self._expected(leaf)
        elapsed = now - leaf.started_at
        linear_span = _LINEAR_FRACTION * expected
        if elapsed >= linear_span:
            return 0.0, self._highest_percent
        rate = 100.0 * leaf_share / expected
        cap = self._highest_percent + 100.0 * leaf_share * (_LINEAR_FRACTION - elapsed / expected)
        return rate, min(cap, _MAX_RUNNING_FRACTION * 100.0)

    def _remaining_text_with_hysteresis(self, remaining_seconds: float, now: float) -> str:
        bucket = _eta_bucket(remaining_seconds)
        if self._shown_eta_bucket is None or bucket >= self._shown_eta_bucket:
            self._shown_eta_bucket = bucket
            self._shorter_eta_since = None
        elif self._shorter_eta_since is None:
            self._shorter_eta_since = now
        elif now - self._shorter_eta_since >= ETA_SHORTEN_HOLD_SECONDS:
            self._shown_eta_bucket = bucket
            self._shorter_eta_since = None
        else:
            # The shorter estimate has not persisted long enough to be said yet.
            pass
        return _text_for_bucket(self._shown_eta_bucket)

    def _parent_node(self, parent_id: str) -> _StepNode:
        parent = self._node_by_id.get(parent_id)
        if parent is None:
            parent = _StepNode(step_id=parent_id, label="", parent_id=None)
            self._roots.append(parent)
            self._node_by_id[parent_id] = parent
        return parent

    def _ensure_running(self, node: _StepNode) -> None:
        """A step with a running child is running itself, however it was (or was not) reported."""
        if node.status is ProgressStepStatus.RUNNING:
            return
        node.status = ProgressStepStatus.RUNNING
        node.started_at = self.clock()
        self._complete_earlier_siblings(node)
        if node.parent_id is not None:
            self._ensure_running(self._parent_node(node.parent_id))

    def _complete_earlier_siblings(self, node: _StepNode) -> None:
        siblings = self._roots if node.parent_id is None else self._parent_node(node.parent_id).children
        for sibling in siblings:
            if sibling is node:
                return
            if sibling.status is not ProgressStepStatus.DONE:
                self._complete_node(sibling)

    def _complete_node(self, node: _StepNode, duration_seconds: float | None = None) -> None:
        for child in node.children:
            self._complete_node(child)
        if node.status is ProgressStepStatus.DONE:
            return
        if duration_seconds is not None:
            self._durations.append(StepDuration(step_id=node.step_id, seconds=duration_seconds))
        elif node.started_at is not None:
            self._durations.append(StepDuration(step_id=node.step_id, seconds=self.clock() - node.started_at))
        else:
            # A step completed without ever being reported started measured nothing.
            pass
        node.status = ProgressStepStatus.DONE
        if node.total:
            node.completed = node.total

    def _forget_subtree(self, node: _StepNode) -> None:
        self._node_by_id.pop(node.step_id, None)
        for child in node.children:
            self._forget_subtree(child)

    def _remember_subtree(self, node: _StepNode) -> None:
        self._node_by_id[node.step_id] = node
        for child in node.children:
            self._remember_subtree(child)


class TrackerEventSink(MutableModel):
    """Feeds mngr's parsed progress events into a tracker under one parent step."""

    tracker: OperationProgressTracker = Field(frozen=True, description="The tracker the events land in")
    default_parent_id: str | None = Field(
        default=None, frozen=True, description="The step mngr's top-level steps nest under; None for the top level"
    )

    def __call__(self, event: ProgressEvent) -> None:
        self.tracker.apply_event(event, self.default_parent_id)


# The event types the workspace template's bootstrap writes, one line per step it runs.
BOOTSTRAP_STEP_STARTED_EVENT_TYPE: Final[str] = "bootstrap_step_started"
BOOTSTRAP_STEP_COMPLETED_EVENT_TYPE: Final[str] = "bootstrap_step_completed"
BOOTSTRAP_COMPLETED_EVENT_TYPE: Final[str] = "bootstrap_completed"


def apply_bootstrap_event_lines(tracker: OperationProgressTracker, lines: Sequence[str], parent_id: str) -> None:
    """Fold the template bootstrap's event lines into the tracker as steps under ``parent_id``.

    Lines that are not bootstrap step events (other event types, unreadable
    JSON) are skipped: a template that writes no such events, or writes ones
    this reader does not know, leaves the parent step a single timed step.
    """
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError as exc:
            logger.warning("Skipping an unreadable bootstrap event line: {}", exc)
            continue
        if not isinstance(event, Mapping):
            continue
        event_type = event.get("type")
        step = event.get("step")
        if event_type == BOOTSTRAP_COMPLETED_EVENT_TYPE:
            continue
        if not isinstance(step, str) or not step:
            continue
        step_id = f"bootstrap.{step}"
        if event_type == BOOTSTRAP_STEP_STARTED_EVENT_TYPE:
            label = event.get("label")
            tracker.start_step(step_id, label if isinstance(label, str) else step, parent_id, None)
        elif event_type == BOOTSTRAP_STEP_COMPLETED_EVENT_TYPE:
            # The bootstrap timed the step itself; its figure beats one taken
            # through the read cadence on this side.
            duration = event.get("duration_seconds")
            tracker.complete_step(step_id, duration if isinstance(duration, (int, float)) and duration >= 0 else None)
        else:
            # An event type a newer template writes and this reader does not know.
            logger.debug("Ignoring a bootstrap event of type {}", event_type)
