"""Tests for the shared listing data collection utilities."""

import json
import subprocess
from pathlib import Path

import pytest

from imbue.mngr.providers.listing_utils import build_listing_collection_script
from imbue.mngr.providers.listing_utils import parse_listing_collection_output
from imbue.mngr.providers.listing_utils import parse_optional_float
from imbue.mngr.providers.listing_utils import parse_optional_int


def test_parse_optional_int_valid() -> None:
    assert parse_optional_int("42") == 42


def test_parse_optional_int_empty() -> None:
    assert parse_optional_int("") is None


def test_parse_optional_int_invalid() -> None:
    assert parse_optional_int("abc") is None


def test_parse_optional_int_float_string_is_none() -> None:
    # A float-formatted string is not a valid int, so int() raises -> None.
    assert parse_optional_int("12.5") is None


def test_parse_optional_int_strips_surrounding_whitespace() -> None:
    # The value is stripped before parsing, so padded integers parse cleanly.
    assert parse_optional_int(" 42 ") == 42


def test_parse_optional_float_valid() -> None:
    assert parse_optional_float("3.14") == 3.14


def test_parse_optional_float_empty() -> None:
    assert parse_optional_float("") is None


def test_parse_optional_float_invalid() -> None:
    assert parse_optional_float("xyz") is None


def test_build_listing_collection_script_contains_key_sections() -> None:
    script = build_listing_collection_script("/mngr", "mngr-")
    assert "UPTIME=" in script
    assert "BTIME=" in script
    assert "LOCK_MTIME=" in script
    assert "SSH_ACTIVITY_MTIME=" in script
    assert "data.json" in script
    assert "ps -e" in script
    assert "/mngr/agents" in script


@pytest.mark.tmux
def test_build_listing_collection_script_round_trips_through_parser(tmp_path: Path) -> None:
    """Running the generated script against a real host_dir tree should round-trip.

    Builds a fake host_dir on disk, executes the generated collection script via
    bash, and feeds its stdout back through parse_listing_collection_output. This
    catches bugs (wrong delimiter, mis-quoted host_dir, wrong agent path) that a
    substring check on the script source cannot. The script shells out to tmux
    (per-agent pane lookup), hence the tmux marker.
    """
    host_dir = tmp_path / "host"
    (host_dir / "agents" / "agent-1").mkdir(parents=True)
    host_data = {"host_id": "host-xyz", "host_name": "fake-host"}
    (host_dir / "data.json").write_text(json.dumps(host_data))
    agent_data = {"id": "agent-1", "name": "agent-one", "type": "claude"}
    (host_dir / "agents" / "agent-1" / "data.json").write_text(json.dumps(agent_data))

    script = build_listing_collection_script(str(host_dir), "mngr-")
    completed = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr

    result = parse_listing_collection_output(completed.stdout)
    assert result["certified_data"] == host_data
    assert len(result["agents"]) == 1
    assert result["agents"][0]["data"] == agent_data


def test_parse_listing_collection_output_basic() -> None:
    output = "\n".join(
        [
            "UPTIME=12345.67",
            "BTIME=1700000000",
            "LOCK_MTIME=",
            "SSH_ACTIVITY_MTIME=1700000100",
            "---MNGR_DATA_JSON_START---",
            json.dumps({"host_id": "host-abc", "host_name": "test-host"}),
            "---MNGR_DATA_JSON_END---",
            "---MNGR_PS_START---",
            "  1     0 init",
            " 42     1 sshd",
            "---MNGR_PS_END---",
        ]
    )
    result = parse_listing_collection_output(output)
    assert result["uptime_seconds"] == 12345.67
    assert result["btime"] == 1700000000
    assert result["lock_mtime"] is None
    assert result["ssh_activity_mtime"] == 1700000100
    assert result["certified_data"]["host_id"] == "host-abc"
    assert "init" in result["ps_output"]
    assert result["agents"] == []


def test_parse_listing_collection_output_with_agent() -> None:
    agent_data = {"id": "agent-123", "name": "test-agent", "type": "claude", "command": "claude"}
    output = "\n".join(
        [
            "UPTIME=100.0",
            "BTIME=1700000000",
            "---MNGR_DATA_JSON_START---",
            "{}",
            "---MNGR_DATA_JSON_END---",
            "---MNGR_PS_START---",
            "---MNGR_PS_END---",
            "---MNGR_AGENT_START:agent-123---",
            "---MNGR_AGENT_DATA_START---",
            json.dumps(agent_data),
            "---MNGR_AGENT_DATA_END---",
            "USER_MTIME=1700000200",
            "AGENT_MTIME=",
            "START_MTIME=1700000100",
            "TMUX_INFO=0|claude|42",
            "ACTIVE=true",
            "URL=http://localhost:8080",
            "---MNGR_AGENT_END---",
        ]
    )
    result = parse_listing_collection_output(output)
    # The host data.json block (even when "{}") must be parsed into certified_data.
    assert result["certified_data"] == {}
    assert len(result["agents"]) == 1
    agent = result["agents"][0]
    assert agent["data"]["id"] == "agent-123"
    assert agent["user_activity_mtime"] == 1700000200
    assert agent["agent_activity_mtime"] is None
    assert agent["start_activity_mtime"] == 1700000100
    assert agent["tmux_info"] == "0|claude|42"
    assert agent["is_active"] is True
    assert agent["url"] == "http://localhost:8080"


def test_parse_listing_collection_output_empty() -> None:
    result = parse_listing_collection_output("")
    assert result.get("agents", []) == []


def test_parse_listing_collection_output_container_state_lines() -> None:
    """The outer (docker) container-state lines should map to their result keys."""
    output = "\n".join(
        [
            "CONTAINER_STATE=exited",
            "CONTAINER_EXIT_CODE=137",
            "---MNGR_DATA_JSON_START---",
            "{}",
            "---MNGR_DATA_JSON_END---",
            "---MNGR_PS_START---",
            "---MNGR_PS_END---",
        ]
    )
    result = parse_listing_collection_output(output)
    assert result["container_state"] == "exited"
    assert result["container_exit_code"] == 137
    assert result["agents"] == []


def test_parse_listing_collection_output_container_missing() -> None:
    """A CONTAINER_MISSING=true line should set container_missing to True."""
    result = parse_listing_collection_output("CONTAINER_MISSING=true")
    assert result["container_missing"] is True


def test_parse_listing_collection_output_excludes_agent_without_data() -> None:
    """An agent block lacking a data JSON section should be dropped from agents."""
    output = "\n".join(
        [
            "---MNGR_DATA_JSON_START---",
            "{}",
            "---MNGR_DATA_JSON_END---",
            "---MNGR_PS_START---",
            "---MNGR_PS_END---",
            "---MNGR_AGENT_START:agent-no-data---",
            # No ---MNGR_AGENT_DATA_START---/END--- block here.
            "USER_MTIME=1700000200",
            "ACTIVE=true",
            "---MNGR_AGENT_END---",
        ]
    )
    result = parse_listing_collection_output(output)
    assert result["agents"] == []
