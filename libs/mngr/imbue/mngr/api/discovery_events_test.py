import json
from pathlib import Path
from typing import cast

from imbue.imbue_common.event_envelope import EventType
from imbue.mngr.api.discovery_events import AgentDestroyedEvent
from imbue.mngr.api.discovery_events import AgentDiscoveryEvent
from imbue.mngr.api.discovery_events import DISCOVERY_EVENT_SOURCE
from imbue.mngr.api.discovery_events import DiscoveryEventType
from imbue.mngr.api.discovery_events import FullDiscoverySnapshotEvent
from imbue.mngr.api.discovery_events import HostDestroyedEvent
from imbue.mngr.api.discovery_events import HostDiscoveryEvent
from imbue.mngr.api.discovery_events import HostSSHInfoEvent
from imbue.mngr.api.discovery_events import _build_ssh_info_from_host
from imbue.mngr.api.discovery_events import _make_envelope_fields
from imbue.mngr.api.discovery_events import append_discovery_event
from imbue.mngr.api.discovery_events import discovered_agent_from_agent_details
from imbue.mngr.api.discovery_events import discovered_host_from_agent_details
from imbue.mngr.api.discovery_events import emit_agent_destroyed
from imbue.mngr.api.discovery_events import emit_agent_discovered
from imbue.mngr.api.discovery_events import emit_host_destroyed
from imbue.mngr.api.discovery_events import emit_host_discovered
from imbue.mngr.api.discovery_events import emit_host_ssh_info
from imbue.mngr.api.discovery_events import extract_agents_and_hosts_from_full_listing
from imbue.mngr.api.discovery_events import find_latest_full_snapshot_offset
from imbue.mngr.api.discovery_events import get_discovery_events_dir
from imbue.mngr.api.discovery_events import get_discovery_events_path
from imbue.mngr.api.discovery_events import make_agent_discovery_event
from imbue.mngr.api.discovery_events import make_full_discovery_snapshot_event
from imbue.mngr.api.discovery_events import make_host_discovery_event
from imbue.mngr.api.discovery_events import parse_discovery_event_line
from imbue.mngr.api.discovery_events import write_full_discovery_snapshot
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SSHInfo
from imbue.mngr.utils.testing import make_test_agent_details
from imbue.mngr.utils.testing import make_test_discovered_agent
from imbue.mngr.utils.testing import make_test_discovered_host

# === Path Helper Tests ===


def test_get_discovery_events_dir_returns_correct_path(temp_config: MngrConfig) -> None:
    events_dir = get_discovery_events_dir(temp_config)
    assert events_dir == temp_config.default_host_dir / "events" / "mngr" / "discovery"


def test_get_discovery_events_path_returns_jsonl_file(temp_config: MngrConfig) -> None:
    events_path = get_discovery_events_path(temp_config)
    assert events_path.name == "events.jsonl"
    assert events_path.parent.name == "discovery"


# === Event Construction Tests ===


def test_make_agent_discovery_event_has_correct_fields() -> None:
    agent = make_test_discovered_agent()
    event = make_agent_discovery_event(agent)
    assert event.type == DiscoveryEventType.AGENT_DISCOVERED
    assert event.source == "mngr/discovery"
    assert event.event_id.startswith("evt-")
    assert event.agent == agent


def test_make_host_discovery_event_has_correct_fields() -> None:
    host = make_test_discovered_host()
    event = make_host_discovery_event(host)
    assert event.type == DiscoveryEventType.HOST_DISCOVERED
    assert event.source == "mngr/discovery"
    assert event.event_id.startswith("evt-")
    assert event.host == host


def test_make_full_discovery_snapshot_event_has_correct_fields() -> None:
    agents = (make_test_discovered_agent(), make_test_discovered_agent())
    hosts = (make_test_discovered_host(),)
    event = make_full_discovery_snapshot_event(agents, hosts)
    assert event.type == DiscoveryEventType.DISCOVERY_FULL
    assert event.source == "mngr/discovery"
    assert len(event.agents) == 2
    assert len(event.hosts) == 1


# === Conversion Helper Tests ===


def test_discovered_agent_from_agent_details_preserves_key_fields() -> None:
    host_id = HostId.generate()
    provider_name = ProviderInstanceName("docker")
    details = make_test_agent_details(host_id=host_id, provider_name=provider_name)
    discovered = discovered_agent_from_agent_details(details)
    assert discovered.agent_id == details.id
    assert discovered.agent_name == details.name
    assert discovered.provider_name == provider_name
    assert discovered.certified_data["type"] == "generic"


