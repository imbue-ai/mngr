import json
from pathlib import Path

from imbue.mngr.api.lifecycle_events import LIFECYCLE_EVENT_SOURCE
from imbue.mngr.api.lifecycle_events import LifecycleEventType
from imbue.mngr.api.lifecycle_events import emit_agent_lifecycle_event
from imbue.mngr.api.lifecycle_events import get_lifecycle_events_dir
from imbue.mngr.api.lifecycle_events import get_lifecycle_events_path
from imbue.mngr.hosts.host import Host
from imbue.mngr.primitives import AgentId

# === Path helper tests ===


def test_get_lifecycle_events_dir_returns_correct_path(temp_host_dir: Path) -> None:
    agent_id = AgentId.generate()
    events_dir = get_lifecycle_events_dir(temp_host_dir, agent_id)
    assert events_dir == temp_host_dir / "agents" / str(agent_id) / "events" / "mngr" / "lifecycle"


def test_get_lifecycle_events_path_returns_correct_path(temp_host_dir: Path) -> None:
    agent_id = AgentId.generate()
    events_path = get_lifecycle_events_path(temp_host_dir, agent_id)
    assert events_path == get_lifecycle_events_dir(temp_host_dir, agent_id) / "events.jsonl"


# === emit_agent_lifecycle_event tests ===


def test_emit_agent_lifecycle_event_creates_file_with_correct_fields(
    local_host: Host,
    temp_host_dir: Path,
) -> None:
    agent_id = AgentId.generate()
    start_id = "start-test-abc"

    emit_agent_lifecycle_event(local_host, agent_id, LifecycleEventType.AGENT_READY, start_id)

    events_path = get_lifecycle_events_path(local_host.host_dir, agent_id)
    assert events_path.exists()
    lines = events_path.read_text().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["type"] == "AGENT_READY"
    assert data["start_id"] == start_id
    assert data["source"] == LIFECYCLE_EVENT_SOURCE
    assert "event_id" in data
    assert "timestamp" in data


def test_emit_agent_lifecycle_event_appends_multiple_events(
    local_host: Host,
    temp_host_dir: Path,
) -> None:
    agent_id = AgentId.generate()
    start_id = "start-test-xyz"

    emit_agent_lifecycle_event(local_host, agent_id, LifecycleEventType.AGENT_STARTING, start_id)
    emit_agent_lifecycle_event(local_host, agent_id, LifecycleEventType.AGENT_READY, start_id)

    events_path = get_lifecycle_events_path(local_host.host_dir, agent_id)
    lines = events_path.read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["type"] == "AGENT_STARTING"
    assert second["type"] == "AGENT_READY"
    assert first["start_id"] == start_id
    assert second["start_id"] == start_id


def test_emit_agent_lifecycle_event_creates_parent_dirs(
    local_host: Host,
    temp_host_dir: Path,
) -> None:
    """emit_agent_lifecycle_event creates the events directory if it does not exist."""
    agent_id = AgentId.generate()
    events_path = get_lifecycle_events_path(local_host.host_dir, agent_id)
    assert not events_path.parent.exists()

    emit_agent_lifecycle_event(local_host, agent_id, LifecycleEventType.AGENT_STARTING, "start-new")

    assert events_path.exists()
