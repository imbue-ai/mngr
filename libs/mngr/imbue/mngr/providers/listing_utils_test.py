"""Tests for the shared listing data collection utilities."""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr.providers.listing_utils import build_listing_collection_script
from imbue.mngr.providers.listing_utils import extract_agent_data_from_parsed_listing
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
    # The agents loop reads the host_dir the prelude resolved, which for a
    # single candidate is the one passed in.
    assert "HOST_DIR=/mngr" in script
    assert '"$HOST_DIR/agents"' in script


def test_build_listing_collection_script_targets_named_primary_window() -> None:
    """Lifecycle detection must target the agent window by name, not the literal :0,
    so it works regardless of the user's tmux base-index."""
    script = build_listing_collection_script("/mngr", "mngr-", window_name="agent")
    assert ':agent" -F' in script
    assert ':0" -F' not in script


def test_build_listing_collection_script_uses_custom_window_name() -> None:
    script = build_listing_collection_script("/mngr", "mngr-", window_name="primary")
    assert ':primary" -F' in script


def _run_listing_script(script: str) -> dict[str, Any]:
    """Execute a generated listing script and parse what it emitted.

    The resolution logic is shell, so asserting on the generated text would
    only restate the implementation; running it against a real directory tree
    is what proves it picks the right candidate.
    """
    finished = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    assert finished.returncode == 0, finished.stderr
    return parse_listing_collection_output(finished.stdout)


def test_listing_script_falls_back_to_the_candidate_holding_the_host_record(tmp_path: Path) -> None:
    """A host_dir with no data.json must lose to a later candidate that has one.

    This is the pre-declutter workspace case: the client is configured for the
    current layout, but the container was baked with the old one and keeps its
    state there.
    """
    configured = tmp_path / "home" / "user" / ".mngr"
    legacy = tmp_path / "mngr"
    configured.mkdir(parents=True)
    legacy.mkdir(parents=True)
    (legacy / "data.json").write_text(json.dumps({"host_id": "host-legacy"}))

    result = _run_listing_script(build_listing_collection_script(str(configured), "mngr-", "agent", (str(legacy),)))

    assert result["host_dir"] == str(legacy)
    assert result["certified_data"] == {"host_id": "host-legacy"}


@pytest.mark.tmux
def test_listing_script_enumerates_agents_from_the_fallback_host_dir(tmp_path: Path) -> None:
    """Agents must come from the resolved host_dir, not the configured one.

    Reading agents from the wrong layout is what made a healthy pre-declutter
    workspace list zero agents, which in turn stripped its ``is_primary`` label
    and dropped it out of the minds sidebar. Marked ``tmux`` because the agent
    branch shells out to ``tmux list-panes`` for lifecycle detection.
    """
    configured = tmp_path / "home" / "user" / ".mngr"
    legacy = tmp_path / "mngr"
    configured.mkdir(parents=True)
    (legacy / "agents" / "agent-abc").mkdir(parents=True)
    (legacy / "data.json").write_text(json.dumps({"host_id": "host-legacy"}))
    (legacy / "agents" / "agent-abc" / "data.json").write_text(json.dumps({"id": "agent-abc", "name": "old"}))

    result = _run_listing_script(build_listing_collection_script(str(configured), "mngr-", "agent", (str(legacy),)))

    assert result["host_dir"] == str(legacy)
    assert [agent["data"]["id"] for agent in result["agents"]] == ["agent-abc"]


def test_listing_script_prefers_the_configured_host_dir_over_an_older_candidate(tmp_path: Path) -> None:
    """With both candidates populated, the configured layout wins."""
    configured = tmp_path / "home" / "user" / ".mngr"
    legacy = tmp_path / "mngr"
    for host_dir, host_id in ((configured, "host-current"), (legacy, "host-legacy")):
        host_dir.mkdir(parents=True)
        (host_dir / "data.json").write_text(json.dumps({"host_id": host_id}))

    result = _run_listing_script(build_listing_collection_script(str(configured), "mngr-", "agent", (str(legacy),)))

    assert result["host_dir"] == str(configured)
    assert result["certified_data"] == {"host_id": "host-current"}


def test_listing_script_ignores_an_empty_husk_directory(tmp_path: Path) -> None:
    """An existing-but-empty configured host_dir must not beat a populated fallback.

    A failed read against the wrong layout leaves exactly this husk behind (mngr
    mkdir -p's the state dir), so resolving on directory existence would lock a
    healthy host onto the empty one.
    """
    configured = tmp_path / "home" / "user" / ".mngr"
    legacy = tmp_path / "mngr"
    (configured / "agents").mkdir(parents=True)
    legacy.mkdir(parents=True)
    (legacy / "data.json").write_text(json.dumps({"host_id": "host-legacy"}))

    result = _run_listing_script(build_listing_collection_script(str(configured), "mngr-", "agent", (str(legacy),)))

    assert result["host_dir"] == str(legacy)
    assert result["certified_data"] == {"host_id": "host-legacy"}


def test_listing_script_keeps_the_configured_host_dir_when_no_candidate_has_a_record(tmp_path: Path) -> None:
    """A host mid-bootstrap (no data.json anywhere) reports the configured layout.

    Falling through to a fallback here would misfile a brand-new host under the
    old layout on the strength of no evidence at all.
    """
    configured = tmp_path / "home" / "user" / ".mngr"
    configured.mkdir(parents=True)

    result = _run_listing_script(
        build_listing_collection_script(str(configured), "mngr-", "agent", (str(tmp_path / "mngr"),))
    )

    assert result["host_dir"] == str(configured)
    assert result["certified_data"] == {}


def test_parse_listing_collection_output_basic() -> None:
    output = "\n".join(
        [
            "UPTIME=12345.67",
            "BTIME=1700000000",
            "LOCK_HELD=true",
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
    assert result["is_lock_held"] is True
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


@pytest.mark.allow_warnings(match=r"missing or non-object 'data'")
def test_extract_agent_data_returns_data_dicts_and_skips_malformed(log_warnings: list[str]) -> None:
    """Only well-formed ``{"data": {...}}`` entries contribute agent data; an entry with
    missing or non-object ``data`` (corrupt/hand-edited) is skipped with a warning."""
    parsed_listing = {
        "container_state": "running",
        "agents": [
            {"data": {"id": "a-1", "name": "system-services"}},
            {"data": {"id": "a-2", "name": "chat-host"}},
            {"user_activity_mtime": 123},
            {"data": ["not", "an", "object"]},
        ],
    }

    assert extract_agent_data_from_parsed_listing(parsed_listing) == [
        {"id": "a-1", "name": "system-services"},
        {"id": "a-2", "name": "chat-host"},
    ]
    assert sum("missing or non-object 'data'" in message for message in log_warnings) == 2
