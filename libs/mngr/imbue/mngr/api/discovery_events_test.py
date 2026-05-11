import json
import threading
from pathlib import Path
from threading import Lock
from typing import cast

import pytest

from imbue.mngr.api.discovery_events import AgentDestroyedEvent
from imbue.mngr.api.discovery_events import AgentDiscoveryEvent
from imbue.mngr.api.discovery_events import DISCOVERY_EVENT_SOURCE
from imbue.mngr.api.discovery_events import DiscoveryErrorEvent
from imbue.mngr.api.discovery_events import DiscoveryEventType
from imbue.mngr.api.discovery_events import FullDiscoverySnapshotEvent
from imbue.mngr.api.discovery_events import HostDestroyedEvent
from imbue.mngr.api.discovery_events import HostDiscoveryEvent
from imbue.mngr.api.discovery_events import HostSSHInfoEvent
from imbue.mngr.api.discovery_events import _DISCOVERY_MAX_FILE_SIZE_BYTES
from imbue.mngr.api.discovery_events import _build_ssh_info_from_host
from imbue.mngr.api.discovery_events import _discovery_stream_emit_line
from imbue.mngr.api.discovery_events import _discovery_stream_tail_events_file
from imbue.mngr.api.discovery_events import _emit_lines_from_offset
from imbue.mngr.api.discovery_events import _make_envelope_fields
from imbue.mngr.api.discovery_events import _rotate_discovery_events_if_needed
from imbue.mngr.api.discovery_events import append_discovery_event
from imbue.mngr.api.discovery_events import discovered_agent_from_agent_details
from imbue.mngr.api.discovery_events import discovered_host_from_agent_details
from imbue.mngr.api.discovery_events import emit_agent_destroyed
from imbue.mngr.api.discovery_events import emit_agent_discovered
from imbue.mngr.api.discovery_events import emit_discovery_error_event
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
from imbue.mngr.api.discovery_events import resolve_provider_names_for_identifiers
from imbue.mngr.api.discovery_events import write_full_discovery_snapshot
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import DiscoverySchemaChangedError
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SSHInfo
from imbue.mngr.utils.jsonl_warn import MalformedJsonLineWarner
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.testing import capture_loguru
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


def test_emit_discovery_error_event_round_trips_provider_name(temp_config: MngrConfig) -> None:
    """Provider-attributable errors must carry the offending provider name through.

    Minds' auto-disable wiring keys off this field; without it the
    consumer would have to pattern-match the error message to figure out
    which ``[providers.imbue_cloud_<slug>]`` block to disable.
    """
    emit_discovery_error_event(
        temp_config,
        error_type="ImbueCloudAuthError",
        error_message="token theft detected",
        source_name="discovery_poll",
        provider_name="imbue_cloud_alice-example-com",
    )
    events_path = get_discovery_events_path(temp_config)
    lines = events_path.read_text().splitlines()
    assert len(lines) == 1
    parsed = parse_discovery_event_line(lines[0])
    assert isinstance(parsed, DiscoveryErrorEvent)
    assert parsed.provider_name == "imbue_cloud_alice-example-com"
    assert parsed.error_type == "ImbueCloudAuthError"


def test_emit_discovery_error_event_provider_name_defaults_to_none(temp_config: MngrConfig) -> None:
    """Errors not attributable to a single provider (e.g. snapshot-level
    failures) leave ``provider_name`` unset.
    """
    emit_discovery_error_event(
        temp_config,
        error_type="RuntimeError",
        error_message="something else broke",
        source_name="discovery_snapshot",
    )
    events_path = get_discovery_events_path(temp_config)
    parsed = parse_discovery_event_line(events_path.read_text().splitlines()[0])
    assert isinstance(parsed, DiscoveryErrorEvent)
    assert parsed.provider_name is None


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


def test_parse_invalid_json_raises() -> None:
    """Malformed JSON is treated as an upstream bug; the parser surfaces the JSONDecodeError."""
    with pytest.raises(json.JSONDecodeError):
        parse_discovery_event_line("{invalid json}")


