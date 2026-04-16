"""Request and response event types for the minds request inbox.

Agents write request events (sharing, permissions) to
``$MNGR_AGENT_STATE_DIR/events/requests/events.jsonl``. The desktop
client watches these and presents them in an inbox panel. Response
events (grant/deny) are written by the desktop client to
``~/.minds/events/requests/events.jsonl``.

All events use the ``EventEnvelope`` base class for consistent structure.
The inbox state is computed by aggregating all request and response events
(event sourcing).
"""

import json
import uuid
from datetime import datetime
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.event_envelope import EventEnvelope
from imbue.imbue_common.event_envelope import EventId
from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import EventType
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update

REQUESTS_EVENT_SOURCE_NAME: Final[str] = "requests"
_RESPONSE_EVENTS_DIR: Final[str] = "events/requests"
_RESPONSE_EVENTS_FILENAME: Final[str] = "events.jsonl"


class RequestType(UpperCaseStrEnum):
    """Type of request an agent can make."""

    SHARING = auto()
    PERMISSIONS = auto()


class RequestStatus(UpperCaseStrEnum):
    """Resolution status for a request."""

    GRANTED = auto()
    DENIED = auto()


def _generate_event_id() -> EventId:
    return EventId(f"evt-{uuid.uuid4().hex}")


def _now_iso() -> IsoTimestamp:
    return IsoTimestamp(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))


class SharingStatusSnapshot(FrozenModel):
    """Snapshot of the current sharing status, included in a sharing request event."""

    enabled: bool = Field(description="Whether sharing is currently enabled")
    url: str | None = Field(default=None, description="Current shared URL if enabled")
    auth_rules: list[dict[str, object]] = Field(
        default_factory=list, description="Current Cloudflare Access auth policy rules"
    )


class RequestEvent(EventEnvelope):
    """Base class for all request events written by agents."""

    agent_id: str = Field(description="Agent ID that made the request")
    request_type: str = Field(description="Type of request (e.g. 'SHARING', 'PERMISSIONS')")
    is_user_requested: bool = Field(
        default=False,
        description="If true, desktop client auto-navigates to the request page",
    )


class SharingRequestEvent(RequestEvent):
    """A request to modify sharing settings for a server."""

    server_name: str = Field(description="Name of the server to share")
    current_status: SharingStatusSnapshot | None = Field(
        default=None, description="Current sharing state for pre-populating the form"
    )
    suggested_emails: list[str] = Field(
        default_factory=list, description="Suggested email addresses to share with"
    )


class PermissionsRequestEvent(RequestEvent):
    """A request for permission to access a resource."""

    resource: str = Field(description="Resource being requested")
    description: str = Field(default="", description="Human-readable description of the request")


class RequestResponseEvent(EventEnvelope):
    """A response to a request, written by the desktop client."""

    request_event_id: str = Field(description="event_id of the original request")
    status: str = Field(description="Resolution status: 'GRANTED' or 'DENIED'")
    agent_id: str = Field(description="Agent ID the request was for")
    server_name: str | None = Field(default=None, description="Server name (for sharing requests)")
    request_type: str = Field(description="Type of request that was responded to")


def create_sharing_request_event(
    agent_id: str,
    server_name: str,
    is_user_requested: bool = False,
    current_status: SharingStatusSnapshot | None = None,
    suggested_emails: list[str] | None = None,
) -> SharingRequestEvent:
    """Create a new sharing request event with auto-generated metadata."""
    return SharingRequestEvent(
        timestamp=_now_iso(),
        type=EventType("sharing_request"),
        event_id=_generate_event_id(),
        source=EventSource(REQUESTS_EVENT_SOURCE_NAME),
        agent_id=agent_id,
        request_type=str(RequestType.SHARING),
        is_user_requested=is_user_requested,
        server_name=server_name,
        current_status=current_status,
        suggested_emails=suggested_emails or [],
    )


def create_request_response_event(
    request_event_id: str,
    status: RequestStatus,
    agent_id: str,
    request_type: str,
    server_name: str | None = None,
) -> RequestResponseEvent:
    """Create a new request response event."""
    return RequestResponseEvent(
        timestamp=_now_iso(),
        type=EventType("request_response"),
        event_id=_generate_event_id(),
        source=EventSource(REQUESTS_EVENT_SOURCE_NAME),
        request_event_id=request_event_id,
        status=str(status),
        agent_id=agent_id,
        server_name=server_name,
        request_type=request_type,
    )


