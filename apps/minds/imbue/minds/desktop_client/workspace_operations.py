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

Operation logs are stored on the record (size-capped) rather than in a
consume-once queue: any number of readers can attach at any time and each
replays the full history from the start, so a page opened mid-operation (or a
second window) sees the same complete accounting as the dispatching page.
"""

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

# Oldest lines are dropped beyond this cap so a chatty operation (a streamed
# restore's restic progress) cannot grow memory without bound. Readers see the
# drop as a jump in their line index, never as an error.
MAX_OPERATION_LOG_LINES: Final[int] = 4000


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
    # The user cancelled the operation while it was still waiting; nothing was
    # mutated. Terminal like FAILED, but not an error -- the UI renders it as
    # a neutral notice.
    CANCELLED = auto()


class WorkspaceOperationRecord(FrozenModel):
    """Snapshot of one in-process workspace operation, keyed by its workspace agent id."""

    agent_id: AgentId = Field(description="Workspace the operation acts on (also the operation id)")
    kind: WorkspaceOperationKind = Field(description="Which kind of operation this is")
    status: WorkspaceOperationStatus = Field(description="Current lifecycle status")
    error: str | None = Field(description="Failure detail when status is FAILED, else None")
    warning: str | None = Field(
        default=None,
        description=(
            "Non-fatal caveat attached to a DONE operation (e.g. the restore succeeded but its chained "
            "backup-service update failed), else None"
        ),
    )
    started_at: datetime = Field(description="When the operation was registered")
    is_mutating: bool = Field(
        default=False,
        description="Whether the operation has started mutating the workspace (a cancel can no longer take effect)",
    )
    target: str | None = Field(
        default=None,
        description=(
            "What the operation acts on, when it needs one to be identifiable: the snapshot id for a restore, "
            "so a page loaded mid-restore can find the row it belongs to. None for whole-workspace operations."
        ),
    )


class OperationLogChunk(FrozenModel):
    """One reader's view of an operation log: the lines at and after its index."""

    lines: tuple[str, ...] = Field(description="Log lines from the requested index onward (possibly empty)")
    next_index: int = Field(description="The index to pass to the next read (past the returned lines)")
    is_terminal: bool = Field(description="Whether the operation has ended (no further lines will ever arrive)")


