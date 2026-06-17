"""Request and response event types for the minds request inbox.

Agents write request events (permissions, latchkey-permission) to
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

    PERMISSIONS = auto()
    LATCHKEY_PERMISSION = auto()
    FILE_SHARING_PERMISSION = auto()


class RequestStatus(UpperCaseStrEnum):
    """Resolution status for a request."""

    GRANTED = auto()
    DENIED = auto()


def _generate_event_id() -> EventId:
    return EventId(f"evt-{uuid.uuid4().hex}")


def _now_iso() -> IsoTimestamp:
    return IsoTimestamp(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))


class RequestEvent(EventEnvelope):
    """Base class for all request events written by agents."""

    agent_id: str = Field(description="Agent ID that made the request")
    request_type: str = Field(description="Type of request (e.g. 'PERMISSIONS', 'LATCHKEY_PERMISSION')")
    is_user_requested: bool = Field(
        default=False,
        description="If true, desktop client auto-navigates to the request page",
    )


class PermissionsRequestEvent(RequestEvent):
    """A request for permission to access a resource."""

    resource: str = Field(description="Resource being requested")
    description: str = Field(default="", description="Human-readable description of the request")


class LatchkeyPredefinedPermissionRequestEvent(RequestEvent):
    """A request for the user to authorize the agent to use a latchkey-managed scope.

    The agent declares which Detent scope schema it wants (e.g.
    ``slack-api``), which permission schemas under that scope it would
    like, and why. The user picks the final permission set in the
    desktop dialog (which may broaden or narrow the agent's request);
    the desktop client launches ``latchkey auth browser`` if no
    credentials exist for the service yet.
    """

    scope: str = Field(
        description="Detent scope schema the agent wants permissions under (e.g. 'slack-api').",
    )
    permissions: tuple[str, ...] = Field(
        default=(),
        description=(
            "Permission schemas the agent requested under the scope; the user may grant a "
            "different subset in the dialog."
        ),
    )
    rationale: str = Field(description="One-paragraph human-readable reason the agent needs this access.")
    permissions_target_path: str | None = Field(
        default=None,
        description=(
            "Absolute path of the permissions.json the agent's gateway JWT resolves to "
            "(the streamed request's ``target`` field, i.e. the agent's opaque permissions "
            "handle). Used to recover a host whose canonical permissions file was never "
            "materialized. ``None`` when the event did not originate from the gateway stream."
        ),
    )


class LatchkeyFileSharingPermissionRequestEvent(RequestEvent):
    """A request for the user to grant the agent access to a single file path.

    Delivered to the inbox when an agent submits a ``type=file-sharing``
    permission request to the gateway. The dialog is presented as a
    yes/no on a specific absolute path (no per-permission editing): on
    approval the desktop client calls
    ``POST /permission-requests/approve/<id>`` and lets the gateway
    splice the precomputed effect into the per-host
    ``latchkey_permissions.json``; on denial it falls back to the
    existing ``DELETE /permission-requests/<id>`` path.

    ``access`` distinguishes a read-only grant from a read-write one;
    the agent declares which it needs in the request, and the gateway's
    effect grants only the WebDAV verbs that match.
    """

    path: str = Field(description="Absolute filesystem path the agent wants access to.")
    access: str = Field(
        description=(
            "Access mode the agent is requesting (``READ`` for read-only, ``WRITE`` for "
            "read+write). Carried verbatim from the streamed gateway payload."
        ),
    )
    rationale: str = Field(description="One-paragraph human-readable reason the agent needs this access.")
    permissions_target_path: str | None = Field(
        default=None,
        description=(
            "Absolute path of the permissions.json the agent's gateway JWT resolves to "
            "(the streamed request's ``target`` field, i.e. the agent's opaque permissions "
            "handle). Used to recover a host whose canonical permissions file was never "
            "materialized. ``None`` when the event did not originate from the gateway stream."
        ),
    )


class RequestResponseEvent(EventEnvelope):
    """A response to a request, written by the desktop client."""

    request_event_id: str = Field(description="event_id of the original request")
    status: str = Field(description="Resolution status: 'GRANTED' or 'DENIED'")
    agent_id: str = Field(description="Agent ID the request was for")
    scope: str | None = Field(
        default=None,
        description=(
            "Detent scope schema (for request types that scope to one, e.g. latchkey-permission). "
            "Informational only -- pending-request filtering joins requests and responses on "
            "``request_event_id``, not on ``scope``."
        ),
    )
    request_type: str = Field(description="Type of request that was responded to")


def create_latchkey_predefined_permission_request_event(
    agent_id: str,
    scope: str,
    rationale: str,
    permissions: tuple[str, ...] = (),
    is_user_requested: bool = False,
) -> "LatchkeyPredefinedPermissionRequestEvent":
    """Create a new latchkey-permission request event with auto-generated metadata."""
    return LatchkeyPredefinedPermissionRequestEvent(
        timestamp=_now_iso(),
        type=EventType("latchkey_permission_request"),
        event_id=_generate_event_id(),
        source=EventSource(REQUESTS_EVENT_SOURCE_NAME),
        agent_id=agent_id,
        request_type=str(RequestType.LATCHKEY_PERMISSION),
        is_user_requested=is_user_requested,
        scope=scope,
        permissions=permissions,
        rationale=rationale,
    )


def create_latchkey_file_sharing_permission_request_event(
    agent_id: str,
    path: str,
    access: str,
    rationale: str,
    is_user_requested: bool = False,
) -> "LatchkeyFileSharingPermissionRequestEvent":
    """Create a new file-sharing permission request event with auto-generated metadata."""
    return LatchkeyFileSharingPermissionRequestEvent(
        timestamp=_now_iso(),
        type=EventType("file_sharing_permission_request"),
        event_id=_generate_event_id(),
        source=EventSource(REQUESTS_EVENT_SOURCE_NAME),
        agent_id=agent_id,
        request_type=str(RequestType.FILE_SHARING_PERMISSION),
        is_user_requested=is_user_requested,
        path=path,
        access=access,
        rationale=rationale,
    )


def create_request_response_event(
    request_event_id: str,
    status: RequestStatus,
    agent_id: str,
    request_type: str,
    scope: str | None = None,
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
        scope=scope,
        request_type=request_type,
    )


class RequestInbox(FrozenModel):
    """Aggregates request and response events to compute the pending inbox.

    Maintains two ordered lists: requests and responses. The pending inbox
    is every request, keyed only by ``event_id``, that has no corresponding
    response. Each request the agent makes is a distinct card, even when
    several share the same agent, scope, and permissions.
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

        Requests are keyed solely by ``event_id``: every distinct
        request the agent makes is its own pending card, even when
        several carry the same agent, scope, and permissions. A request
        is pending if no response references its ``event_id``.

        Keying by ``event_id`` (rather than by content) means a
        redelivery of the *same* request -- the gateway re-emits every
        still-pending request on each stream reconnect -- collapses
        onto the existing entry instead of producing a duplicate card,
        while two genuinely separate requests are never merged.
        """
        responded_event_ids: set[str] = {str(r.request_event_id) for r in self.responses}

        # Keep the latest occurrence per event_id so a redelivered
        # request idempotently overwrites the earlier copy.
        latest_by_id: dict[str, RequestEvent] = {}
        for req in self.requests:
            latest_by_id[str(req.event_id)] = req

        # Filter out responded requests
        pending = [req for req in latest_by_id.values() if str(req.event_id) not in responded_event_ids]

        # Sort by timestamp descending (most recent first)
        pending.sort(key=lambda r: str(r.timestamp), reverse=True)
        return pending

    def get_request_by_id(self, event_id: str) -> RequestEvent | None:
        """Find a request event by its event_id.

        Note: this returns the request regardless of whether it has been
        responded to. Callers that must not act on an already-resolved
        request (e.g. re-rendering the grant/deny page) should additionally
        check :meth:`is_request_resolved`.
        """
        for req in self.requests:
            if str(req.event_id) == event_id:
                return req
        return None

    def is_request_resolved(self, event_id: str) -> bool:
        """Return whether a response has already been recorded for this request.

        A granted or denied request lingers in ``requests`` (the log is
        append-only), so its presence alone does not mean it is still
        actionable -- a matching response in ``responses`` means it is done.
        """
        return any(str(r.request_event_id) == event_id for r in self.responses)

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
        if request_type == str(RequestType.PERMISSIONS):
            return PermissionsRequestEvent.model_validate(data)
        elif request_type == str(RequestType.LATCHKEY_PERMISSION):
            return LatchkeyPredefinedPermissionRequestEvent.model_validate(data)
        elif request_type == str(RequestType.FILE_SHARING_PERMISSION):
            return LatchkeyFileSharingPermissionRequestEvent.model_validate(data)
        else:
            return RequestEvent.model_validate(data)
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        logger.warning("Failed to parse request event: {} (line: {})", e, line[:200])
        return None


# Field names that older versions of the schema wrote on response
# events but the current schema no longer accepts. These are stripped
# from raw JSON before validation so a historical events.jsonl from a
# previous minds version still loads cleanly. ``scope`` (the modern
# replacement for ``service_name`` on response events) is informational
# only -- pending-request filtering uses ``request_event_id`` -- so
# legacy entries that lose their service identity on the way in are
# still functionally correct.
_LEGACY_RESPONSE_EVENT_FIELDS: tuple[str, ...] = ("service_name",)


def parse_response_event(line: str) -> RequestResponseEvent | None:
    """Parse a single JSONL line into a RequestResponseEvent, or None on failure."""
    try:
        data = json.loads(line)
        if not isinstance(data, dict):
            return None
        for legacy_field in _LEGACY_RESPONSE_EVENT_FIELDS:
            data.pop(legacy_field, None)
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