def _dedup_key(event: RequestEvent | RequestResponseEvent) -> tuple[str, str | None, str]:
    """Compute the deduplication key for a request or response event."""
    server_name: str | None = None
    if isinstance(event, SharingRequestEvent):
        server_name = event.server_name
    elif isinstance(event, RequestResponseEvent):
        server_name = event.server_name
    else:
        pass
    return (event.agent_id, server_name, event.request_type)


class RequestInbox(FrozenModel):
    """Aggregates request and response events to compute the pending inbox.

    Maintains two ordered lists: requests and responses. The pending inbox
    is computed by finding the latest request per dedup key that has no
    corresponding response.
    """

    requests: list[RequestEvent] = Field(default_factory=list)
    responses: list[RequestResponseEvent] = Field(default_factory=list)

    def add_request(self, event: RequestEvent) -> "RequestInbox":
        """Return a new inbox with the request added."""
        return self.model_copy_update(
            to_update(self.field_ref().requests, [*self.requests, event]),
        )

    def add_response(self, event: RequestResponseEvent) -> "RequestInbox":
        """Return a new inbox with the response added."""
        return self.model_copy_update(
            to_update(self.field_ref().responses, [*self.responses, event]),
        )

    def get_pending_requests(self) -> list[RequestEvent]:
        """Compute the list of pending (unresolved) requests.

        For each dedup key, takes the most recent request. A request is
        pending if no response references its event_id.
        """
        responded_event_ids: set[str] = {str(r.request_event_id) for r in self.responses}

        # Find latest request per dedup key
        latest_by_key: dict[tuple[str, str | None, str], RequestEvent] = {}
        for req in self.requests:
            key = _dedup_key(req)
            latest_by_key[key] = req

        # Filter out responded requests
        pending = [
            req for req in latest_by_key.values() if str(req.event_id) not in responded_event_ids
        ]

        # Sort by timestamp descending (most recent first)
        pending.sort(key=lambda r: str(r.timestamp), reverse=True)
        return pending

    def get_request_by_id(self, event_id: str) -> RequestEvent | None:
        """Find a request event by its event_id."""
        for req in self.requests:
            if str(req.event_id) == event_id:
                return req
        return None

    def get_pending_count(self) -> int:
        """Return the number of pending requests."""
        return len(self.get_pending_requests())


def parse_request_event(line: str) -> RequestEvent | None:
    """Parse a single JSONL line into a RequestEvent, or None on failure."""
    try:
        data = json.loads(line)
        if not isinstance(data, dict):
            return None
        request_type = data.get("request_type", "")
        if request_type == str(RequestType.SHARING):
            return SharingRequestEvent.model_validate(data)
        elif request_type == str(RequestType.PERMISSIONS):
            return PermissionsRequestEvent.model_validate(data)
        else:
            return RequestEvent.model_validate(data)
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        logger.warning("Failed to parse request event: {} (line: {})", e, line[:200])
        return None


def parse_response_event(line: str) -> RequestResponseEvent | None:
    """Parse a single JSONL line into a RequestResponseEvent, or None on failure."""
    try:
        data = json.loads(line)
        if not isinstance(data, dict):
            return None
        return RequestResponseEvent.model_validate(data)
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        logger.warning("Failed to parse response event: {} (line: {})", e, line[:200])
        return None


def load_response_events(data_dir: Path) -> list[RequestResponseEvent]:
    """Load all response events from ``~/.minds/events/requests/events.jsonl``."""
    events_file = data_dir / _RESPONSE_EVENTS_DIR / _RESPONSE_EVENTS_FILENAME
    if not events_file.exists():
        return []
    events: list[RequestResponseEvent] = []
    try:
        for line in events_file.read_text().splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            event = parse_response_event(stripped)
            if event is not None:
                events.append(event)
    except OSError as e:
        logger.warning("Failed to read response events: {}", e)
    return events


def append_response_event(data_dir: Path, event: RequestResponseEvent) -> None:
    """Append a response event to ``~/.minds/events/requests/events.jsonl``."""
    events_dir = data_dir / _RESPONSE_EVENTS_DIR
    events_dir.mkdir(parents=True, exist_ok=True)
    events_file = events_dir / _RESPONSE_EVENTS_FILENAME
    line = json.dumps(event.model_dump(mode="json")) + "\n"
    with events_file.open("a") as f:
        f.write(line)


def write_request_event_to_file(events_file: Path, event: RequestEvent) -> None:
    """Append a request event to the given events.jsonl file."""
    events_file.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event.model_dump(mode="json")) + "\n"
    with events_file.open("a") as f:
        f.write(line)
