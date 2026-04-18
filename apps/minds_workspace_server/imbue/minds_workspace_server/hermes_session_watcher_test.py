"""Tests for the hermes SQLite session watcher."""

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from imbue.minds_workspace_server.hermes_session_watcher import HermesSessionWatcher
from imbue.mngr.utils.polling import poll_until

# Minimal schema matching hermes_state.py so our DB looks authentic enough
# for the watcher's queries (sessions + messages tables).
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    started_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL,
    finish_reason TEXT
);
"""


def _make_hermes_db(agent_state_dir: Path) -> Path:
    """Create an empty hermes state.db at the expected per-agent location."""
    hermes_home = agent_state_dir / "hermes_home"
    hermes_home.mkdir(parents=True)
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()
    return db_path


def _insert_session(db_path: Path, session_id: str, source: str = "cli", started_at: float = 1700000000.0) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
            (session_id, source, started_at),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_message(
    db_path: Path,
    session_id: str,
    role: str,
    content: str | None = None,
    tool_call_id: str | None = None,
    tool_calls: str | None = None,
    tool_name: str | None = None,
    timestamp: float = 1700000001.0,
    finish_reason: str | None = None,
) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            "INSERT INTO messages (session_id, role, content, tool_call_id, tool_calls, "
            "tool_name, timestamp, finish_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, role, content, tool_call_id, tool_calls, tool_name, timestamp, finish_reason),
        )
        conn.commit()
        row_id = cursor.lastrowid
        assert row_id is not None
        return row_id
    finally:
        conn.close()


def test_get_all_events_with_no_db_returns_empty(tmp_path: Path) -> None:
    """When the DB file does not yet exist (agent just started), return no events."""
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()

    watcher = HermesSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        on_events=lambda aid, evts: None,
    )

    assert watcher.get_all_events() == []


def test_get_all_events_translates_user_and_assistant_messages(tmp_path: Path) -> None:
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    db_path = _make_hermes_db(agent_state_dir)

    _insert_session(db_path, "sess_1", source="cli", started_at=1700000000.0)
    _insert_message(db_path, "sess_1", role="user", content="Hello hermes", timestamp=1700000001.0)
    _insert_message(
        db_path,
        "sess_1",
        role="assistant",
        content="Hi there!",
        timestamp=1700000002.0,
        finish_reason="stop",
    )

    watcher = HermesSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        on_events=lambda aid, evts: None,
    )

    events = watcher.get_all_events()

    assert len(events) == 2
    assert events[0]["type"] == "user_message"
    assert events[0]["content"] == "Hello hermes"
    assert events[0]["source"] == "hermes/sqlite"
    assert events[0]["session_id"] == "sess_1"

    assert events[1]["type"] == "assistant_message"
    assert events[1]["text"] == "Hi there!"
    assert events[1]["stop_reason"] == "stop"
    assert events[1]["tool_calls"] == []


def test_get_all_events_translates_tool_calls_and_tool_results(tmp_path: Path) -> None:
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    db_path = _make_hermes_db(agent_state_dir)

    _insert_session(db_path, "sess_1", source="cli", started_at=1700000000.0)
    tool_calls_json = json.dumps(
        [
            {
                "id": "call_abc",
                "type": "function",
                "function": {
                    "name": "terminal",
                    "arguments": json.dumps({"cmd": "ls"}),
                },
            }
        ]
    )
    _insert_message(
        db_path,
        "sess_1",
        role="assistant",
        content="Running ls",
        tool_calls=tool_calls_json,
        timestamp=1700000001.0,
    )
    _insert_message(
        db_path,
        "sess_1",
        role="tool",
        content="file1\nfile2\n",
        tool_call_id="call_abc",
        tool_name="terminal",
        timestamp=1700000002.0,
    )

    watcher = HermesSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        on_events=lambda aid, evts: None,
    )

    events = watcher.get_all_events()

    assert len(events) == 2
    assistant = events[0]
    assert assistant["type"] == "assistant_message"
    assert len(assistant["tool_calls"]) == 1
    assert assistant["tool_calls"][0]["tool_call_id"] == "call_abc"
    assert assistant["tool_calls"][0]["tool_name"] == "terminal"
    assert assistant["tool_calls"][0]["input_preview"] == '{"cmd":"ls"}'

    tool_result = events[1]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_call_id"] == "call_abc"
    assert tool_result["tool_name"] == "terminal"
    assert tool_result["output"] == "file1\nfile2\n"
    assert tool_result["is_error"] is False


def test_get_all_events_ignores_non_cli_sessions(tmp_path: Path) -> None:
    """Gateway sessions (telegram, discord, etc.) should not appear in the default listing."""
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    db_path = _make_hermes_db(agent_state_dir)

    _insert_session(db_path, "sess_cli", source="cli")
    _insert_session(db_path, "sess_tg", source="telegram")
    _insert_message(db_path, "sess_cli", role="user", content="cli message", timestamp=1700000001.0)
    _insert_message(db_path, "sess_tg", role="user", content="tg message", timestamp=1700000002.0)

    watcher = HermesSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        on_events=lambda aid, evts: None,
    )

    events = watcher.get_all_events()

    assert len(events) == 1
    assert events[0]["content"] == "cli message"


def test_get_all_events_filters_by_session_id(tmp_path: Path) -> None:
    """Passing session_id should bypass the cli-only filter and return that session's events."""
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    db_path = _make_hermes_db(agent_state_dir)

    _insert_session(db_path, "sess_a", source="cli")
    _insert_session(db_path, "sess_b", source="telegram")
    _insert_message(db_path, "sess_a", role="user", content="from-a", timestamp=1700000001.0)
    _insert_message(db_path, "sess_b", role="user", content="from-b", timestamp=1700000002.0)

    watcher = HermesSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        on_events=lambda aid, evts: None,
    )

    events = watcher.get_all_events(session_id="sess_b")

    assert len(events) == 1
    assert events[0]["content"] == "from-b"


