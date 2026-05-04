"""Writes event files to ``$MNGR_AGENT_STATE_DIR/events/<source>/events.jsonl``.

This module is used by the workspace server to create agent-originated events
(sharing requests, refresh signals, etc.) that the minds desktop client picks
up via ``mngr event --follow``.
"""

import json
import os
import uuid
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

from loguru import logger

# Keys that the server always controls when writing a request event. Anything
# the caller passes under one of these names is silently dropped so the agent
# cannot spoof identity or routing metadata.
_RESERVED_REQUEST_EVENT_KEYS: Final[frozenset[str]] = frozenset(
    {"timestamp", "type", "event_id", "source", "agent_id", "request_type"}
)

# Mapping from RequestType (the string written to ``request_type``) to the
# event-envelope ``type`` field. Mirrors the ``RequestType`` enum and the
# ``EventType(...)`` values used in ``imbue.minds.desktop_client.request_events``
# so the desktop client's parser can dispatch on ``request_type``. Only request
# types in this table are accepted by ``write_request_event``; adding a new
# RequestType requires updating both this mapping and the desktop-side enum.
_REQUEST_TYPE_TO_EVENT_TYPE: Final[dict[str, str]] = {
    "SHARING": "sharing_request",
    "PERMISSIONS": "permissions_request",
    "LATCHKEY_PERMISSION": "latchkey_permission_request",
}

KNOWN_REQUEST_TYPES: Final[frozenset[str]] = frozenset(_REQUEST_TYPE_TO_EVENT_TYPE.keys())


class UnknownRequestTypeError(ValueError):
    """Raised when ``request_type`` is not in :data:`KNOWN_REQUEST_TYPES`."""

    ...


def _generate_event_id() -> str:
    return f"evt-{uuid.uuid4().hex}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _agent_state_dir() -> Path:
    """Return the agent state directory from the environment."""
    agent_state_dir = os.environ.get("MNGR_AGENT_STATE_DIR", "")
    if not agent_state_dir:
        raise RuntimeError("MNGR_AGENT_STATE_DIR environment variable is not set")
    return Path(agent_state_dir)


def _get_request_events_file() -> Path:
    """Return the path to the request events file for this agent."""
    return _agent_state_dir() / "events" / "requests" / "events.jsonl"


def _get_refresh_events_file() -> Path:
    """Return the path to the refresh events file for this agent."""
    return _agent_state_dir() / "events" / "refresh" / "events.jsonl"


def _append_event_line(events_file: Path, event: dict[str, object]) -> None:
    """Append a single JSONL-encoded event to the given file, creating parents as needed."""
    events_file.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event) + "\n"
    with events_file.open("a") as f:
        f.write(line)


def write_request_event(
    request_type: str,
    payload: dict[str, object],
    is_user_requested: bool = True,
) -> dict[str, object]:
    """Append a generic request event to ``events/requests/events.jsonl``.

    ``request_type`` must be one of :data:`KNOWN_REQUEST_TYPES`; unknown values
    raise :class:`UnknownRequestTypeError`. The ``payload`` is merged onto a
    metadata dict the server fills in (``timestamp``, ``type``, ``event_id``,
    ``source``, ``agent_id``, ``request_type``). Reserved metadata keys are
    stripped from ``payload`` so the caller cannot spoof them. Returns the full
    event dict that was written.
    """
    if request_type not in _REQUEST_TYPE_TO_EVENT_TYPE:
        known = ", ".join(sorted(KNOWN_REQUEST_TYPES))
        raise UnknownRequestTypeError(f"Unknown request_type {request_type!r}; expected one of: {known}")

    agent_id = os.environ.get("MNGR_AGENT_ID", "")
    if not agent_id:
        raise RuntimeError("MNGR_AGENT_ID environment variable is not set")

    event: dict[str, object] = {
        "timestamp": _now_iso(),
        "type": _REQUEST_TYPE_TO_EVENT_TYPE[request_type],
        "event_id": _generate_event_id(),
        "source": "requests",
        "agent_id": agent_id,
        "request_type": request_type,
        "is_user_requested": is_user_requested,
    }
    for key, value in payload.items():
        if key in _RESERVED_REQUEST_EVENT_KEYS:
            continue
        event[key] = value

    _append_event_line(_get_request_events_file(), event)
    logger.info("Wrote {} request event for agent {}", request_type, agent_id)
    return event


def write_sharing_request(
    agent_id: str,
    service_name: str,
    is_user_requested: bool = False,
    current_status: dict[str, object] | None = None,
    suggested_emails: list[str] | None = None,
) -> None:
    """Write a sharing request event to the agent's request events file."""
    event: dict[str, object] = {
        "timestamp": _now_iso(),
        "type": "sharing_request",
        "event_id": _generate_event_id(),
        "source": "requests",
        "agent_id": agent_id,
        "request_type": "SHARING",
        "is_user_requested": is_user_requested,
        "service_name": service_name,
    }
    if current_status is not None:
        event["current_status"] = current_status
    if suggested_emails:
        event["suggested_emails"] = suggested_emails

    _append_event_line(_get_request_events_file(), event)
    logger.info("Wrote sharing request event for agent {} service {}", agent_id, service_name)


def write_refresh_request(service_name: str) -> None:
    """Write a refresh-service event to the agent's refresh events file.

    Appended to ``events/refresh/events.jsonl`` (source=``refresh``). The minds
    desktop client tails this file via ``mngr event --follow`` and turns each
    line into a WebSocket broadcast that tells the workspace frontend to reload
    any open tabs whose web-service name matches ``service_name``.
    """
    event: dict[str, object] = {
        "timestamp": _now_iso(),
        "type": "refresh_service",
        "event_id": _generate_event_id(),
        "source": "refresh",
        "service_name": service_name,
    }
    _append_event_line(_get_refresh_events_file(), event)
    logger.info("Wrote refresh event for service {}", service_name)
