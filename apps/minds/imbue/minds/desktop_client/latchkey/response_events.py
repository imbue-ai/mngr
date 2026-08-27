"""Response events for permission requests.

Pending requests are the gateway's own records
(:class:`~imbue.minds.desktop_client.latchkey.gateway_client.StreamedPermissionRequest`);
the desktop client stores only the verdicts, appended to
``~/.minds/events/requests/events.jsonl`` -- the durable record every
pending/resolved read consults.
"""

import json
import uuid
from datetime import datetime
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel

_RESPONSE_EVENTS_DIR: Final[str] = "events/requests"
_RESPONSE_EVENTS_FILENAME: Final[str] = "events.jsonl"


class RequestStatus(UpperCaseStrEnum):
    """Resolution status for a request."""

    GRANTED = auto()
    DENIED = auto()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class RequestResponseEvent(FrozenModel):
    """A grant/deny verdict, appended to the response event log.

    Historical log lines carry retired envelope fields (``type``,
    ``event_id``, ``source``, per-request extras), so unknown fields are
    ignored on parse.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    timestamp: str = Field(description="ISO-8601 UTC time the verdict was recorded")
    request_event_id: str = Field(description="Request id of the request this answers")
    status: str = Field(description="Resolution status: 'GRANTED' or 'DENIED'")
    agent_id: str = Field(description="Agent ID the request was for")


def create_request_response_event(
    request_event_id: str,
    status: RequestStatus,
    agent_id: str,
) -> RequestResponseEvent:
    """Create a new request response event."""
    return RequestResponseEvent(
        timestamp=_now_iso(),
        request_event_id=request_event_id,
        status=str(status),
        agent_id=agent_id,
    )


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
    payload = dict(event.model_dump(mode="json"))
    # CLEANUP: drop these four keys once no supported desktop-client version
    # requires the retired envelope/response fields -- an older reader
    # (release rollback, or a second checkout sharing the dev env)
    # hard-requires them and would otherwise drop every verdict written by
    # this version. ``request_type`` is informational-only in those readers
    # (their pending/resolved join uses ``request_event_id``), so the
    # legacy catch-all value is safe for every verdict kind.
    payload["type"] = "request_response"
    payload["event_id"] = f"evt-{uuid.uuid4().hex}"
    payload["source"] = "requests"
    payload["request_type"] = "LATCHKEY_PERMISSION"
    line = json.dumps(payload) + "\n"
    with events_file.open("a") as f:
        f.write(line)
