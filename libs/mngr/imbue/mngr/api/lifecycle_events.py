import json
import shlex
from datetime import datetime
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.logging import format_nanosecond_iso_timestamp
from imbue.imbue_common.logging import generate_log_event_id
from imbue.mngr.errors import BaseMngrError
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId

LIFECYCLE_EVENT_SOURCE: Final[str] = "mngr/lifecycle"


class LifecycleEventType(UpperCaseStrEnum):
    """Type of agent lifecycle event."""

    AGENT_STARTING = auto()
    AGENT_READY = auto()


def get_lifecycle_events_dir(host_dir: Path, agent_id: AgentId) -> Path:
    """Return the directory for an agent's lifecycle event files."""
    return host_dir / "agents" / str(agent_id) / "events" / "mngr" / "lifecycle"


def get_lifecycle_events_path(host_dir: Path, agent_id: AgentId) -> Path:
    """Return the path to an agent's lifecycle events JSONL file."""
    return get_lifecycle_events_dir(host_dir, agent_id) / "events.jsonl"


def emit_agent_lifecycle_event(
    host: OnlineHostInterface,
    agent_id: AgentId,
    event_type: LifecycleEventType,
    start_id: str,
) -> None:
    """Append a lifecycle event to the agent's mngr/lifecycle event stream.

    Uses host.execute_stateful_command() to write the event via shell, matching
    the pattern used by Claude hooks for activity events. The start_id correlates
    paired AGENT_STARTING/AGENT_READY events from the same start attempt.
    """
    events_dir = get_lifecycle_events_dir(host.host_dir, agent_id)
    events_file = events_dir / "events.jsonl"

    timestamp = format_nanosecond_iso_timestamp(datetime.now(timezone.utc))
    event_id = generate_log_event_id()

    event_data = {
        "source": LIFECYCLE_EVENT_SOURCE,
        "type": str(event_type),
        "event_id": event_id,
        "timestamp": timestamp,
        "start_id": start_id,
    }
    event_json = json.dumps(event_data, separators=(",", ":"))

    command = (
        f"mkdir -p {shlex.quote(str(events_dir))} && echo {shlex.quote(event_json)} >> {shlex.quote(str(events_file))}"
    )

    try:
        host.execute_stateful_command(command)
        logger.debug("Emitted {} lifecycle event for agent {}", event_type, agent_id)
    except (BaseMngrError, OSError) as e:
        logger.warning("Failed to emit {} lifecycle event for agent {}: {}", event_type, agent_id, e)
