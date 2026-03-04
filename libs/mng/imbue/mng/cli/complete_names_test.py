import json
from pathlib import Path

from imbue.mng.cli.complete_names import resolve_names_from_discovery_stream
from imbue.mng.utils.testing import write_discovery_snapshot_to_path


def test_complete_names_reads_discovery_stream(tmp_path: Path) -> None:
    """The complete_names module should resolve agent names from the discovery event stream."""
    events_path = tmp_path / "events" / "mng" / "discovery" / "events.jsonl"
    write_discovery_snapshot_to_path(events_path, ["beta-agent", "alpha-agent"])

    agent_names, host_names = resolve_names_from_discovery_stream(events_path)

    assert agent_names == ["alpha-agent", "beta-agent"]
    assert host_names == ["localhost"]


def test_complete_names_handles_destroyed_agents(tmp_path: Path) -> None:
    """The complete_names module should exclude destroyed agents."""
    events_dir = tmp_path / "events" / "mng" / "discovery"
    events_dir.mkdir(parents=True, exist_ok=True)
    events_path = events_dir / "events.jsonl"

    # Write a full snapshot with two agents, then destroy one
    snapshot = {
        "timestamp": "2025-01-01T00:00:00Z",
        "type": "DISCOVERY_FULL",
        "event_id": "evt-1",
        "source": "mng/discovery",
        "agents": [
            {"agent_id": "agent-0", "agent_name": "kept-agent", "host_id": "host-1", "provider_name": "local"},
            {"agent_id": "agent-1", "agent_name": "doomed-agent", "host_id": "host-1", "provider_name": "local"},
        ],
        "hosts": [{"host_id": "host-1", "host_name": "localhost", "provider_name": "local"}],
    }
    destroyed = {
        "timestamp": "2025-01-01T00:01:00Z",
        "type": "AGENT_DESTROYED",
        "event_id": "evt-2",
        "source": "mng/discovery",
        "agent_id": "agent-1",
        "host_id": "host-1",
    }
    events_path.write_text(json.dumps(snapshot) + "\n" + json.dumps(destroyed) + "\n")

    agent_names, _ = resolve_names_from_discovery_stream(events_path)

    assert agent_names == ["kept-agent"]


def test_complete_names_handles_host_destroyed(tmp_path: Path) -> None:
    """The complete_names module should remove agents when their host is destroyed."""
    events_dir = tmp_path / "events" / "mng" / "discovery"
    events_dir.mkdir(parents=True, exist_ok=True)
    events_path = events_dir / "events.jsonl"

    snapshot = {
        "timestamp": "2025-01-01T00:00:00Z",
        "type": "DISCOVERY_FULL",
        "event_id": "evt-1",
        "source": "mng/discovery",
        "agents": [
            {"agent_id": "agent-0", "agent_name": "agent-on-host-1", "host_id": "host-1", "provider_name": "local"},
            {"agent_id": "agent-1", "agent_name": "agent-on-host-2", "host_id": "host-2", "provider_name": "modal"},
        ],
        "hosts": [
            {"host_id": "host-1", "host_name": "host-one", "provider_name": "local"},
            {"host_id": "host-2", "host_name": "host-two", "provider_name": "modal"},
        ],
    }
    host_destroyed = {
        "timestamp": "2025-01-01T00:01:00Z",
        "type": "HOST_DESTROYED",
        "event_id": "evt-2",
        "source": "mng/discovery",
        "host_id": "host-2",
        "agent_ids": ["agent-1"],
    }
    events_path.write_text(json.dumps(snapshot) + "\n" + json.dumps(host_destroyed) + "\n")

    agent_names, host_names = resolve_names_from_discovery_stream(events_path)

    assert agent_names == ["agent-on-host-1"]
    assert host_names == ["host-one"]


def test_complete_names_returns_empty_when_no_file(tmp_path: Path) -> None:
    """Returns empty lists when the discovery events file does not exist."""
    nonexistent = tmp_path / "no" / "such" / "file.jsonl"

    agent_names, host_names = resolve_names_from_discovery_stream(nonexistent)

    assert agent_names == []
    assert host_names == []


def test_complete_names_incremental_agent_discovered(tmp_path: Path) -> None:
    """AGENT_DISCOVERED events after the snapshot should add new agents."""
    events_dir = tmp_path / "events" / "mng" / "discovery"
    events_dir.mkdir(parents=True, exist_ok=True)
    events_path = events_dir / "events.jsonl"

    snapshot = {
        "timestamp": "2025-01-01T00:00:00Z",
        "type": "DISCOVERY_FULL",
        "event_id": "evt-1",
        "source": "mng/discovery",
        "agents": [
            {"agent_id": "agent-0", "agent_name": "original", "host_id": "host-1", "provider_name": "local"},
        ],
        "hosts": [{"host_id": "host-1", "host_name": "host-one", "provider_name": "local"}],
    }
    new_agent = {
        "timestamp": "2025-01-01T00:01:00Z",
        "type": "AGENT_DISCOVERED",
        "event_id": "evt-2",
        "source": "mng/discovery",
        "agent": {"agent_id": "agent-1", "agent_name": "newcomer", "host_id": "host-1", "provider_name": "local"},
    }
    events_path.write_text(json.dumps(snapshot) + "\n" + json.dumps(new_agent) + "\n")

    agent_names, _ = resolve_names_from_discovery_stream(events_path)

    assert agent_names == ["newcomer", "original"]
