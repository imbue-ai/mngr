"""The in-process registry of cloud-connection registrations and credential updates.

Adding or updating a connection validates the pasted credentials with a
``mngr <backend> prepare`` (or a Modal listing) that can run for minutes, so
the routes answer 202 with an operation handle and finish the work on a
worker. The registry is what the status route polls and the logs route tails:
one record per operation, keyed by the connection's instance name, plus the
replayable log buffer the prepare's output lands in (the same sink the create
flow streams through).
"""

import threading
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from enum import auto
from typing import Final

from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.agent_creator import CreateAttemptLogSink
from imbue.minds.desktop_client.api_models import CloudAccountSummary
from imbue.minds.desktop_client.create_progress import OperationProgressSnapshot
from imbue.minds.desktop_client.create_progress import OperationProgressTracker
from imbue.minds.desktop_client.create_progress_history import ExpectedDurations
from imbue.minds.desktop_client.create_progress_history import ProgressHistoryStore
from imbue.minds.desktop_client.create_progress_history import connection_scope
from imbue.minds.errors import ConnectionOperationInProgressError
from imbue.mngr.primitives import ProgressStepId

# How long a finished operation stays pollable: the modal polls every second
# or two and reads the result at once, so this only has to outlive a page
# reload or a slow network, never a session.
FINISHED_CONNECTION_OPERATION_RETENTION: Final[timedelta] = timedelta(minutes=10)
# Modal has no prepare; its one listing is the single step of its operation's bar.
MODAL_CHECK_STEP: Final[ProgressStepId] = ProgressStepId("modal.check")


class ConnectionOperationKind(UpperCaseStrEnum):
    """What a connection operation does: register a new connection, or replace one's credentials."""

    ADD = auto()
    UPDATE = auto()

    @property
    def wire_kind(self) -> str:
        """The ``kind`` the operation handle and the status read carry: ``connection_add`` or ``connection_update``."""
        return f"connection_{self.value.lower()}"


class ConnectionOperationStatus(UpperCaseStrEnum):
    """Lifecycle status of a connection operation."""

    RUNNING = auto()
    DONE = auto()
    FAILED = auto()


class ConnectionOperationFailure(FrozenModel):
    """How a connection operation failed, as the synchronous route used to answer it."""

    status_code: int = Field(description="The HTTP status the synchronous route would have answered with")
    error: str = Field(description="The user-presentable message")
    code: str | None = Field(default=None, description="The failure code a client branches on, when one applies")


class ConnectionOperationRecord(FrozenModel):
    """One connection operation as the status route reports it."""

    operation_id: str = Field(description="The connection's instance name (<backend>_<hex>)")
    kind: ConnectionOperationKind = Field(description="Registration or credential update")
    status: ConnectionOperationStatus = Field(description="Running, done, or failed")
    account: CloudAccountSummary | None = Field(default=None, description="The connection, once the operation is done")
    failure: ConnectionOperationFailure | None = Field(default=None, description="Why it failed, once it has")
    finished_at: datetime | None = Field(default=None, description="When it reached a terminal status")


