"""Tests for the shared listing data collection utilities."""

import json

import pytest

from imbue.mngr.providers.listing_utils import SEP_AGENT_DATA_END
from imbue.mngr.providers.listing_utils import SEP_AGENT_DATA_START
from imbue.mngr.providers.listing_utils import SEP_AGENT_END
from imbue.mngr.providers.listing_utils import SEP_AGENT_START
from imbue.mngr.providers.listing_utils import SEP_DATA_JSON_END
from imbue.mngr.providers.listing_utils import SEP_DATA_JSON_START
from imbue.mngr.providers.listing_utils import SEP_PS_END
from imbue.mngr.providers.listing_utils import SEP_PS_START
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


def test_build_listing_collection_script_emits_sections_in_required_order() -> None:
    """The collection script must emit its sections in a fixed order.

    ``parse_listing_collection_output`` is a single forward scan that records
    the *first* occurrence of each scalar key and reads delimited blocks in the
    order they appear, so the generated script's structure -- not just the
    presence of scattered substrings -- is load-bearing. This pins the section
    ordering, the host_dir/prefix interpolation, and the delimiter markers that
    the parser depends on. (The script is a fragment meant to be sourced inside
    an existing shell, so it intentionally has no shebang or ``set -e``.)
    """
    script = build_listing_collection_script("/mngr", "mngr-")

    # Scalar status lines and delimited blocks must appear in this exact order.
    ordered_markers = [
        "UPTIME=",
        "BTIME=",
        "LOCK_MTIME=",
        "SSH_ACTIVITY_MTIME=",
        SEP_DATA_JSON_START,
        SEP_DATA_JSON_END,
        SEP_PS_START,
        SEP_PS_END,
    ]
    positions = [script.index(marker) for marker in ordered_markers]
    assert positions == sorted(positions), positions

    # host_dir and prefix are interpolated into the real paths the script reads.
    assert "/mngr/data.json" in script
    assert "/mngr/agents" in script
    assert "stat -c %Y '/mngr/host_lock'" in script
    assert "session_name='mngr-'\"$agent_name\"" in script

    # The ps snapshot is collected for lifecycle detection.
    assert "ps -e -o pid=,ppid=,comm=" in script

    # The per-agent block is delimited so the parser can group fields per agent.
    assert SEP_AGENT_START in script
    assert SEP_AGENT_DATA_START in script
    assert SEP_AGENT_DATA_END in script
    assert SEP_AGENT_END in script


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


def test_parse_listing_collection_output_handles_empty_scalar_values() -> None:
    """Empty key=value lines parse to None rather than raising or defaulting."""
    output = "UPTIME=\nBTIME=\nLOCK_MTIME=\nSSH_ACTIVITY_MTIME=\n"
    result = parse_listing_collection_output(output)
    assert result["uptime_seconds"] is None
    assert result["btime"] is None
    assert result["lock_mtime"] is None
    assert result["ssh_activity_mtime"] is None


@pytest.mark.allow_warnings(match=r"Failed to parse agent data\.json")
def test_parse_listing_collection_output_skips_agent_with_malformed_json() -> None:
    """An agent whose data.json fails to parse is dropped (no "data" key, so excluded)."""
    output = "\n".join(
        [
            "UPTIME=100",
            f"{SEP_AGENT_START}agent-bad---",
            SEP_AGENT_DATA_START,
            "not valid json{{",
            SEP_AGENT_DATA_END,
            SEP_AGENT_END,
        ]
    )
    result = parse_listing_collection_output(output)
    assert result["agents"] == []
