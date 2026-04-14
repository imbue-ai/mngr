import json
from pathlib import Path

from imbue.mngr.api.lifecycle_events import get_lifecycle_events_path
from imbue.mngr.primitives import AgentId


def write_lifecycle_event(
    host_dir: Path,
    agent_id: AgentId,
    event_type: str,
    start_id: str = "start-test",
) -> None:
    """Write a lifecycle event to the agent's events file on disk."""
    events_file = get_lifecycle_events_path(host_dir, agent_id)
    events_file.parent.mkdir(parents=True, exist_ok=True)
    event_data = json.dumps({"type": event_type, "start_id": start_id})
    with open(events_file, "a") as f:
        f.write(event_data + "\n")
