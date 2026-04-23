"""Writes request events to ``$MNGR_AGENT_STATE_DIR/events/requests/events.jsonl``.

This module is used by the workspace server to create sharing and
permissions request events that the minds desktop client will process.
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


def _get_events_file() -> Path:
    """Return the path to the request events file for this agent."""
    agent_state_dir = os.environ.get("MNGR_AGENT_STATE_DIR", "")
    if not agent_state_dir:
        raise RuntimeError("MNGR_AGENT_STATE_DIR environment variable is not set")
    return Path(agent_state_dir) / "events" / "requests" / "events.jsonl"


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

    events_file = _get_events_file()
    events_file.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event) + "\n"
    with events_file.open("a") as f:
        f.write(line)
    logger.info("Wrote sharing request event for agent {} service {}", agent_id, service_name)