def test_discovered_host_from_agent_details_preserves_key_fields() -> None:
    host_id = HostId.generate()
    provider_name = ProviderInstanceName("modal")
    details = make_test_agent_details(host_id=host_id, provider_name=provider_name)
    host = discovered_host_from_agent_details(details)
    assert host.host_id == host_id
    assert host.host_name == HostName("test-host")
    assert host.provider_name == provider_name


def test_extract_agents_and_hosts_returns_ssh_info() -> None:
    ssh = SSHInfo(
        user="root",
        host="remote.example.com",
        port=2222,
        key_path=Path("/tmp/key"),
        command="ssh -i /tmp/key -p 2222 root@remote.example.com",
    )
    host_id = HostId.generate()
    details = make_test_agent_details(host_id=host_id, provider_name=ProviderInstanceName("modal"), ssh=ssh)
    _, _, host_ssh_infos = extract_agents_and_hosts_from_full_listing([details])
    assert len(host_ssh_infos) == 1
    assert host_ssh_infos[0][0] == host_id
    assert host_ssh_infos[0][1].host == "remote.example.com"


def test_extract_agents_and_hosts_returns_empty_ssh_for_local() -> None:
    details = make_test_agent_details(provider_name=ProviderInstanceName("local"))
    _, _, host_ssh_infos = extract_agents_and_hosts_from_full_listing([details])
    assert len(host_ssh_infos) == 0


class _FakeHostWithSSH:
    """Minimal stub for testing _build_ssh_info_from_host with SSH info."""

    def get_ssh_connection_info(self) -> tuple[str, str, int, Path]:
        return ("root", "remote.example.com", 2222, Path("/tmp/key"))


class _FakeLocalHost:
    """Minimal stub for testing _build_ssh_info_from_host without SSH info."""

    def get_ssh_connection_info(self) -> None:
        return None


def test_build_ssh_info_from_host_returns_ssh_info_for_remote_host() -> None:
    result = _build_ssh_info_from_host(cast(OnlineHostInterface, _FakeHostWithSSH()))
    assert result is not None
    assert result.user == "root"
    assert result.host == "remote.example.com"
    assert result.port == 2222
    assert result.key_path == Path("/tmp/key")
    assert result.command == "ssh -i /tmp/key -p 2222 root@remote.example.com"


def test_build_ssh_info_from_host_returns_none_for_local_host() -> None:
    result = _build_ssh_info_from_host(cast(OnlineHostInterface, _FakeLocalHost()))
    assert result is None


def test_extract_agents_and_hosts_deduplicates_hosts() -> None:
    host_id = HostId.generate()
    provider_name = ProviderInstanceName("local")
    details1 = make_test_agent_details(host_id=host_id, provider_name=provider_name)
    details2 = make_test_agent_details(host_id=host_id, provider_name=provider_name)
    agents, hosts, _ = extract_agents_and_hosts_from_full_listing([details1, details2])
    assert len(agents) == 2
    assert len(hosts) == 1


# === File I/O Tests ===


def test_append_discovery_event_creates_dirs_and_writes(temp_config: MngrConfig) -> None:
    agent = make_test_discovered_agent()
    event = make_agent_discovery_event(agent)
    append_discovery_event(temp_config, event)

    events_path = get_discovery_events_path(temp_config)
    assert events_path.exists()
    lines = events_path.read_text().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["type"] == DiscoveryEventType.AGENT_DISCOVERED


def test_append_discovery_event_appends_multiple_events(temp_config: MngrConfig) -> None:
    for _ in range(3):
        event = make_agent_discovery_event(make_test_discovered_agent())
        append_discovery_event(temp_config, event)

    events_path = get_discovery_events_path(temp_config)
    lines = events_path.read_text().splitlines()
    assert len(lines) == 3


def test_emit_agent_discovered_writes_to_file(temp_config: MngrConfig) -> None:
    agent = make_test_discovered_agent()
    emit_agent_discovered(temp_config, agent)

    events_path = get_discovery_events_path(temp_config)
    lines = events_path.read_text().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["agent"]["agent_name"] == str(agent.agent_name)


def test_emit_host_discovered_writes_to_file(temp_config: MngrConfig) -> None:
    host = make_test_discovered_host()
    emit_host_discovered(temp_config, host)

    events_path = get_discovery_events_path(temp_config)
    lines = events_path.read_text().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["host"]["host_name"] == str(host.host_name)


def test_write_full_discovery_snapshot_writes_to_file(temp_config: MngrConfig) -> None:
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


def test_find_latest_full_snapshot_offset_returns_zero_when_no_full_events(temp_config: MngrConfig) -> None:
    # Write only agent events
    emit_agent_discovered(temp_config, make_test_discovered_agent())
    emit_agent_discovered(temp_config, make_test_discovered_agent())

    events_path = get_discovery_events_path(temp_config)
    assert find_latest_full_snapshot_offset(events_path) == 0