def test_parse_unknown_event_type_raises_schema_changed() -> None:
    """A discovery line with a type that isn't in the discriminated union raises DiscoverySchemaChangedError."""
    with pytest.raises(DiscoverySchemaChangedError):
        parse_discovery_event_line('{"type": "unknown_event"}')


def test_parse_recognized_event_with_missing_field_raises_schema_changed() -> None:
    """A line of a known event type that fails validation must raise DiscoverySchemaChangedError."""
    # AGENT_DISCOVERED requires an "agent" field; omit it to simulate a schema mismatch.
    line = json.dumps(
        {
            "timestamp": "2025-01-01T00:00:00.000000000+00:00",
            "type": DiscoveryEventType.AGENT_DISCOVERED,
            "event_id": "evt-test",
            "source": "mngr/discovery",
        }
    )
    with pytest.raises(DiscoverySchemaChangedError) as exc_info:
        parse_discovery_event_line(line)
    assert exc_info.value.event_type == DiscoveryEventType.AGENT_DISCOVERED


def test_parse_recognized_event_with_extra_field_raises_schema_changed() -> None:
    """Discovery models use extra='forbid', so unexpected fields must raise DiscoverySchemaChangedError."""
    agent = make_test_discovered_agent()
    event = make_agent_discovery_event(agent)
    data = event.model_dump(mode="json")
    data["unexpected_new_field"] = "value-from-future-schema"
    with pytest.raises(DiscoverySchemaChangedError):
        parse_discovery_event_line(json.dumps(data))


@pytest.mark.allow_warnings(match=r"Discovery event schema mismatch")
def test_resolve_provider_names_recovers_after_schema_mismatch(temp_mngr_ctx: MngrContext) -> None:
    """A stale-schema event must trigger a regenerate (full scan) and a parse retry.

    After the regenerate, the on-disk file has a fresh DISCOVERY_FULL snapshot in the
    current schema; replaying from the new offset succeeds. The stub local-only
    provider has no agents, so resolution returns None, but the key assertion is that
    no exception escapes -- the recovery path ran and parsing succeeded on retry.
    """
    config = temp_mngr_ctx.config
    # Seed with a valid full snapshot, then append a stale-schema agent-discovery event.
    agent = DiscoveredAgent(
        host_id=HostId.generate(),
        agent_id=AgentId.generate(),
        agent_name=AgentName("known-agent"),
        provider_name=ProviderInstanceName("local"),
        certified_data={},
    )
    write_full_discovery_snapshot(config, [agent], [])

    events_path = get_discovery_events_path(config)
    pre_recovery_size = events_path.stat().st_size
    with open(events_path, "a") as f:
        stale_line = json.dumps(
            {
                "timestamp": "2025-01-01T00:00:00.000000000+00:00",
                "type": DiscoveryEventType.AGENT_DISCOVERED,
                "event_id": "evt-stale",
                "source": "mngr/discovery",
                # Missing required "agent" field -- simulates schema evolution.
            }
        )
        f.write(stale_line + "\n")

    result = resolve_provider_names_for_identifiers(temp_mngr_ctx, ["known-agent"])

    # The regenerate path appended a fresh DISCOVERY_FULL snapshot past the stale line.
    final_lines = events_path.read_text().splitlines()
    last_event = json.loads(final_lines[-1])
    assert last_event["type"] == DiscoveryEventType.DISCOVERY_FULL
    assert events_path.stat().st_size > pre_recovery_size
    # The retry parsed against the fresh snapshot, which has no agents from the
    # stub provider setup, so the seeded "known-agent" is not in the post-recovery
    # state and resolution returns None.
    assert result is None


# === find_latest_full_snapshot_offset Tests ===


def test_find_latest_full_snapshot_offset_returns_zero_when_no_file(tmp_path: Path) -> None:
    assert find_latest_full_snapshot_offset(tmp_path / "nonexistent.jsonl") == 0


def test_find_latest_full_snapshot_offset_returns_zero_when_no_full_events(temp_config: MngrConfig) -> None:
    # Write only agent events
    emit_agent_discovered(temp_config, make_test_discovered_agent())
    emit_agent_discovered(temp_config, make_test_discovered_agent())

    events_path = get_discovery_events_path(temp_config)
    assert find_latest_full_snapshot_offset(events_path) == 0


