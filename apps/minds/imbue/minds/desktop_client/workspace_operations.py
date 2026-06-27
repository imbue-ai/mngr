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


class WorkspaceOperationRegistryInterface(MutableModel, ABC):
    """Tracks short-lived in-process workspace operations (restart) and their log streams."""

    @abstractmethod
    def start(self, agent_id: AgentId, kind: WorkspaceOperationKind, now: datetime) -> None:
        """Register a new RUNNING operation for ``agent_id``, replacing any prior record."""

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


class InMemoryWorkspaceOperationRegistry(WorkspaceOperationRegistryInterface):
    """Thread-safe in-memory implementation shared by request threads and operation workers."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    record_by_agent_id: dict[AgentId, WorkspaceOperationRecord] = Field(default_factory=dict)
    log_queue_by_agent_id: dict[AgentId, "queue.Queue[str]"] = Field(default_factory=dict)
    lock: SkipValidation[threading.Lock] = Field(default_factory=threading.Lock)

    def start(self, agent_id: AgentId, kind: WorkspaceOperationKind, now: datetime) -> None:
        with self.lock:
            self.record_by_agent_id[agent_id] = WorkspaceOperationRecord(
                agent_id=agent_id,
                kind=kind,
                status=WorkspaceOperationStatus.RUNNING,
                error=None,
                started_at=now,
            )
            self.log_queue_by_agent_id[agent_id] = queue.Queue()

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