class ConnectionOperationRegistry(MutableModel):
    """Tracks every in-flight and recently finished connection operation, with its log buffer and progress."""

    progress_history: ProgressHistoryStore | None = Field(
        default=None,
        frozen=True,
        description=(
            "Where each operation's step durations are learned from and recorded to; None weighs every bar by "
            "the default step duration and records nothing (minimal tests)"
        ),
    )
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _record_by_id: dict[str, ConnectionOperationRecord] = PrivateAttr(default_factory=dict)
    _sink_by_id: dict[str, CreateAttemptLogSink] = PrivateAttr(default_factory=dict)
    _tracker_by_id: dict[str, OperationProgressTracker] = PrivateAttr(default_factory=dict)
    _scope_by_id: dict[str, str] = PrivateAttr(default_factory=dict)

    def begin(self, operation_id: str, kind: ConnectionOperationKind, backend_name: str) -> CreateAttemptLogSink:
        """Claim the operation and return the fresh log sink its output lands in.

        Raises ``ConnectionOperationInProgressError`` while another operation on
        the same connection is still running. Finished operations past their
        retention are dropped here, so the registry never grows past the
        connections a device touches in a session. ``backend_name`` picks the
        durations the operation's progress bar is weighed by.
        """
        now = datetime.now(timezone.utc)
        scope = connection_scope(backend_name)
        with self._lock:
            existing = self._record_by_id.get(operation_id)
            if existing is not None and existing.status is ConnectionOperationStatus.RUNNING:
                raise ConnectionOperationInProgressError(operation_id)
            for stale_id, record in tuple(self._record_by_id.items()):
                if (
                    record.finished_at is not None
                    and now - record.finished_at > FINISHED_CONNECTION_OPERATION_RETENTION
                ):
                    del self._record_by_id[stale_id]
                    self._sink_by_id.pop(stale_id, None)
                    self._tracker_by_id.pop(stale_id, None)
                    self._scope_by_id.pop(stale_id, None)
            sink = CreateAttemptLogSink()
            self._record_by_id[operation_id] = ConnectionOperationRecord(
                operation_id=operation_id, kind=kind, status=ConnectionOperationStatus.RUNNING
            )
            self._sink_by_id[operation_id] = sink
            self._tracker_by_id[operation_id] = self._new_tracker(scope)
            self._scope_by_id[operation_id] = scope
        return sink

    def _new_tracker(self, scope: str) -> OperationProgressTracker:
        if self.progress_history is None:
            return ExpectedDurations(seconds_by_step_id={}, default_seconds=10.0).new_tracker()
        return self.progress_history.expected_durations(scope).new_tracker()

    def progress_tracker(self, operation_id: str) -> OperationProgressTracker:
        """The tracker the operation's worker feeds. Raises ``KeyError`` for an operation never begun."""
        with self._lock:
            return self._tracker_by_id[operation_id]

    def progress_snapshot(self, operation_id: str) -> OperationProgressSnapshot | None:
        with self._lock:
            tracker = self._tracker_by_id.get(operation_id)
        return tracker.snapshot() if tracker is not None else None

    def finish_done(self, operation_id: str, account: CloudAccountSummary) -> None:
        self._finish(operation_id, ConnectionOperationStatus.DONE, account=account, failure=None)

    def finish_failed(self, operation_id: str, failure: ConnectionOperationFailure) -> None:
        self._finish(operation_id, ConnectionOperationStatus.FAILED, account=None, failure=failure)

    def _finish(
        self,
        operation_id: str,
        status: ConnectionOperationStatus,
        account: CloudAccountSummary | None,
        failure: ConnectionOperationFailure | None,
    ) -> None:
        with self._lock:
            running = self._record_by_id[operation_id]
            self._record_by_id[operation_id] = ConnectionOperationRecord(
                operation_id=operation_id,
                kind=running.kind,
                status=status,
                account=account,
                failure=failure,
                finished_at=datetime.now(timezone.utc),
            )
            tracker = self._tracker_by_id.get(operation_id)
            scope = self._scope_by_id.get(operation_id)
        if tracker is None or scope is None:
            return
        if status is ConnectionOperationStatus.DONE:
            tracker.complete_all()
        # The steps that finished measured something real, whichever way the operation ended.
        if self.progress_history is not None:
            self.progress_history.record(scope, tracker.drain_durations(), tracker.drain_skipped_step_ids())

    def get(self, operation_id: str) -> ConnectionOperationRecord | None:
        with self._lock:
            return self._record_by_id.get(operation_id)

    def get_log_sink(self, operation_id: str) -> CreateAttemptLogSink | None:
        with self._lock:
            return self._sink_by_id.get(operation_id)