class WorkspaceOperationRegistryInterface(MutableModel, ABC):
    """Tracks short-lived in-process workspace operations (restart) and their log streams."""

    @abstractmethod
    def start(self, agent_id: AgentId, kind: WorkspaceOperationKind, now: datetime) -> None:
        """Register a new RUNNING operation for ``agent_id``, replacing any prior record."""

    @abstractmethod
    def start_if_idle(
        self, agent_id: AgentId, kind: WorkspaceOperationKind, now: datetime, target: str | None
    ) -> bool:
        """Atomically register a new RUNNING operation unless one is already RUNNING.

        Returns whether this caller won the claim. Dispatch routes use this
        (instead of a separate get + ``start``) so two concurrent requests
        cannot both spawn a worker for the same workspace. ``target`` records
        what the operation acts on, for operations that need one (a restore's
        snapshot id).
        """

    @abstractmethod
    def append_log(self, agent_id: AgentId, line: str) -> None:
        """Append a log line to the operation's stored log (no-op if the operation is unknown)."""

    @abstractmethod
    def complete(self, agent_id: AgentId) -> None:
        """Mark the operation DONE and end its log stream."""

    @abstractmethod
    def complete_with_warning(self, agent_id: AgentId, warning: str) -> None:
        """Mark the operation DONE but carrying a non-fatal caveat, and end its log stream."""

    @abstractmethod
    def fail(self, agent_id: AgentId, error: str) -> None:
        """Mark the operation FAILED with ``error`` and end its log stream."""

    @abstractmethod
    def cancel(self, agent_id: AgentId) -> None:
        """Mark the operation CANCELLED (a user cancel honored before any mutation) and end its log stream."""

    @abstractmethod
    def get(self, agent_id: AgentId) -> WorkspaceOperationRecord | None:
        """Return the current record for ``agent_id``, or None if there is no operation."""

    @abstractmethod
    def read_log_chunk(self, agent_id: AgentId, from_index: int, timeout_seconds: float) -> OperationLogChunk | None:
        """Return the operation's log lines at/after ``from_index``, or None if the operation is unknown.

        Blocks up to ``timeout_seconds`` when no new lines are available yet
        and the operation is still running, so a streaming reader can poll
        without spinning. ``from_index=0`` replays the full stored history.
        """

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
    log_lines_by_agent_id: dict[AgentId, list[str]] = Field(default_factory=dict)
    # How many lines have been dropped from the front of each stored log (the
    # cap), so reader indices stay logical rather than positional.
    log_first_index_by_agent_id: dict[AgentId, int] = Field(default_factory=dict)
    cancel_event_by_agent_id: dict[AgentId, SkipValidation[threading.Event]] = Field(default_factory=dict)
    # One condition guards all registry state; log readers wait on it and
    # every append/finish notifies it.
    state_condition: SkipValidation[threading.Condition] = Field(default_factory=threading.Condition)

    def start(self, agent_id: AgentId, kind: WorkspaceOperationKind, now: datetime) -> None:
        with self.state_condition:
            self._register_locked(agent_id, kind, now, None)

    def start_if_idle(
        self, agent_id: AgentId, kind: WorkspaceOperationKind, now: datetime, target: str | None
    ) -> bool:
        with self.state_condition:
            existing = self.record_by_agent_id.get(agent_id)
            if existing is not None and existing.status == WorkspaceOperationStatus.RUNNING:
                return False
            self._register_locked(agent_id, kind, now, target)
            return True

    def _register_locked(
        self, agent_id: AgentId, kind: WorkspaceOperationKind, now: datetime, target: str | None
    ) -> None:
        """Register a fresh RUNNING record; the caller must hold ``self.state_condition``."""
        self.record_by_agent_id[agent_id] = WorkspaceOperationRecord(
            agent_id=agent_id,
            kind=kind,
            status=WorkspaceOperationStatus.RUNNING,
            error=None,
            started_at=now,
            target=target,
        )
        self.log_lines_by_agent_id[agent_id] = []
        self.log_first_index_by_agent_id[agent_id] = 0
        self.cancel_event_by_agent_id[agent_id] = threading.Event()
        self.state_condition.notify_all()

    def append_log(self, agent_id: AgentId, line: str) -> None:
        with self.state_condition:
            log_lines = self.log_lines_by_agent_id.get(agent_id)
            if log_lines is None:
                return
            log_lines.append(line)
            overflow = len(log_lines) - MAX_OPERATION_LOG_LINES
            if overflow > 0:
                del log_lines[:overflow]
                self.log_first_index_by_agent_id[agent_id] += overflow
            self.state_condition.notify_all()

    def complete(self, agent_id: AgentId) -> None:
        self._finish(agent_id, WorkspaceOperationStatus.DONE, error=None, warning=None)

    def complete_with_warning(self, agent_id: AgentId, warning: str) -> None:
        self._finish(agent_id, WorkspaceOperationStatus.DONE, error=None, warning=warning)

    def fail(self, agent_id: AgentId, error: str) -> None:
        self._finish(agent_id, WorkspaceOperationStatus.FAILED, error=error, warning=None)

    def cancel(self, agent_id: AgentId) -> None:
        self._finish(agent_id, WorkspaceOperationStatus.CANCELLED, error=None, warning=None)

    def _finish(
        self, agent_id: AgentId, status: WorkspaceOperationStatus, error: str | None, warning: str | None
    ) -> None:
        with self.state_condition:
            existing = self.record_by_agent_id.get(agent_id)
            if existing is not None:
                self.record_by_agent_id[agent_id] = existing.model_copy_update(
                    to_update(existing.field_ref().status, status),
                    to_update(existing.field_ref().error, error),
                    to_update(existing.field_ref().warning, warning),
                )
            self.state_condition.notify_all()

    def get(self, agent_id: AgentId) -> WorkspaceOperationRecord | None:
        with self.state_condition:
            return self.record_by_agent_id.get(agent_id)

    def read_log_chunk(self, agent_id: AgentId, from_index: int, timeout_seconds: float) -> OperationLogChunk | None:
        with self.state_condition:
            if agent_id not in self.record_by_agent_id:
                return None
            chunk = self._read_available_locked(agent_id, from_index)
            if chunk.lines or chunk.is_terminal:
                return chunk
            # Nothing new yet and the operation is still running: wait for the
            # next append/finish (or the timeout) and read once more.
            self.state_condition.wait(timeout=timeout_seconds)
            if agent_id not in self.record_by_agent_id:
                return None
            return self._read_available_locked(agent_id, from_index)

    def _read_available_locked(self, agent_id: AgentId, from_index: int) -> OperationLogChunk:
        """Build the chunk currently available at ``from_index``; the caller must hold ``self.state_condition``."""
        log_lines = self.log_lines_by_agent_id.get(agent_id) or []
        first_index = self.log_first_index_by_agent_id.get(agent_id, 0)
        start = max(from_index - first_index, 0)
        lines = tuple(log_lines[start:])
        record = self.record_by_agent_id.get(agent_id)
        is_terminal = record is None or record.status != WorkspaceOperationStatus.RUNNING
        return OperationLogChunk(lines=lines, next_index=first_index + len(log_lines), is_terminal=is_terminal)

    def begin_mutation(self, agent_id: AgentId) -> bool:
        with self.state_condition:
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
        with self.state_condition:
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
        with self.state_condition:
            cancel_event = self.cancel_event_by_agent_id.get(agent_id)
        return cancel_event is not None and cancel_event.is_set()

    def wait_for_cancel(self, agent_id: AgentId, timeout_seconds: float) -> bool:
        with self.state_condition:
            cancel_event = self.cancel_event_by_agent_id.get(agent_id)
        if cancel_event is None:
            return False
        return cancel_event.wait(timeout_seconds)
