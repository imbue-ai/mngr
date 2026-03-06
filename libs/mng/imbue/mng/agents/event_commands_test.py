"""Unit tests for event_commands.py."""

import json
import os
import subprocess
from pathlib import Path

from imbue.mng.agents.event_commands import build_state_transition_command


def test_build_state_transition_command_produces_valid_jsonl(tmp_path: Path) -> None:
    """The generated shell command should produce a valid JSONL line with the correct schema."""
    state_dir = str(tmp_path)
    command = build_state_transition_command("RUNNING", "WAITING")

    env = {
        **os.environ,
        "MNG_AGENT_STATE_DIR": state_dir,
        "MNG_AGENT_ID": "agent-test-123",
        "MNG_AGENT_NAME": "test-agent",
    }
    result = subprocess.run(["bash", "-c", command], env=env, capture_output=True, text=True)
    assert result.returncode == 0, f"Command failed: {result.stderr}"

    event_file = tmp_path / "events" / "mng_agents" / "events.jsonl"
    assert event_file.exists()

    lines = event_file.read_text().splitlines()
    assert len(lines) == 1

    event = json.loads(lines[0])
    assert event["type"] == "agent_state_transition"
    assert event["source"] == "mng_agents"
    assert event["agent_id"] == "agent-test-123"
    assert event["agent_name"] == "test-agent"
    assert event["from_state"] == "RUNNING"
    assert event["to_state"] == "WAITING"
    assert event["timestamp"].endswith("Z")
    assert event["event_id"].startswith("evt-")


def test_build_state_transition_command_waiting_to_running(tmp_path: Path) -> None:
    """The WAITING->RUNNING transition should produce the correct from/to states."""
    state_dir = str(tmp_path)
    command = build_state_transition_command("WAITING", "RUNNING")

    env = {
        **os.environ,
        "MNG_AGENT_STATE_DIR": state_dir,
        "MNG_AGENT_ID": "agent-456",
        "MNG_AGENT_NAME": "worker",
    }
    result = subprocess.run(["bash", "-c", command], env=env, capture_output=True, text=True)
    assert result.returncode == 0, f"Command failed: {result.stderr}"

    event_file = tmp_path / "events" / "mng_agents" / "events.jsonl"
    event = json.loads(event_file.read_text().splitlines()[0])

    assert event["from_state"] == "WAITING"
    assert event["to_state"] == "RUNNING"


def test_build_state_transition_command_appends_multiple_events(tmp_path: Path) -> None:
    """Running the command twice should append two JSONL lines."""
    state_dir = str(tmp_path)
    env = {
        **os.environ,
        "MNG_AGENT_STATE_DIR": state_dir,
        "MNG_AGENT_ID": "agent-789",
        "MNG_AGENT_NAME": "multi",
    }

    cmd1 = build_state_transition_command("WAITING", "RUNNING")
    cmd2 = build_state_transition_command("RUNNING", "WAITING")
    combined = f"{cmd1}\n{cmd2}"

    result = subprocess.run(["bash", "-c", combined], env=env, capture_output=True, text=True)
    assert result.returncode == 0, f"Command failed: {result.stderr}"

    event_file = tmp_path / "events" / "mng_agents" / "events.jsonl"
    lines = event_file.read_text().splitlines()
    assert len(lines) == 2

    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["from_state"] == "WAITING"
    assert first["to_state"] == "RUNNING"
    assert second["from_state"] == "RUNNING"
    assert second["to_state"] == "WAITING"
    # Event IDs should be unique
    assert first["event_id"] != second["event_id"]
