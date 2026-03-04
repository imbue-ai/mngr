import json
from datetime import datetime
from datetime import timezone
from pathlib import Path
from uuid import uuid4

from imbue.mng.api.discovery_events import AgentDiscoveryEvent
from imbue.mng.api.discovery_events import DiscoveryEventType
from imbue.mng.api.discovery_events import FullDiscoverySnapshotEvent
from imbue.mng.api.discovery_events import HostDiscoveryEvent
from imbue.mng.api.discovery_events import append_discovery_event
from imbue.mng.api.discovery_events import build_discovered_agent
from imbue.mng.api.discovery_events import discovered_agent_from_agent_details
from imbue.mng.api.discovery_events import discovered_host_from_agent_details
from imbue.mng.api.discovery_events import emit_agent_discovered
from imbue.mng.api.discovery_events import emit_host_discovered
from imbue.mng.api.discovery_events import extract_agents_and_hosts_from_full_listing
from imbue.mng.api.discovery_events import find_latest_full_snapshot_offset
from imbue.mng.api.discovery_events import get_discovery_events_dir
from imbue.mng.api.discovery_events import get_discovery_events_path
from imbue.mng.api.discovery_events import make_agent_discovery_event
from imbue.mng.api.discovery_events import make_full_discovery_snapshot_event
from imbue.mng.api.discovery_events import make_host_discovery_event
from imbue.mng.api.discovery_events import parse_discovery_event_line
from imbue.mng.api.discovery_events import write_full_discovery_snapshot
from imbue.mng.config.data_types import MngConfig
from imbue.mng.interfaces.data_types import AgentDetails
from imbue.mng.interfaces.data_types import HostDetails
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.utils.testing import make_test_discovered_agent
from imbue.mng.utils.testing import make_test_discovered_host


def _make_agent_details(host_id: HostId, provider_name: ProviderInstanceName) -> AgentDetails:
    host_details = HostDetails(
        id=host_id,
        name="test-host",
        provider_name=provider_name,
    )
    return AgentDetails(
        id=AgentId.generate(),
        name=AgentName(f"test-agent-{uuid4().hex}"),
        type="claude",
        command=CommandString("echo test"),
        work_dir=Path("/tmp/test"),
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        labels={},
        host=host_details,
    )


# === Path Helper Tests ===


def test_get_discovery_events_dir_returns_correct_path(temp_config: MngConfig) -> None:
    events_dir = get_discovery_events_dir(temp_config)
    assert events_dir == temp_config.default_host_dir / "events" / "mng" / "discovery"


def test_get_discovery_events_path_returns_jsonl_file(temp_config: MngConfig) -> None:
    events_path = get_discovery_events_path(temp_config)
    assert events_path.name == "events.jsonl"
    assert events_path.parent.name == "discovery"


# === Event Construction Tests ===


def test_make_agent_discovery_event_has_correct_fields() -> None:
    agent = make_test_discovered_agent()
    event = make_agent_discovery_event(agent)
    assert event.type == DiscoveryEventType.AGENT_DISCOVERED
    assert event.source == "mng/discovery"
    assert event.event_id.startswith("evt-")
    assert event.agent == agent


def test_make_host_discovery_event_has_correct_fields() -> None:
    host = make_test_discovered_host()
    event = make_host_discovery_event(host)
    assert event.type == DiscoveryEventType.HOST_DISCOVERED
    assert event.source == "mng/discovery"
    assert event.event_id.startswith("evt-")
    assert event.host == host


def test_make_full_discovery_snapshot_event_has_correct_fields() -> None:
    agents = (make_test_discovered_agent(), make_test_discovered_agent())
    hosts = (make_test_discovered_host(),)
    event = make_full_discovery_snapshot_event(agents, hosts)
    assert event.type == DiscoveryEventType.DISCOVERY_FULL
    assert event.source == "mng/discovery"
    assert len(event.agents) == 2
    assert len(event.hosts) == 1


# === Conversion Helper Tests ===


def test_build_discovered_agent_creates_correct_object() -> None:
    agent_id = AgentId.generate()
    agent_name = AgentName("my-agent")
    host_id = HostId.generate()
    provider_name = ProviderInstanceName("local")
    agent = build_discovered_agent(
        agent_id=agent_id,
        agent_name=agent_name,
        host_id=host_id,
        provider_name=provider_name,
    )
    assert agent.agent_id == agent_id
    assert agent.agent_name == agent_name
    assert agent.host_id == host_id
    assert agent.provider_name == provider_name


def test_discovered_agent_from_agent_details_preserves_key_fields() -> None:
    host_id = HostId.generate()
    provider_name = ProviderInstanceName("docker")
    details = _make_agent_details(host_id, provider_name)
    discovered = discovered_agent_from_agent_details(details)
    assert discovered.agent_id == details.id
    assert discovered.agent_name == details.name
    assert discovered.provider_name == provider_name
    assert discovered.certified_data["type"] == "claude"


def test_discovered_host_from_agent_details_preserves_key_fields() -> None:
    host_id = HostId.generate()
    provider_name = ProviderInstanceName("modal")
    details = _make_agent_details(host_id, provider_name)
    host = discovered_host_from_agent_details(details)
    assert host.host_id == host_id
    assert host.host_name == HostName("test-host")
    assert host.provider_name == provider_name


def test_extract_agents_and_hosts_deduplicates_hosts() -> None:
    host_id = HostId.generate()
    provider_name = ProviderInstanceName("local")
    details1 = _make_agent_details(host_id, provider_name)
    details2 = _make_agent_details(host_id, provider_name)
    agents, hosts = extract_agents_and_hosts_from_full_listing([details1, details2])
    assert len(agents) == 2
    assert len(hosts) == 1


