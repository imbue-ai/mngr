import json
import subprocess
import sys
from pathlib import Path

import pytest

from imbue.mng.cli.complete_names import _find_last_full_snapshot_line_idx
from imbue.mng.cli.complete_names import _get_discovery_events_path
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


# =============================================================================
# _find_last_full_snapshot_line_idx tests
# =============================================================================


def test_find_last_full_snapshot_returns_negative_one_for_empty_list() -> None:
    """Returns -1 when given an empty list of lines."""
    assert _find_last_full_snapshot_line_idx([]) == -1


def test_find_last_full_snapshot_returns_negative_one_when_no_snapshot() -> None:
    """Returns -1 when no DISCOVERY_FULL event is present."""
    lines = [
        json.dumps({"type": "AGENT_DISCOVERED", "agent": {"agent_id": "a1", "agent_name": "x"}}),
        json.dumps({"type": "HOST_DISCOVERED", "host": {"host_id": "h1", "host_name": "y"}}),
    ]
    assert _find_last_full_snapshot_line_idx(lines) == -1


def test_find_last_full_snapshot_finds_last_snapshot() -> None:
    """Returns the index of the last DISCOVERY_FULL line when multiple exist."""
    snapshot1 = json.dumps({"type": "DISCOVERY_FULL", "agents": [], "hosts": []})
    other = json.dumps({"type": "AGENT_DISCOVERED", "agent": {}})
    snapshot2 = json.dumps({"type": "DISCOVERY_FULL", "agents": [], "hosts": []})
    lines = [snapshot1, other, snapshot2]
    assert _find_last_full_snapshot_line_idx(lines) == 2


def test_find_last_full_snapshot_skips_blank_lines() -> None:
    """Blank lines should be skipped without error."""
    snapshot = json.dumps({"type": "DISCOVERY_FULL", "agents": [], "hosts": []})
    lines = [snapshot, "", "  ", "\n"]
    assert _find_last_full_snapshot_line_idx(lines) == 0


def test_find_last_full_snapshot_skips_malformed_json() -> None:
    """Malformed JSON lines containing the DISCOVERY_FULL string should be skipped."""
    bad_line = '{"type": "DISCOVERY_FULL", broken json'
    good = json.dumps({"type": "DISCOVERY_FULL", "agents": [], "hosts": []})
    lines = [good, bad_line]
    # The bad line at index 1 is skipped, and the good line at index 0 is found
    assert _find_last_full_snapshot_line_idx(lines) == 0


# =============================================================================
# resolve_names_from_discovery_stream edge case tests
# =============================================================================


def test_resolve_names_returns_empty_for_empty_file(tmp_path: Path) -> None:
    """An existing but empty events file should return empty lists."""
    events_dir = tmp_path / "events"
    events_dir.mkdir(parents=True)
    events_path = events_dir / "events.jsonl"
    events_path.write_text("")

    agent_names, host_names = resolve_names_from_discovery_stream(events_path)

    assert agent_names == []
    assert host_names == []


def test_resolve_names_skips_malformed_json_lines_during_replay(tmp_path: Path) -> None:
    """Malformed JSON lines during replay should be silently skipped."""
    events_dir = tmp_path / "events"
    events_dir.mkdir(parents=True)
    events_path = events_dir / "events.jsonl"

    snapshot = json.dumps(
        {
            "type": "DISCOVERY_FULL",
            "agents": [{"agent_id": "a1", "agent_name": "good-agent", "host_id": "h1"}],
            "hosts": [{"host_id": "h1", "host_name": "host-one"}],
        }
    )
    bad_line = "not valid json {"
    events_path.write_text(snapshot + "\n" + bad_line + "\n")

    agent_names, host_names = resolve_names_from_discovery_stream(events_path)

    assert agent_names == ["good-agent"]
    assert host_names == ["host-one"]


def test_resolve_names_handles_host_discovered_event(tmp_path: Path) -> None:
    """HOST_DISCOVERED events should add new hosts to the result."""
    events_dir = tmp_path / "events"
    events_dir.mkdir(parents=True)
    events_path = events_dir / "events.jsonl"

    snapshot = json.dumps(
        {
            "type": "DISCOVERY_FULL",
            "agents": [],
            "hosts": [{"host_id": "h1", "host_name": "original-host"}],
        }
    )
    new_host = json.dumps(
        {
            "type": "HOST_DISCOVERED",
            "host": {"host_id": "h2", "host_name": "new-host"},
        }
    )
    events_path.write_text(snapshot + "\n" + new_host + "\n")

    _, host_names = resolve_names_from_discovery_stream(events_path)

    assert host_names == ["new-host", "original-host"]