def test_polling_emits_only_new_events(tmp_path: Path) -> None:
    """The background poller should emit each message exactly once, even across cycles."""
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    db_path = _make_hermes_db(agent_state_dir)

    _insert_session(db_path, "sess_1", source="cli")
    _insert_message(db_path, "sess_1", role="user", content="first", timestamp=1700000001.0)

    collected_events: list[dict[str, Any]] = []
    collected_lock = threading.Lock()

    def on_events(_agent_id: str, events: list[dict[str, Any]]) -> None:
        with collected_lock:
            collected_events.extend(events)

    watcher = HermesSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        on_events=on_events,
    )

    # Prime the high-water mark with the existing row so it's not re-emitted.
    initial = watcher.get_all_events()
    assert len(initial) == 1

    watcher.start()
    try:
        _insert_message(db_path, "sess_1", role="assistant", content="second", timestamp=1700000002.0)
        _insert_message(db_path, "sess_1", role="user", content="third", timestamp=1700000003.0)

        def _both_events_received() -> bool:
            with collected_lock:
                return len(collected_events) >= 2

        poll_until(_both_events_received, timeout=5.0, poll_interval=0.1)
    finally:
        watcher.stop()

    with collected_lock:
        # The two rows inserted after the initial read should both be emitted,
        # and the first row must not be re-emitted.
        contents = [e.get("content", e.get("text", "")) for e in collected_events]
        assert contents == ["second", "third"]


def test_hermes_home_resolved_from_env_file(tmp_path: Path) -> None:
    """When the agent's env file points HERMES_HOME elsewhere, the watcher uses that path."""
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()

    custom_hermes_home = tmp_path / "custom_hermes_home"
    custom_hermes_home.mkdir()
    db_path = custom_hermes_home / "state.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()

    (agent_state_dir / "env").write_text(f"HERMES_HOME={custom_hermes_home}\n")

    _insert_session(db_path, "sess_1", source="cli")
    _insert_message(db_path, "sess_1", role="user", content="from env-configured home", timestamp=1700000001.0)

    watcher = HermesSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        on_events=lambda aid, evts: None,
    )

    events = watcher.get_all_events()
    assert len(events) == 1
    assert events[0]["content"] == "from env-configured home"


def test_get_backfill_events_returns_events_before_target(tmp_path: Path) -> None:
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    db_path = _make_hermes_db(agent_state_dir)

    _insert_session(db_path, "sess_1", source="cli")
    for i in range(5):
        _insert_message(db_path, "sess_1", role="user", content=f"msg-{i}", timestamp=1700000000.0 + i)

    watcher = HermesSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        on_events=lambda aid, evts: None,
    )

    all_events = watcher.get_all_events()
    assert len(all_events) == 5
    target_event_id = all_events[3]["event_id"]

    backfill = watcher.get_backfill_events(target_event_id, limit=2)
    assert len(backfill) == 2
    assert backfill[0]["content"] == "msg-1"
    assert backfill[1]["content"] == "msg-2"


def test_get_subagent_metadata_always_returns_none(tmp_path: Path) -> None:
    """Hermes has no subagent concept; this is here to satisfy the shared interface."""
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()

    watcher = HermesSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        on_events=lambda aid, evts: None,
    )

    assert watcher.get_subagent_metadata("anything") is None