# === File I/O Tests ===


def test_append_discovery_event_creates_dirs_and_writes(temp_config: MngConfig) -> None:
    agent = make_test_discovered_agent()
    event = make_agent_discovery_event(agent)
    append_discovery_event(temp_config, event)

    events_path = get_discovery_events_path(temp_config)
    assert events_path.exists()
    lines = events_path.read_text().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["type"] == DiscoveryEventType.AGENT_DISCOVERED


def test_append_discovery_event_appends_multiple_events(temp_config: MngConfig) -> None:
    for _ in range(3):
        event = make_agent_discovery_event(make_test_discovered_agent())
        append_discovery_event(temp_config, event)

    events_path = get_discovery_events_path(temp_config)
    lines = events_path.read_text().splitlines()
    assert len(lines) == 3


def test_emit_agent_discovered_writes_to_file(temp_config: MngConfig) -> None:
    agent = make_test_discovered_agent()
    emit_agent_discovered(temp_config, agent)

    events_path = get_discovery_events_path(temp_config)
    lines = events_path.read_text().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["agent"]["agent_name"] == str(agent.agent_name)


def test_emit_host_discovered_writes_to_file(temp_config: MngConfig) -> None:
    host = make_test_discovered_host()
    emit_host_discovered(temp_config, host)

    events_path = get_discovery_events_path(temp_config)
    lines = events_path.read_text().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["host"]["host_name"] == str(host.host_name)


def test_write_full_discovery_snapshot_writes_to_file(temp_config: MngConfig) -> None:
    agents = (make_test_discovered_agent(), make_test_discovered_agent())
    hosts = (make_test_discovered_host(),)
    returned_event = write_full_discovery_snapshot(temp_config, agents, hosts)

    events_path = get_discovery_events_path(temp_config)
    lines = events_path.read_text().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["type"] == DiscoveryEventType.DISCOVERY_FULL
    assert len(data["agents"]) == 2
    assert len(data["hosts"]) == 1
    assert returned_event.event_id == data["event_id"]


# === Parsing Tests ===


def test_parse_agent_discovery_event_round_trips() -> None:
    agent = make_test_discovered_agent()
    event = make_agent_discovery_event(agent)
    line = json.dumps(event.model_dump(mode="json"), separators=(",", ":"))
    parsed = parse_discovery_event_line(line)
    assert isinstance(parsed, AgentDiscoveryEvent)
    assert parsed.agent.agent_id == agent.agent_id


def test_parse_host_discovery_event_round_trips() -> None:
    host = make_test_discovered_host()
    event = make_host_discovery_event(host)
    line = json.dumps(event.model_dump(mode="json"), separators=(",", ":"))
    parsed = parse_discovery_event_line(line)
    assert isinstance(parsed, HostDiscoveryEvent)
    assert parsed.host.host_id == host.host_id


def test_parse_full_snapshot_event_round_trips() -> None:
    agents = (make_test_discovered_agent(),)
    hosts = (make_test_discovered_host(),)
    event = make_full_discovery_snapshot_event(agents, hosts)
    line = json.dumps(event.model_dump(mode="json"), separators=(",", ":"))
    parsed = parse_discovery_event_line(line)
    assert isinstance(parsed, FullDiscoverySnapshotEvent)
    assert len(parsed.agents) == 1
    assert len(parsed.hosts) == 1


def test_parse_empty_line_returns_none() -> None:
    assert parse_discovery_event_line("") is None
    assert parse_discovery_event_line("   ") is None


def test_parse_invalid_json_returns_none() -> None:
    assert parse_discovery_event_line("{invalid json}") is None


def test_parse_unknown_event_type_returns_none() -> None:
    assert parse_discovery_event_line('{"type": "unknown_event"}') is None


# === find_latest_full_snapshot_offset Tests ===


def test_find_latest_full_snapshot_offset_returns_zero_when_no_file(tmp_path: Path) -> None:
    assert find_latest_full_snapshot_offset(tmp_path / "nonexistent.jsonl") == 0


def test_find_latest_full_snapshot_offset_returns_zero_when_no_full_events(temp_config: MngConfig) -> None:
    # Write only agent events
    emit_agent_discovered(temp_config, make_test_discovered_agent())
    emit_agent_discovered(temp_config, make_test_discovered_agent())

    events_path = get_discovery_events_path(temp_config)
    assert find_latest_full_snapshot_offset(events_path) == 0


def test_find_latest_full_snapshot_offset_finds_last_full_event(temp_config: MngConfig) -> None:
    # Write: agent, full, agent, full, agent
    emit_agent_discovered(temp_config, make_test_discovered_agent())
    write_full_discovery_snapshot(temp_config, (make_test_discovered_agent(),), (make_test_discovered_host(),))
    emit_agent_discovered(temp_config, make_test_discovered_agent())
    write_full_discovery_snapshot(temp_config, (make_test_discovered_agent(),), (make_test_discovered_host(),))
    emit_agent_discovered(temp_config, make_test_discovered_agent())

    events_path = get_discovery_events_path(temp_config)
    offset = find_latest_full_snapshot_offset(events_path)

    # Read from the offset -- should get the second full event and the last agent event
    with open(events_path) as f:
        f.seek(offset)
        remaining_lines = [line.strip() for line in f if line.strip()]
    assert len(remaining_lines) == 2
    first_data = json.loads(remaining_lines[0])
    assert first_data["type"] == DiscoveryEventType.DISCOVERY_FULL
