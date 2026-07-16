"""In-memory registry for short-lived in-process workspace operations.

A workspace restart runs as an in-process worker (``mngr stop`` + ``mngr
start``), so -- unlike a destroy, which is a detached subprocess that must
outlive the desktop app for crash-survival (see :mod:`destroying`) -- it has no
durability requirement and its operation record lives purely in memory,
consistent with how create is tracked (in :class:`AgentCreator`). Killing the
app mid-restart simply abandons the restart; nothing is leaked.

The ``/api/v1/workspaces/operations/restart/<id>`` resource reads restart status
and a status-log stream from here, keyed by the workspace's agent id (which is the
operation id; the type-segmented route means it is never confused with a destroy).
"""

import queue
import threading
from abc import ABC
from abc import abstractmethod
from datetime import datetime
from enum import auto
from typing import Final

from pydantic import ConfigDict
from pydantic import Field
from pydantic import SkipValidation

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.primitives import AgentId

# Pushed onto an operation's log queue to mark end-of-stream, so a tailing SSE
# handler knows to emit its terminal frame and stop (mirrors AgentCreator's
# LOG_SENTINEL convention).
OPERATION_LOG_SENTINEL: Final[str] = "\x00__minds_operation_log_end__\x00"


class WorkspaceOperationKind(UpperCaseStrEnum):
    """Which kind of in-process workspace operation a record tracks."""

    RESTART = auto()
    BACKUP_UPDATE = auto()
    BACKUP_CONFIGURE = auto()
    BACKUP_RESTORE = auto()


class WorkspaceOperationStatus(UpperCaseStrEnum):
    """Lifecycle status of an in-process workspace operation."""

    RUNNING = auto()
    DONE = auto()
    FAILED = auto()


class WorkspaceOperationRecord(FrozenModel):
    """Snapshot of one in-process workspace operation, keyed by its workspace agent id."""

    agent_id: AgentId = Field(description="Workspace the operation acts on (also the operation id)")
    kind: WorkspaceOperationKind = Field(description="Which kind of operation this is")
    status: WorkspaceOperationStatus = Field(description="Current lifecycle status")
    error: str | None = Field(description="Failure detail when status is FAILED, else None")
    started_at: datetime = Field(description="When the operation was registered")
    is_mutating: bool = Field(
        default=False,
        description="Whether the operation has started mutating the workspace (a cancel can no longer take effect)",
    )


class WorkspaceOperationRegistryInterface(MutableModel, ABC):
    """Tracks short-lived in-process workspace operations (restart) and their log streams."""

    @abstractmethod
    def start(self, agent_id: AgentId, kind: WorkspaceOperationKind, now: datetime) -> None:
        """Register a new RUNNING operation for ``agent_id``, replacing any prior record."""

    @abstractmethod
    def start_if_idle(self, agent_id: AgentId, kind: WorkspaceOperationKind, now: datetime) -> bool:
        """Atomically register a new RUNNING operation unless one is already RUNNING.

        Returns whether this caller won the claim. Dispatch routes use this
        (instead of a separate get + ``start``) so two concurrent requests
        cannot both spawn a worker for the same workspace.
        """

    @abstractmethod
    def append_log(self, agent_id: AgentId, line: str) -> None:
        """Append a log line to the operation's stream (no-op if the operation is unknown)."""

    @abstractmethod
    def complete(self, agent_id: AgentId) -> None:
        """Mark the operation DONE and close its log stream."""

    @abstractmethod
    def fail(self, agent_id: AgentId, error: str) -> None:
        """Mark the operation FAILED with ``error`` and close its log stream."""

    @abstractmethod
    def get(self, agent_id: AgentId) -> WorkspaceOperationRecord | None:
        """Return the current record for ``agent_id``, or None if there is no operation."""

    @abstractmethod
    def get_log_queue(self, agent_id: AgentId) -> "queue.Queue[str] | None":
        """Return the operation's log queue for streaming, or None if the operation is unknown."""

    @abstractmethod
    def begin_mutation(self, agent_id: AgentId) -> bool:
        """Atomically mark the RUNNING operation as past its point of no return; returns whether it may proceed.

        Cancellation is only honest while an operation is still waiting:
        workers call this immediately before dispatching their mutating step,
        and a cancel that raced the worker's last poll wins here (the worker
        must then end the operation as cancelled instead of mutating). Once
        this returns True, ``request_cancel`` refuses further cancels.
        """

    @abstractmethod
    def request_cancel(self, agent_id: AgentId) -> bool:
        """Ask the still-waiting RUNNING operation for ``agent_id`` to stop; returns whether it will.

        Refused (returning False) once the operation has begun mutating --
        the caller should tell the user it is too late rather than pretending
        the cancel took effect.
        """

    @abstractmethod
    def is_cancel_requested(self, agent_id: AgentId) -> bool:
        """Whether a cancel has been requested for the current operation of ``agent_id``."""

    @abstractmethod
    def wait_for_cancel(self, agent_id: AgentId, timeout_seconds: float) -> bool:
        """Block up to ``timeout_seconds`` for a cancel request; returns whether one arrived.

        Poll loops use this instead of a plain sleep so a cancel wakes them
        immediately.
        """


