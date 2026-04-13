"""Tests for the shared listing data collection utilities."""

import json

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
