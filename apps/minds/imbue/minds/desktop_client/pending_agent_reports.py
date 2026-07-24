"""Durable in-memory queue of agent-submitted bug reports awaiting human review.

An in-workspace agent escalates a bug report by POSTing its diagnosis to
``/api/v1/agents/<id>/report``. Rather than submit to Sentry itself, that route
appends the report here, so it is retained regardless of whether any window is
currently connected or has a modal free -- the fire-and-forget "open a modal now"
broker this replaces dropped reports whenever the target window was busy or no SSE
connection was subscribed at POST time.

The desktop app drains the queue one report at a time: it opens the report-a-bug
modal pre-filled with the head report's description for a human to review, then
submits it (``/help/report``) or discards it (``/help/report/dismiss``), either of
which removes it here and lets the next head surface.

The store is the single source of truth for pending reports. Chrome-events SSE
connections subscribe a wake ``Event``; ``add`` and ``remove`` set every
subscriber's event so each connection's loop re-reads the head and nudges its
window to open the next report. The queue is in-memory only -- a full app restart
drops any unreviewed reports, which is acceptable (reports are rare and reviewed
promptly).
"""

import threading
import uuid

from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel


class PendingAgentReport(FrozenModel):
    """One agent-submitted bug report waiting for a human to review and submit."""

    report_id: str = Field(description="Opaque id used to submit or discard this specific report")
    description: str = Field(description="The agent's diagnosis, pre-filled into the report form")
    workspace_agent_id: str | None = Field(
        default=None,
        description="The workspace the report is scoped to, so the chrome opens the modal in that window.",
    )


class PendingAgentReportStore(MutableModel):
    """Ordered in-memory queue of pending agent reports, with wake-on-change fan-out.

    ``add`` appends and wakes every subscribed chrome-events connection; ``remove``
    (on submit or discard) drops one report by id and wakes them again so the loop
    advances to the next head. Subscriptions are added on connect and removed in the
    SSE loop's ``finally``.
    """

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _reports: list[PendingAgentReport] = PrivateAttr(default_factory=list)
    _wake_events: list[threading.Event] = PrivateAttr(default_factory=list)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def subscribe(self, wake_event: threading.Event) -> None:
        """Register a chrome-events connection's wake event to be set on every queue change."""
        with self._lock:
            self._wake_events.append(wake_event)

    def unsubscribe(self, wake_event: threading.Event) -> None:
        """Unregister a previously subscribed wake event. No-op if not subscribed."""
        with self._lock:
            try:
                self._wake_events.remove(wake_event)
            except ValueError:
                pass

    def add(self, description: str, workspace_agent_id: str | None) -> str:
        """Append a report to the queue and wake every subscriber. Returns its new report id."""
        report = PendingAgentReport(
            report_id=uuid.uuid4().hex, description=description, workspace_agent_id=workspace_agent_id
        )
        with self._lock:
            self._reports.append(report)
            wake_events = list(self._wake_events)
        for wake_event in wake_events:
            wake_event.set()
        return report.report_id

    def remove(self, report_id: str) -> bool:
        """Drop the report with ``report_id`` and wake every subscriber. Returns whether it was present."""
        with self._lock:
            for index, report in enumerate(self._reports):
                if report.report_id == report_id:
                    del self._reports[index]
                    wake_events = list(self._wake_events)
                    break
            else:
                return False
        for wake_event in wake_events:
            wake_event.set()
        return True

    def head(self) -> PendingAgentReport | None:
        """Return the oldest pending report (the next to review), or None when the queue is empty."""
        with self._lock:
            return self._reports[0] if self._reports else None

    def head_id(self) -> str | None:
        """Return just the oldest pending report's id (the SSE nudge's payload), or None when empty."""
        with self._lock:
            return self._reports[0].report_id if self._reports else None

    def list_pending(self) -> list[PendingAgentReport]:
        """Return a snapshot of all pending reports, oldest first."""
        with self._lock:
            return list(self._reports)