class InMemoryWorkspaceOperationRegistry(WorkspaceOperationRegistryInterface):
    """Thread-safe in-memory implementation shared by request threads and operation workers."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    record_by_agent_id: dict[AgentId, WorkspaceOperationRecord] = Field(default_factory=dict)
    log_queue_by_agent_id: dict[AgentId, "queue.Queue[str]"] = Field(default_factory=dict)
    cancel_event_by_agent_id: dict[AgentId, SkipValidation[threading.Event]] = Field(default_factory=dict)
    lock: SkipValidation[threading.Lock] = Field(default_factory=threading.Lock)

    def start(self, agent_id: AgentId, kind: WorkspaceOperationKind, now: datetime) -> None:
        with self.lock:
            self._register_locked(agent_id, kind, now)

    def start_if_idle(self, agent_id: AgentId, kind: WorkspaceOperationKind, now: datetime) -> bool:
        with self.lock:
            existing = self.record_by_agent_id.get(agent_id)
            if existing is not None and existing.status == WorkspaceOperationStatus.RUNNING:
                return False
            self._register_locked(agent_id, kind, now)
            return True

    def _register_locked(self, agent_id: AgentId, kind: WorkspaceOperationKind, now: datetime) -> None:
        """Register a fresh RUNNING record; the caller must hold ``self.lock``."""
        self.record_by_agent_id[agent_id] = WorkspaceOperationRecord(
            agent_id=agent_id,
            kind=kind,
            status=WorkspaceOperationStatus.RUNNING,
            error=None,
            started_at=now,
        )
        self.log_queue_by_agent_id[agent_id] = queue.Queue()
        self.cancel_event_by_agent_id[agent_id] = threading.Event()

    def append_log(self, agent_id: AgentId, line: str) -> None:
        with self.lock:
            log_queue = self.log_queue_by_agent_id.get(agent_id)
        if log_queue is not None:
            log_queue.put(line)

    def complete(self, agent_id: AgentId) -> None:
        self._finish(agent_id, WorkspaceOperationStatus.DONE, error=None)

    def fail(self, agent_id: AgentId, error: str) -> None:
        self._finish(agent_id, WorkspaceOperationStatus.FAILED, error=error)

    def _finish(self, agent_id: AgentId, status: WorkspaceOperationStatus, error: str | None) -> None:
        with self.lock:
            existing = self.record_by_agent_id.get(agent_id)
            if existing is not None:
                self.record_by_agent_id[agent_id] = existing.model_copy_update(
                    to_update(existing.field_ref().status, status),
                    to_update(existing.field_ref().error, error),
                )
            log_queue = self.log_queue_by_agent_id.get(agent_id)
        if log_queue is not None:
            log_queue.put(OPERATION_LOG_SENTINEL)

    def get(self, agent_id: AgentId) -> WorkspaceOperationRecord | None:
        with self.lock:
            return self.record_by_agent_id.get(agent_id)

    def get_log_queue(self, agent_id: AgentId) -> "queue.Queue[str] | None":
        with self.lock:
            return self.log_queue_by_agent_id.get(agent_id)

    def begin_mutation(self, agent_id: AgentId) -> bool:
        with self.lock:
            record = self.record_by_agent_id.get(agent_id)
            cancel_event = self.cancel_event_by_agent_id.get(agent_id)
            if record is None or record.status != WorkspaceOperationStatus.RUNNING:
                return False
            # A cancel that arrived before this claim wins: the worker must
            # honor it instead of mutating. Decided under the same lock that
            # request_cancel sets the event under, so exactly one of the two
            # can win a race.
            if cancel_event is not None and cancel_event.is_set():
                return False
            self.record_by_agent_id[agent_id] = record.model_copy_update(
                to_update(record.field_ref().is_mutating, True)
            )
            return True

    def request_cancel(self, agent_id: AgentId) -> bool:
        with self.lock:
            record = self.record_by_agent_id.get(agent_id)
            cancel_event = self.cancel_event_by_agent_id.get(agent_id)
            if record is None or record.status != WorkspaceOperationStatus.RUNNING or cancel_event is None:
                return False
            # Too late: the operation is already mutating the workspace.
            # Setting the event happens under the lock so this decision and
            # begin_mutation's are strictly ordered.
            if record.is_mutating:
                return False
            cancel_event.set()
            return True

    def is_cancel_requested(self, agent_id: AgentId) -> bool:
        with self.lock:
            cancel_event = self.cancel_event_by_agent_id.get(agent_id)
        return cancel_event is not None and cancel_event.is_set()

    def wait_for_cancel(self, agent_id: AgentId, timeout_seconds: float) -> bool:
        with self.lock:
            cancel_event = self.cancel_event_by_agent_id.get(agent_id)
        if cancel_event is None:
            return False
        return cancel_event.wait(timeout_seconds)