def test_find_latest_full_snapshot_offset_finds_last_full_event(temp_config: MngrConfig) -> None:
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


# === Destroy Event Tests ===


def test_emit_agent_destroyed_writes_to_file(temp_config: MngrConfig) -> None:
    agent_id = AgentId.generate()
    host_id = HostId.generate()
    emit_agent_destroyed(temp_config, agent_id, host_id)

    events_path = get_discovery_events_path(temp_config)
    lines = events_path.read_text().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["type"] == DiscoveryEventType.AGENT_DESTROYED
    assert data["agent_id"] == str(agent_id)
    assert data["host_id"] == str(host_id)


def test_emit_host_destroyed_writes_to_file(temp_config: MngrConfig) -> None:
    host_id = HostId.generate()
    agent_ids = (AgentId.generate(), AgentId.generate())
    emit_host_destroyed(temp_config, host_id, agent_ids)

    events_path = get_discovery_events_path(temp_config)
    lines = events_path.read_text().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["type"] == DiscoveryEventType.HOST_DESTROYED
    assert data["host_id"] == str(host_id)
    assert len(data["agent_ids"]) == 2


def test_parse_agent_destroyed_event_round_trips() -> None:
    agent_id = AgentId.generate()
    host_id = HostId.generate()
    timestamp, event_id = _make_envelope_fields()
    event = AgentDestroyedEvent(
        timestamp=timestamp,
        type=EventType(DiscoveryEventType.AGENT_DESTROYED),
        event_id=event_id,
        source=DISCOVERY_EVENT_SOURCE,
        agent_id=agent_id,
        host_id=host_id,
    )
    line = json.dumps(event.model_dump(mode="json"), separators=(",", ":"))
    parsed = parse_discovery_event_line(line)
    assert isinstance(parsed, AgentDestroyedEvent)
    assert parsed.agent_id == agent_id


def test_parse_host_destroyed_event_round_trips() -> None:
    host_id = HostId.generate()
    agent_ids = (AgentId.generate(),)
    timestamp, event_id = _make_envelope_fields()
    event = HostDestroyedEvent(
        timestamp=timestamp,
        type=EventType(DiscoveryEventType.HOST_DESTROYED),
        event_id=event_id,
        source=DISCOVERY_EVENT_SOURCE,
        host_id=host_id,
        agent_ids=agent_ids,
    )
    line = json.dumps(event.model_dump(mode="json"), separators=(",", ":"))
    parsed = parse_discovery_event_line(line)
    assert isinstance(parsed, HostDestroyedEvent)
    assert parsed.host_id == host_id
    assert len(parsed.agent_ids) == 1


# === HOST_SSH_INFO Event Tests ===


def test_emit_host_ssh_info_writes_to_file(temp_config: MngrConfig) -> None:
    host_id = HostId.generate()
    ssh = SSHInfo(
        user="root",
        host="remote.example.com",
        port=2222,
        key_path=Path("/tmp/key"),
        command="ssh -i /tmp/key -p 2222 root@remote.example.com",
    )
    emit_host_ssh_info(temp_config, host_id, ssh)

    events_path = get_discovery_events_path(temp_config)
    lines = events_path.read_text().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["type"] == DiscoveryEventType.HOST_SSH_INFO
    assert data["host_id"] == str(host_id)
    assert data["ssh"]["host"] == "remote.example.com"
    assert data["ssh"]["port"] == 2222


def test_parse_host_ssh_info_event_round_trips() -> None:
    host_id = HostId.generate()
    ssh = SSHInfo(
        user="root",
        host="remote.example.com",
        port=2222,
        key_path=Path("/tmp/key"),
        command="ssh -i /tmp/key -p 2222 root@remote.example.com",
    )
    timestamp, event_id = _make_envelope_fields()
    event = HostSSHInfoEvent(
        timestamp=timestamp,
        type=EventType(DiscoveryEventType.HOST_SSH_INFO),
        event_id=event_id,
        source=DISCOVERY_EVENT_SOURCE,
        host_id=host_id,
        ssh=ssh,
    )
    line = json.dumps(event.model_dump(mode="json"), separators=(",", ":"))
    parsed = parse_discovery_event_line(line)
    assert isinstance(parsed, HostSSHInfoEvent)
    assert parsed.host_id == host_id
    assert parsed.ssh.host == "remote.example.com"
    assert parsed.ssh.port == 2222
    assert parsed.ssh.key_path == Path("/tmp/key")
