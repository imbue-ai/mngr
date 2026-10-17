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
from imbue.minds.errors import ConnectionOperationInProgressError

# How long a finished operation stays pollable: the modal polls every second
# or two and reads the result at once, so this only has to outlive a page
# reload or a slow network, never a session.
FINISHED_CONNECTION_OPERATION_RETENTION: Final[timedelta] = timedelta(minutes=10)


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
    """Tracks every in-flight and recently finished connection operation, with its log buffer."""

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _record_by_id: dict[str, ConnectionOperationRecord] = PrivateAttr(default_factory=dict)
    _sink_by_id: dict[str, CreateAttemptLogSink] = PrivateAttr(default_factory=dict)

    def begin(self, operation_id: str, kind: ConnectionOperationKind) -> CreateAttemptLogSink:
        """Claim the operation and return the fresh log sink its output lands in.

        Raises ``ConnectionOperationInProgressError`` while another operation on
        the same connection is still running. Finished operations past their
        retention are dropped here, so the registry never grows past the
        connections a device touches in a session.
        """
        now = datetime.now(timezone.utc)
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
            sink = CreateAttemptLogSink()
            self._record_by_id[operation_id] = ConnectionOperationRecord(
                operation_id=operation_id, kind=kind, status=ConnectionOperationStatus.RUNNING
            )
            self._sink_by_id[operation_id] = sink
        return sink

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

    def get(self, operation_id: str) -> ConnectionOperationRecord | None:
        with self._lock:
            return self._record_by_id.get(operation_id)

    def get_log_sink(self, operation_id: str) -> CreateAttemptLogSink | None:
        with self._lock:
            return self._sink_by_id.get(operation_id)
