"""The one place the desktop client answers "what permission requests are pending?".

There is no mirrored copy of the queue: the latchkey gateway's own
persisted pending set (``GET /permission-requests``) is read on demand,
and a request counts as answered once a response event exists for it --
the response event log (``~/.minds/events/requests/events.jsonl``) is
the durable record of every verdict, seeded into memory at construction
and indexed through :meth:`record_response` as new verdicts are written
(the resolve epilogue owns the durable append). Because the in-memory
response index is append-only and keyed by request id, there is no
read-modify-write anywhere and therefore nothing to lock: a lost-update
race on pending state is unrepresentable.

Pending requests are served in the gateway's own shape,
:class:`~imbue.minds.desktop_client.latchkey.gateway_client.StreamedPermissionRequest`;
readers dispatch on the concrete type of ``request.payload``.
"""

from abc import ABC
from abc import abstractmethod
from pathlib import Path

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClientError
from imbue.minds.desktop_client.latchkey.gateway_client import StreamedPermissionRequest
from imbue.minds.desktop_client.latchkey.response_events import RequestResponseEvent
from imbue.minds.desktop_client.latchkey.response_events import load_response_events


class PendingRequestsInterface(ABC):
    """Pending permission requests and recorded verdicts, as the routes read them."""

    @abstractmethod
    def list_pending(self) -> tuple[StreamedPermissionRequest, ...]:
        """Every currently pending request, newest first."""

    @abstractmethod
    def get_pending(self, request_id: str) -> StreamedPermissionRequest | None:
        """The named pending request, or None when it is not pending."""

    @abstractmethod
    def is_resolved(self, request_id: str) -> bool:
        """Whether a grant/deny response has been recorded for this request."""

    @abstractmethod
    def record_response(self, event: RequestResponseEvent) -> None:
        """Index a just-recorded verdict so pending/resolved reads see it at once."""

    @abstractmethod
    def responses(self) -> tuple[RequestResponseEvent, ...]:
        """Every recorded verdict, oldest first (later duplicates win per id)."""


class GatewayPendingRequests(MutableModel, PendingRequestsInterface):
    """Gateway-backed pending set plus the response-log verdict index.

    ``list_pending`` is one loopback GET against the gateway; a failed read
    falls back to the last successful list (re-filtered against verdicts
    recorded since) so a gateway restart degrades to briefly-stale rather
    than empty.
    """

    gateway_client: LatchkeyGatewayClient = Field(
        frozen=True, description="Client for the gateway's permission-requests endpoints."
    )
    data_dir: Path = Field(frozen=True, description="Minds data dir holding the response event log.")

    _responses_by_request_id: dict[str, RequestResponseEvent] = PrivateAttr(default_factory=dict)
    _last_good_pending: tuple[StreamedPermissionRequest, ...] = PrivateAttr(default=())

    model_config = {"arbitrary_types_allowed": True, "frozen": False, "extra": "forbid"}

    @classmethod
    def load(cls, gateway_client: LatchkeyGatewayClient, data_dir: Path) -> "GatewayPendingRequests":
        """Build the view with its verdict index seeded from the response log."""
        view = cls(gateway_client=gateway_client, data_dir=data_dir)
        for event in load_response_events(data_dir):
            view._responses_by_request_id[event.request_event_id] = event
        return view

    def list_pending(self) -> tuple[StreamedPermissionRequest, ...]:
        try:
            streamed = self.gateway_client.list_permission_requests()
        except LatchkeyGatewayClientError as e:
            logger.warning("Could not list pending permission requests from the gateway: {}", e)
            return self._without_resolved(self._last_good_pending)
        pending = self._without_resolved(tuple(reversed(streamed)))
        self._last_good_pending = pending
        return pending

    def get_pending(self, request_id: str) -> StreamedPermissionRequest | None:
        return next((req for req in self.list_pending() if req.request_id == request_id), None)

    def is_resolved(self, request_id: str) -> bool:
        return request_id in self._responses_by_request_id

    def record_response(self, event: RequestResponseEvent) -> None:
        self._responses_by_request_id[event.request_event_id] = event

    def responses(self) -> tuple[RequestResponseEvent, ...]:
        return tuple(self._responses_by_request_id.values())

    def _without_resolved(
        self, requests: tuple[StreamedPermissionRequest, ...]
    ) -> tuple[StreamedPermissionRequest, ...]:
        # The gateway keeps listing a request whose deny-time DELETE failed;
        # the recorded verdict wins, so it never resurfaces as pending.
        return tuple(req for req in requests if req.request_id not in self._responses_by_request_id)
