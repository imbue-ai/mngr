import json
import subprocess
from pathlib import Path

from imbue.mng.agents.event_commands import build_state_transition_command


def _run_bash(command: str, env: dict[str, str]) -> None:
    """Run a shell command and assert it succeeds."""
    result = subprocess.run(["bash", "-c", command], env=env, capture_output=True, text=True)
    assert result.returncode == 0, f"Command failed: {result.stderr}"


def test_build_state_transition_command_produces_valid_jsonl(tmp_path: Path, agent_event_env: dict[str, str]) -> None:
    """The generated shell command should produce a valid JSONL line with the correct schema."""
    command = build_state_transition_command("RUNNING", "WAITING")
    _run_bash(command, agent_event_env)

    event_file = tmp_path / "events" / "mng/agents" / "events.jsonl"
    assert event_file.exists()

    lines = event_file.read_text().splitlines()
    assert len(lines) == 1

    event = json.loads(lines[0])
    assert event["type"] == "agent_state_transition"
    assert event["source"] == "mng/agents"
    assert event["agent_id"] == "agent-test-fixture"
    assert event["agent_name"] == "test-agent"
    assert event["from_state"] == "RUNNING"
    assert event["to_state"] == "WAITING"
    assert event["timestamp"].endswith("Z")
    assert event["event_id"].startswith("evt-")


def test_build_state_transition_command_waiting_to_running(tmp_path: Path, agent_event_env: dict[str, str]) -> None:
    """The WAITING->RUNNING transition should produce the correct from/to states."""
    command = build_state_transition_command("WAITING", "RUNNING")
    _run_bash(command, agent_event_env)

    event_file = tmp_path / "events" / "mng/agents" / "events.jsonl"
    event = json.loads(event_file.read_text().splitlines()[0])

    assert event["from_state"] == "WAITING"
    assert event["to_state"] == "RUNNING"


def test_build_state_transition_command_appends_multiple_events(
    tmp_path: Path, agent_event_env: dict[str, str]
) -> None:
    """Running the command twice should append two JSONL lines."""
    cmd1 = build_state_transition_command("WAITING", "RUNNING")
    cmd2 = build_state_transition_command("RUNNING", "WAITING")
    combined = f"{cmd1}\n{cmd2}"

    _run_bash(combined, agent_event_env)

    event_file = tmp_path / "events" / "mng/agents" / "events.jsonl"
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