def test_find_latest_full_snapshot_offset_warns_on_mid_file_corruption(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    # A leading agent event, then a valid full snapshot, then a corrupt line,
    # then a trailing agent event. The corrupt line is followed by more data,
    # so a warning should be emitted. The leading event ensures the snapshot
    # offset is non-zero, so the assertion distinguishes "snapshot located"
    # from "no snapshot found, fallback to 0".
    leading_agent = (
        '{"timestamp":"2026-01-01T00:00:00Z","type":"AGENT_DISCOVERED","event_id":"evt-w",'
        '"source":"mngr/discovery","agent":{}}'
    )
    valid_full = (
        '{"timestamp":"2026-01-02T00:00:00Z","type":"DISCOVERY_FULL","event_id":"evt-x",'
        '"source":"mngr/discovery","agents":[],"hosts":[]}'
    )
    valid_agent = (
        '{"timestamp":"2026-01-03T00:00:00Z","type":"AGENT_DISCOVERED","event_id":"evt-y",'
        '"source":"mngr/discovery","agent":{}}'
    )
    leading_line = f"{leading_agent}\n"
    events_path.write_text(f"{leading_line}{valid_full}\nthis is not json {{{{\n{valid_agent}\n")

    with capture_loguru(level="WARNING") as log_output:
        offset = find_latest_full_snapshot_offset(events_path)

    # The snapshot starts immediately after the leading line, so its byte offset
    # equals the byte length of the leading line.
    assert offset == len(leading_line.encode("utf-8"))
    assert "Skipped corrupt JSONL line" in log_output.getvalue()


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


# === resolve_provider_names_for_identifiers Tests ===


def test_resolve_provider_names_returns_none_when_no_file(temp_mngr_ctx: MngrContext) -> None:
    """Should return None when the events file does not exist."""
    result = resolve_provider_names_for_identifiers(temp_mngr_ctx, ["my-agent"])
    assert result is None


def test_resolve_provider_names_resolves_by_agent_name(temp_mngr_ctx: MngrContext) -> None:
    """Should resolve an agent name to its provider from a full snapshot."""
    agent = DiscoveredAgent(
        host_id=HostId.generate(),
        agent_id=AgentId.generate(),
        agent_name=AgentName("my-agent"),
        provider_name=ProviderInstanceName("docker"),
        certified_data={},
    )
    host = DiscoveredHost(
        host_id=agent.host_id,
        host_name=HostName("docker-host"),
        provider_name=ProviderInstanceName("docker"),
    )
    write_full_discovery_snapshot(temp_mngr_ctx.config, [agent], [host])

    result = resolve_provider_names_for_identifiers(temp_mngr_ctx, ["my-agent"])
    assert result == ("docker",)


def test_resolve_provider_names_resolves_by_agent_id(temp_mngr_ctx: MngrContext) -> None:
    """Should resolve an agent ID to its provider from a full snapshot."""
    agent_id = AgentId.generate()
    agent = DiscoveredAgent(
        host_id=HostId.generate(),
        agent_id=agent_id,
        agent_name=AgentName("some-agent"),
        provider_name=ProviderInstanceName("modal"),
        certified_data={},
    )
    write_full_discovery_snapshot(temp_mngr_ctx.config, [agent], [])

    result = resolve_provider_names_for_identifiers(temp_mngr_ctx, [str(agent_id)])
    assert result == ("modal",)


def test_resolve_provider_names_returns_none_for_unknown_identifier(temp_mngr_ctx: MngrContext) -> None:
    """Should return None when any identifier cannot be resolved."""
    agent = DiscoveredAgent(
        host_id=HostId.generate(),
        agent_id=AgentId.generate(),
        agent_name=AgentName("known-agent"),
        provider_name=ProviderInstanceName("local"),
        certified_data={},
    )
    write_full_discovery_snapshot(temp_mngr_ctx.config, [agent], [])

    result = resolve_provider_names_for_identifiers(temp_mngr_ctx, ["unknown-agent"])
    assert result is None


def test_resolve_provider_names_returns_none_when_any_identifier_missing(temp_mngr_ctx: MngrContext) -> None:
    """Should return None when even one identifier is unknown (partial match is not enough)."""
    agent = DiscoveredAgent(
        host_id=HostId.generate(),
        agent_id=AgentId.generate(),
        agent_name=AgentName("known-agent"),
        provider_name=ProviderInstanceName("local"),
        certified_data={},
    )
    write_full_discovery_snapshot(temp_mngr_ctx.config, [agent], [])

    result = resolve_provider_names_for_identifiers(temp_mngr_ctx, ["known-agent", "unknown-agent"])
    assert result is None


def test_resolve_provider_names_deduplicates_providers(temp_mngr_ctx: MngrContext) -> None:
    """Should deduplicate provider names when multiple agents share a provider."""
    agent1 = DiscoveredAgent(
        host_id=HostId.generate(),
        agent_id=AgentId.generate(),
        agent_name=AgentName("agent-a"),
        provider_name=ProviderInstanceName("docker"),
        certified_data={},
    )
    agent2 = DiscoveredAgent(
        host_id=HostId.generate(),
        agent_id=AgentId.generate(),
        agent_name=AgentName("agent-b"),
        provider_name=ProviderInstanceName("docker"),
        certified_data={},
    )
    write_full_discovery_snapshot(temp_mngr_ctx.config, [agent1, agent2], [])

    result = resolve_provider_names_for_identifiers(temp_mngr_ctx, ["agent-a", "agent-b"])
    assert result == ("docker",)


def test_resolve_provider_names_unions_providers_for_multiple_agents(temp_mngr_ctx: MngrContext) -> None:
    """Should return the union of providers when agents are on different providers."""
    agent1 = DiscoveredAgent(
        host_id=HostId.generate(),
        agent_id=AgentId.generate(),
        agent_name=AgentName("local-agent"),
        provider_name=ProviderInstanceName("local"),
        certified_data={},
    )
    agent2 = DiscoveredAgent(
        host_id=HostId.generate(),
        agent_id=AgentId.generate(),
        agent_name=AgentName("docker-agent"),
        provider_name=ProviderInstanceName("docker"),
        certified_data={},
    )
    write_full_discovery_snapshot(temp_mngr_ctx.config, [agent1, agent2], [])

    result = resolve_provider_names_for_identifiers(temp_mngr_ctx, ["local-agent", "docker-agent"])
    assert result is not None
    assert set(result) == {"local", "docker"}


def test_resolve_provider_names_handles_same_name_on_multiple_providers(temp_mngr_ctx: MngrContext) -> None:
    """When the same agent name exists on multiple providers, should return all of them."""
    agent1 = DiscoveredAgent(
        host_id=HostId.generate(),
        agent_id=AgentId.generate(),
        agent_name=AgentName("shared-name"),
        provider_name=ProviderInstanceName("local"),
        certified_data={},
    )
    agent2 = DiscoveredAgent(
        host_id=HostId.generate(),
        agent_id=AgentId.generate(),
        agent_name=AgentName("shared-name"),
        provider_name=ProviderInstanceName("docker"),
        certified_data={},
    )
    write_full_discovery_snapshot(temp_mngr_ctx.config, [agent1, agent2], [])

    result = resolve_provider_names_for_identifiers(temp_mngr_ctx, ["shared-name"])
    assert result is not None
    assert set(result) == {"local", "docker"}


def test_resolve_provider_names_replays_incremental_events(temp_mngr_ctx: MngrContext) -> None:
    """Should pick up agents added via incremental events after the snapshot."""
    # Start with a snapshot containing one agent
    agent1 = DiscoveredAgent(
        host_id=HostId.generate(),
        agent_id=AgentId.generate(),
        agent_name=AgentName("old-agent"),
        provider_name=ProviderInstanceName("local"),
        certified_data={},
    )
    write_full_discovery_snapshot(temp_mngr_ctx.config, [agent1], [])

    # Add a new agent via an incremental event
    new_agent = DiscoveredAgent(
        host_id=HostId.generate(),
        agent_id=AgentId.generate(),
        agent_name=AgentName("new-agent"),
        provider_name=ProviderInstanceName("docker"),
        certified_data={},
    )
    emit_agent_discovered(temp_mngr_ctx.config, new_agent)

    result = resolve_provider_names_for_identifiers(temp_mngr_ctx, ["new-agent"])
    assert result == ("docker",)


def test_resolve_provider_names_respects_destroy_events_by_id(temp_mngr_ctx: MngrContext) -> None:
    """Should not resolve destroyed agents by ID."""
    agent_id = AgentId.generate()
    host_id = HostId.generate()
    agent = DiscoveredAgent(
        host_id=host_id,
        agent_id=agent_id,
        agent_name=AgentName("destroyed-agent"),
        provider_name=ProviderInstanceName("local"),
        certified_data={},
    )
    write_full_discovery_snapshot(temp_mngr_ctx.config, [agent], [])
    emit_agent_destroyed(temp_mngr_ctx.config, agent_id, host_id)

    # By ID should fail (destroyed)
    result = resolve_provider_names_for_identifiers(temp_mngr_ctx, [str(agent_id)])
    assert result is None


def test_resolve_provider_names_respects_destroy_events_by_name(temp_mngr_ctx: MngrContext) -> None:
    """Should not resolve destroyed agents by name."""
    agent_id = AgentId.generate()
    host_id = HostId.generate()
    agent = DiscoveredAgent(
        host_id=host_id,
        agent_id=agent_id,
        agent_name=AgentName("destroyed-agent"),
        provider_name=ProviderInstanceName("local"),
        certified_data={},
    )
    write_full_discovery_snapshot(temp_mngr_ctx.config, [agent], [])
    emit_agent_destroyed(temp_mngr_ctx.config, agent_id, host_id)

    # By name should also fail (destroyed)
    result = resolve_provider_names_for_identifiers(temp_mngr_ctx, ["destroyed-agent"])
    assert result is None


def test_resolve_provider_names_with_no_snapshot_only_incremental(temp_mngr_ctx: MngrContext) -> None:
    """Should work with only incremental events (no full snapshot)."""
    agent = DiscoveredAgent(
        host_id=HostId.generate(),
        agent_id=AgentId.generate(),
        agent_name=AgentName("incremental-agent"),
        provider_name=ProviderInstanceName("modal"),
        certified_data={},
    )
    emit_agent_discovered(temp_mngr_ctx.config, agent)

    result = resolve_provider_names_for_identifiers(temp_mngr_ctx, ["incremental-agent"])
    assert result == ("modal",)


# === Discovery Stream Tests ===


def test_discovery_stream_emit_line_emits_valid_json_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    emitted_ids: set[str] = set()
    lock = Lock()
    warner = MalformedJsonLineWarner(source_description="test")
    event = make_agent_discovery_event(make_test_discovered_agent())
    line = json.dumps(event.model_dump(mode="json"))

    _discovery_stream_emit_line(line, warner, emitted_ids, lock, None)

    captured = capsys.readouterr()
    assert captured.out.strip()
    parsed = json.loads(captured.out.strip())
    assert parsed["type"] == DiscoveryEventType.AGENT_DISCOVERED


def test_discovery_stream_emit_line_deduplicates_by_event_id(capsys: pytest.CaptureFixture[str]) -> None:
    emitted_ids: set[str] = set()
    lock = Lock()
    warner = MalformedJsonLineWarner(source_description="test")
    event = make_agent_discovery_event(make_test_discovered_agent())
    line = json.dumps(event.model_dump(mode="json"))

    # Emit the same event twice
    _discovery_stream_emit_line(line, warner, emitted_ids, lock, None)
    _discovery_stream_emit_line(line, warner, emitted_ids, lock, None)

    captured = capsys.readouterr()
    # Only one line should be emitted
    output_lines = [ln for ln in captured.out.splitlines() if ln.strip()]
    assert len(output_lines) == 1


def test_discovery_stream_emit_line_skips_empty_lines(capsys: pytest.CaptureFixture[str]) -> None:
    emitted_ids: set[str] = set()
    lock = Lock()
    warner = MalformedJsonLineWarner(source_description="test")

    _discovery_stream_emit_line("", warner, emitted_ids, lock, None)
    _discovery_stream_emit_line("   ", warner, emitted_ids, lock, None)

    captured = capsys.readouterr()
    assert captured.out == ""


def test_discovery_stream_emit_line_skips_invalid_json(capsys: pytest.CaptureFixture[str]) -> None:
    emitted_ids: set[str] = set()
    lock = Lock()
    warner = MalformedJsonLineWarner(source_description="test")

    _discovery_stream_emit_line("{invalid json}", warner, emitted_ids, lock, None)

    captured = capsys.readouterr()
    assert captured.out == ""


def test_discovery_stream_emit_line_warns_on_mid_stream_corruption() -> None:
    emitted_ids: set[str] = set()
    lock = Lock()
    warner = MalformedJsonLineWarner(source_description="test stream")
    event = make_agent_discovery_event(make_test_discovered_agent())
    valid_line = json.dumps(event.model_dump(mode="json"))

    with capture_loguru(level="WARNING") as log_output:
        # Buffered: not yet flushed
        _discovery_stream_emit_line("garbage{", warner, emitted_ids, lock, lambda _: None)
        # Subsequent valid line proves the malformed line was not at EOF
        _discovery_stream_emit_line(valid_line, warner, emitted_ids, lock, lambda _: None)
    assert "Skipped corrupt JSONL line in test stream" in log_output.getvalue()


def test_discovery_stream_emit_line_uses_callback_when_provided() -> None:
    emitted_ids: set[str] = set()
    lock = Lock()
    warner = MalformedJsonLineWarner(source_description="test")
    event = make_agent_discovery_event(make_test_discovered_agent())
    line = json.dumps(event.model_dump(mode="json"))
    received_lines: list[str] = []

    _discovery_stream_emit_line(line, warner, emitted_ids, lock, received_lines.append)

    assert len(received_lines) == 1
    parsed = json.loads(received_lines[0])
    assert parsed["type"] == DiscoveryEventType.AGENT_DISCOVERED


def test_discovery_stream_tail_detects_new_content(temp_config: MngrConfig) -> None:
    events_path = get_discovery_events_path(temp_config)

    # Write an initial event
    emit_agent_discovered(temp_config, make_test_discovered_agent())
    initial_offset = events_path.stat().st_size

    emitted_ids: set[str] = set()
    lock = Lock()
    stop_event = threading.Event()
    captured_lines: list[str] = []
    warner = MalformedJsonLineWarner(source_description="test")

    # Start tail thread with on_line callback instead of manipulating sys.stdout
    tail = threading.Thread(
        target=_discovery_stream_tail_events_file,
        args=(events_path, initial_offset, stop_event, emitted_ids, lock, warner, captured_lines.append),
        daemon=True,
    )
    tail.start()

    # Write a new event while the tail is running
    emit_agent_discovered(temp_config, make_test_discovered_agent())

    # Poll until the tail thread picks up the new event
    poll_until(lambda: len(captured_lines) >= 1, timeout=5.0)

    stop_event.set()
    tail.join(timeout=5.0)

    # The tail should have picked up the new event
    assert len(captured_lines) == 1


def test_discovery_stream_tail_preserves_partial_writes(tmp_path: Path) -> None:
    """Regression test: the tail loop must not advance past a partial-write line.

    Before the fix, a poll that ended in a mid-flush partial line would parse
    the partial as malformed JSON and advance byte_offset past it; the rest of
    that line, written later, was never re-read and was silently lost.
    """
    events_path = tmp_path / "events.jsonl"
    events_path.touch()

    captured_lines: list[str] = []
    stop_event = threading.Event()
    emitted_ids: set[str] = set()
    lock = Lock()
    warner = MalformedJsonLineWarner(source_description="test partial")

    tail = threading.Thread(
        target=_discovery_stream_tail_events_file,
        args=(events_path, 0, stop_event, emitted_ids, lock, warner, captured_lines.append),
        daemon=True,
    )
    tail.start()

    event_1 = make_agent_discovery_event(make_test_discovered_agent())
    event_2 = make_agent_discovery_event(make_test_discovered_agent())
    line_1 = json.dumps(event_1.model_dump(mode="json")) + "\n"
    line_2 = json.dumps(event_2.model_dump(mode="json")) + "\n"
    split_at = len(line_2) // 2
    partial_2 = line_2[:split_at]
    rest_2 = line_2[split_at:]

    try:
        # First write: a complete line followed by half of the second line (no trailing newline).
        with open(events_path, "w") as f:
            f.write(line_1 + partial_2)

        poll_until(lambda: len(captured_lines) >= 1, timeout=5.0)

        # Now flush the rest of the second line.
        with open(events_path, "a") as f:
            f.write(rest_2)

        poll_until(lambda: len(captured_lines) >= 2, timeout=5.0)
    finally:
        stop_event.set()
        tail.join(timeout=5.0)

    assert len(captured_lines) == 2
    parsed_ids = {json.loads(line)["event_id"] for line in captured_lines}
    assert parsed_ids == {str(event_1.event_id), str(event_2.event_id)}


def test_emit_lines_from_offset_warns_on_corruption_across_calls(tmp_path: Path) -> None:
    """Regression test: a single shared warner across phase reads must surface
    mid-file corruption that straddles phase boundaries.

    Before the fix, run_discovery_stream used a fresh MalformedJsonLineWarner
    for each synchronous phase, so a malformed line at the end of phase 1's
    read window was buffered, then silently discarded when phase 1 ended -- no
    warning fired even when phase 3 (or the tail) later read valid data after it.
    """
    events_path = tmp_path / "events.jsonl"
    valid_full = (
        '{"timestamp":"2026-01-01T00:00:00Z","type":"DISCOVERY_FULL","event_id":"evt-x",'
        '"source":"mngr/discovery","agents":[],"hosts":[]}'
    )
    valid_agent = (
        '{"timestamp":"2026-01-02T00:00:00Z","type":"AGENT_DISCOVERED","event_id":"evt-y",'
        '"source":"mngr/discovery","agent":{}}'
    )
    # Phase 1 input: valid snapshot then a malformed line at the end of the read window.
    events_path.write_text(f"{valid_full}\nthis is not json {{{{\n")

    warner = MalformedJsonLineWarner(source_description=f"discovery events file '{events_path}'")
    emitted_ids: set[str] = set()
    lock = Lock()
    captured: list[str] = []

    with capture_loguru(level="WARNING") as log_output:
        # Phase 1: read from start to current EOF.
        _emit_lines_from_offset(events_path, 0, warner, emitted_ids, lock, captured.append)
        # The malformed line is buffered; nothing has flushed it yet.
        assert "Skipped corrupt JSONL line" not in log_output.getvalue()

        # Simulate data appended between phases (e.g. by the background sync).
        with open(events_path, "a") as f:
            f.write(f"{valid_agent}\n")

        # Phase 3 re-reads from the same offset after the sync. With a shared
        # warner, the buffered malformed line gets flushed when this read sees
        # the new valid line.
        _emit_lines_from_offset(events_path, 0, warner, emitted_ids, lock, captured.append)

    assert "Skipped corrupt JSONL line" in log_output.getvalue()


def test_emit_lines_from_offset_holds_back_partial_last_line(tmp_path: Path) -> None:
    """Regression test: a partial trailing line at the time of phase-1 read must be
    held back so the tail thread can re-read it in one piece once the writer flushes.

    Before the fix, _emit_lines_from_offset used Python's text-mode line iterator,
    which yields a trailing partial line. The partial got buffered in the warner
    as malformed, the returned offset advanced past it, the tail thread started at
    the post-partial position, and when the writer flushed the rest the tail saw
    only the suffix -- losing the event and producing two misleading mid-file-
    corruption warnings about its two halves.
    """
    events_path = tmp_path / "events.jsonl"
    event_1 = make_agent_discovery_event(make_test_discovered_agent())
    event_2 = make_agent_discovery_event(make_test_discovered_agent())
    line_1 = json.dumps(event_1.model_dump(mode="json")) + "\n"
    line_2 = json.dumps(event_2.model_dump(mode="json")) + "\n"
    split_at = len(line_2) // 2
    partial_2 = line_2[:split_at]
    rest_2 = line_2[split_at:]
    events_path.write_text(line_1 + partial_2)

    warner = MalformedJsonLineWarner(source_description=f"discovery events file '{events_path}'")
    emitted_ids: set[str] = set()
    lock = Lock()
    captured: list[str] = []

    with capture_loguru(level="WARNING") as log_output:
        # Phase 1: should consume only line_1 and hold back the partial.
        consumed_offset = _emit_lines_from_offset(events_path, 0, warner, emitted_ids, lock, captured.append)

        # Writer flushes the rest of line_2.
        with open(events_path, "a") as f:
            f.write(rest_2)

        # Tail-equivalent read from the consumed_offset must reconstruct line_2.
        with open(events_path, "rb") as f:
            f.seek(consumed_offset)
            new_content = f.read().decode("utf-8")
        # The remainder must contain the full reconstructed line_2 (partial + rest)
        # exactly once -- not just the rest_2 suffix.
        assert new_content == partial_2 + rest_2

    # No false mid-file-corruption warnings about the partial line should fire.
    assert "Skipped corrupt JSONL line" not in log_output.getvalue()
    # Phase 1 emitted exactly one event (line_1).
    assert len(captured) == 1
    assert json.loads(captured[0])["event_id"] == str(event_1.event_id)


# === Discovery Event Rotation Tests ===


def test_rotate_discovery_events_does_nothing_when_file_is_small(tmp_path: Path) -> None:
    """Rotation should not trigger when the file is below the size threshold."""
    events_path = tmp_path / "events.jsonl"
    events_path.write_text('{"type":"test"}\n')

    _rotate_discovery_events_if_needed(events_path)

    # File should still exist and no rotated files should be created
    assert events_path.exists()
    rotated = [f for f in tmp_path.iterdir() if f.name.startswith("events.jsonl.")]
    assert len(rotated) == 0


def test_rotate_discovery_events_does_nothing_when_file_missing(tmp_path: Path) -> None:
    """Rotation should do nothing when the events file does not exist."""
    events_path = tmp_path / "events.jsonl"
    _rotate_discovery_events_if_needed(events_path)
    assert not events_path.exists()


def test_rotate_discovery_events_rotates_when_threshold_exceeded(tmp_path: Path) -> None:
    """Rotation should rename the file when it exceeds the size threshold."""
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("")
    # Use truncate to set file size to exactly the threshold without writing real data
    with open(events_path, "ab") as f:
        f.truncate(_DISCOVERY_MAX_FILE_SIZE_BYTES)

    _rotate_discovery_events_if_needed(events_path)

    # The original file should have been renamed
    assert not events_path.exists()
    rotated = [f for f in tmp_path.iterdir() if f.name.startswith("events.jsonl.")]
    assert len(rotated) == 1


def test_rotate_discovery_events_cleans_up_old_rotated_files(tmp_path: Path) -> None:
    """Rotation should remove old rotated files beyond the max count."""
    events_path = tmp_path / "events.jsonl"

    # Create several pre-existing rotated files (more than the max of 1)
    (tmp_path / "events.jsonl.20250101000000000000").write_text("old1\n")
    (tmp_path / "events.jsonl.20250201000000000000").write_text("old2\n")
    (tmp_path / "events.jsonl.20250301000000000000").write_text("old3\n")

    # Create the current file at the threshold size
    events_path.write_text("")
    with open(events_path, "ab") as f:
        f.truncate(_DISCOVERY_MAX_FILE_SIZE_BYTES)

    _rotate_discovery_events_if_needed(events_path)

    # After rotation, there should be at most _DISCOVERY_MAX_ROTATED_COUNT (1) rotated files
    # plus the newly rotated file = the newest rotated file should survive
    rotated = sorted(f for f in tmp_path.iterdir() if f.name.startswith("events.jsonl."))
    # With max_rotated_count=1, only the newest file should remain
    assert len(rotated) == 1