def test_resolve_names_handles_unknown_event_types(tmp_path: Path) -> None:
    """Unknown event types should be silently ignored."""
    events_dir = tmp_path / "events"
    events_dir.mkdir(parents=True)
    events_path = events_dir / "events.jsonl"

    snapshot = json.dumps(
        {
            "type": "DISCOVERY_FULL",
            "agents": [{"agent_id": "a1", "agent_name": "my-agent", "host_id": "h1"}],
            "hosts": [{"host_id": "h1", "host_name": "my-host"}],
        }
    )
    unknown = json.dumps({"type": "SOME_FUTURE_EVENT", "data": "whatever"})
    events_path.write_text(snapshot + "\n" + unknown + "\n")

    agent_names, host_names = resolve_names_from_discovery_stream(events_path)

    assert agent_names == ["my-agent"]
    assert host_names == ["my-host"]


def test_resolve_names_works_without_full_snapshot(tmp_path: Path) -> None:
    """When there is no DISCOVERY_FULL snapshot, incremental events should still be replayed."""
    events_dir = tmp_path / "events"
    events_dir.mkdir(parents=True)
    events_path = events_dir / "events.jsonl"

    agent_event = json.dumps(
        {
            "type": "AGENT_DISCOVERED",
            "agent": {"agent_id": "a1", "agent_name": "discovered-agent", "host_id": "h1"},
        }
    )
    host_event = json.dumps(
        {
            "type": "HOST_DISCOVERED",
            "host": {"host_id": "h1", "host_name": "discovered-host"},
        }
    )
    events_path.write_text(agent_event + "\n" + host_event + "\n")

    agent_names, host_names = resolve_names_from_discovery_stream(events_path)

    assert agent_names == ["discovered-agent"]
    assert host_names == ["discovered-host"]


# =============================================================================
# _get_discovery_events_path tests
# =============================================================================


def test_get_discovery_events_path_uses_mng_host_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_get_discovery_events_path should use MNG_HOST_DIR env var when set."""
    monkeypatch.setenv("MNG_HOST_DIR", str(tmp_path))
    result = _get_discovery_events_path()
    assert result == tmp_path / "events" / "mng" / "discovery" / "events.jsonl"


def test_get_discovery_events_path_uses_mng_root_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """_get_discovery_events_path should use MNG_ROOT_NAME env var for custom root."""
    monkeypatch.delenv("MNG_HOST_DIR", raising=False)
    monkeypatch.setenv("MNG_ROOT_NAME", "custom-root")
    result = _get_discovery_events_path()
    expected = Path("~/.custom-root").expanduser() / "events" / "mng" / "discovery" / "events.jsonl"
    assert result == expected


# =============================================================================
# main() tests via subprocess
# =============================================================================


def test_main_prints_agent_names_by_default(tmp_path: Path) -> None:
    """main() without flags should print agent names to stdout."""
    host_dir = tmp_path / "host"
    events_path = host_dir / "events" / "mng" / "discovery" / "events.jsonl"
    write_discovery_snapshot_to_path(events_path, ["beta", "alpha"])

    result = subprocess.run(
        [sys.executable, "-m", "imbue.mng.cli.complete_names"],
        env={"MNG_HOST_DIR": str(host_dir), "PATH": ""},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    lines = result.stdout.strip().splitlines()
    assert lines == ["alpha", "beta"]


def test_main_prints_host_names_with_hosts_flag(tmp_path: Path) -> None:
    """main() with --hosts should print host names to stdout."""
    host_dir = tmp_path / "host"
    events_path = host_dir / "events" / "mng" / "discovery" / "events.jsonl"
    write_discovery_snapshot_to_path(events_path, ["my-agent"])

    result = subprocess.run(
        [sys.executable, "-m", "imbue.mng.cli.complete_names", "--hosts"],
        env={"MNG_HOST_DIR": str(host_dir), "PATH": ""},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    lines = result.stdout.strip().splitlines()
    assert lines == ["localhost"]


def test_main_prints_both_with_both_flag(tmp_path: Path) -> None:
    """main() with --both should print both agent and host names."""
    host_dir = tmp_path / "host"
    events_path = host_dir / "events" / "mng" / "discovery" / "events.jsonl"
    write_discovery_snapshot_to_path(events_path, ["my-agent"])

    result = subprocess.run(
        [sys.executable, "-m", "imbue.mng.cli.complete_names", "--both"],
        env={"MNG_HOST_DIR": str(host_dir), "PATH": ""},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    lines = result.stdout.strip().splitlines()
    assert "my-agent" in lines
    assert "localhost" in lines
