"""Writes event files to ``$MNGR_AGENT_STATE_DIR/events/<source>/events.jsonl``.

This module is used by the workspace server to create agent-originated events
(sharing requests, refresh signals, etc.) that the minds desktop client picks
up via ``mngr events --follow``.
"""

import json
import os
import uuid
from datetime import datetime
from datetime import timezone
from pathlib import Path

from loguru import logger


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
    desktop client tails this file via ``mngr events --follow`` and turns each
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
