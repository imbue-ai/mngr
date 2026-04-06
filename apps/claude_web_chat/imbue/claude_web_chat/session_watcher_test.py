"""Tests for the session file watcher."""

import json
import time
from pathlib import Path
from typing import Any

from imbue.claude_web_chat.session_watcher import AgentSessionWatcher


def _write_session_file(projects_dir: Path, session_id: str, events: list[dict[str, Any]]) -> Path:
    session_dir = projects_dir / "hash123"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / f"{session_id}.jsonl"
    content = "\n".join(json.dumps(e) for e in events) + "\n"
    session_file.write_text(content)
    return session_file


def _setup_agent(tmp_path: Path, events: list[dict[str, Any]]) -> tuple[Path, Path, str]:
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    claude_config_dir = tmp_path / "claude_config"
    projects_dir = claude_config_dir / "projects"

    session_id = "test-session"
    _write_session_file(projects_dir, session_id, events)
    (agent_state_dir / "claude_session_id_history").write_text(f"{session_id}\n")

    return agent_state_dir, claude_config_dir, session_id


def test_get_all_events_returns_parsed_events(tmp_path: Path) -> None:
    events = [
        {
            "type": "user",
            "uuid": "uuid-1",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content": "Hello"},
        },
        {
            "type": "assistant",
            "uuid": "uuid-2",
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-6",
                "content": [{"type": "text", "text": "Hi!"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        },
    ]

    agent_state_dir, claude_config_dir, _ = _setup_agent(tmp_path, events)
    collected: list[tuple[str, list[dict[str, Any]]]] = []

    watcher = AgentSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: collected.append((aid, evts)),
    )

    result = watcher.get_all_events()
    assert len(result) == 2
    assert result[0]["type"] == "user_message"
    assert result[1]["type"] == "assistant_message"


def test_get_all_events_with_tail(tmp_path: Path) -> None:
    events = [
        {
            "type": "user",
            "uuid": f"uuid-{i}",
            "timestamp": f"2026-01-01T00:00:{i:02d}Z",
            "message": {"role": "user", "content": f"Message {i}"},
        }
        for i in range(10)
    ]

    agent_state_dir, claude_config_dir, _ = _setup_agent(tmp_path, events)

    watcher = AgentSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: None,
    )

    result = watcher.get_all_events(tail=3)
    assert len(result) == 3
    assert result[0]["content"] == "Message 7"
    assert result[2]["content"] == "Message 9"


def test_get_backfill_events(tmp_path: Path) -> None:
    events = [
        {
            "type": "user",
            "uuid": f"uuid-{i}",
            "timestamp": f"2026-01-01T00:00:{i:02d}Z",
            "message": {"role": "user", "content": f"Message {i}"},
        }
        for i in range(10)
    ]

    agent_state_dir, claude_config_dir, _ = _setup_agent(tmp_path, events)

    watcher = AgentSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: None,
    )

    # Get events before uuid-5-user
    result = watcher.get_backfill_events("uuid-5-user", limit=3)
    assert len(result) == 3
    assert result[0]["content"] == "Message 2"
    assert result[2]["content"] == "Message 4"


def test_watcher_detects_new_events(tmp_path: Path) -> None:
    events = [
        {
            "type": "user",
            "uuid": "uuid-1",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content": "Hello"},
        },
    ]

    agent_state_dir, claude_config_dir, session_id = _setup_agent(tmp_path, events)
    collected: list[tuple[str, list[dict[str, Any]]]] = []

    watcher = AgentSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: collected.append((aid, evts)),
    )

    # Load initial events (this sets the byte offsets)
    initial = watcher.get_all_events()
    assert len(initial) == 1

    # Start the watcher and give it time to initialize
    watcher.start()
    time.sleep(2.0)  # Allow watcher to fully initialize and set offsets

    try:
        # Append a new event to the session file
        session_file = claude_config_dir / "projects" / "hash123" / f"{session_id}.jsonl"
        new_event = {
            "type": "assistant",
            "uuid": "uuid-2",
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-6",
                "content": [{"type": "text", "text": "Hi!"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        }
        with open(session_file, "a") as f:
            f.write(json.dumps(new_event) + "\n")

        # Wait for the watcher to pick it up
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if collected:
                break
            time.sleep(0.2)

        assert len(collected) >= 1, "Watcher did not detect new events"
        assert collected[0][0] == "test-agent"
        assert collected[0][1][0]["type"] == "assistant_message"
    finally:
        watcher.stop()


def test_watcher_handles_missing_history_file(tmp_path: Path) -> None:
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    claude_config_dir = tmp_path / "claude_config"

    watcher = AgentSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: None,
    )

    # Should not raise
    result = watcher.get_all_events()
    assert len(result) == 0


def test_watcher_handles_missing_session_file(tmp_path: Path) -> None:
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    claude_config_dir = tmp_path / "claude_config"
    claude_config_dir.mkdir()

    # Write history with a session ID whose file doesn't exist
    (agent_state_dir / "claude_session_id_history").write_text("nonexistent-session\n")

    watcher = AgentSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: None,
    )

    result = watcher.get_all_events()
    assert len(result) == 0
