"""Unit tests for conversation_watcher.py."""

import json
from pathlib import Path

import pytest

from imbue.mng_claude_changeling.conftest import create_test_llm_db
from imbue.mng_claude_changeling.conftest import write_changelings_settings_toml
from imbue.mng_claude_changeling.conftest import write_conversation_to_db
from imbue.mng_claude_changeling.resources.conversation_watcher import _get_llm_db_path
from imbue.mng_claude_changeling.resources.conversation_watcher import _get_tracked_conversation_ids
from imbue.mng_claude_changeling.resources.conversation_watcher import _load_poll_interval
from imbue.mng_claude_changeling.resources.conversation_watcher import _sync_messages

# -- _load_poll_interval tests --


def test_load_poll_interval_defaults_when_no_file(tmp_path: Path) -> None:
    assert _load_poll_interval(tmp_path) == 5


def test_load_poll_interval_reads_from_file(tmp_path: Path) -> None:
    write_changelings_settings_toml(tmp_path, "[watchers]\nconversation_poll_interval_seconds = 15\n")
    assert _load_poll_interval(tmp_path) == 15


def test_load_poll_interval_handles_corrupt_file(tmp_path: Path) -> None:
    write_changelings_settings_toml(tmp_path, "this is not valid toml {{{")
    assert _load_poll_interval(tmp_path) == 5


def test_load_poll_interval_handles_empty_watchers_section(tmp_path: Path) -> None:
    write_changelings_settings_toml(tmp_path, "[watchers]\n")
    assert _load_poll_interval(tmp_path) == 5


# -- _get_llm_db_path tests --


def test_get_llm_db_path_raises_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_USER_PATH", raising=False)
    with pytest.raises(RuntimeError, match="LLM_USER_PATH must be set"):
        _get_llm_db_path()


def test_get_llm_db_path_with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_USER_PATH", "/custom/llm/path")
    db_path = _get_llm_db_path()
    assert db_path == Path("/custom/llm/path/logs.db")


# -- _get_tracked_conversation_ids tests --


def test_get_tracked_conversation_ids_empty_when_no_db(tmp_path: Path) -> None:
    db_path = tmp_path / "nonexistent.db"
    assert _get_tracked_conversation_ids(db_path) == set()


def test_get_tracked_conversation_ids_reads_cids(tmp_path: Path) -> None:
    db_path = tmp_path / "logs.db"
    create_test_llm_db(db_path, [])
    write_conversation_to_db(db_path, "conv-1")
    write_conversation_to_db(db_path, "conv-2")

    cids = _get_tracked_conversation_ids(db_path)
    assert cids == {"conv-1", "conv-2"}


def test_get_tracked_conversation_ids_empty_table(tmp_path: Path) -> None:
    db_path = tmp_path / "logs.db"
    create_test_llm_db(db_path, [])

    cids = _get_tracked_conversation_ids(db_path)
    assert cids == set()


# -- _sync_messages tests --


def test_sync_messages_syncs_from_database(tmp_path: Path) -> None:
    messages_file = tmp_path / "messages" / "events.jsonl"
    messages_file.parent.mkdir(parents=True)

    db_path = tmp_path / "logs.db"
    create_test_llm_db(
        db_path,
        [
            ("resp-1", "Hello", "Hi!", "claude-sonnet-4-6", "2025-01-15T10:01:00", "conv-1"),
        ],
    )
    write_conversation_to_db(db_path, "conv-1")

    synced = _sync_messages(db_path, messages_file)
    assert synced == 2  # 1 user + 1 assistant

    lines = messages_file.read_text().strip().split("\n")
    events = [json.loads(line) for line in lines]
    assert len(events) == 2
    roles = [e["role"] for e in events]
    assert "user" in roles
    assert "assistant" in roles


def test_sync_messages_is_idempotent(tmp_path: Path) -> None:
    messages_file = tmp_path / "messages" / "events.jsonl"
    messages_file.parent.mkdir(parents=True)

    db_path = tmp_path / "logs.db"
    create_test_llm_db(
        db_path,
        [("resp-1", "Hello", "Hi!", "claude-sonnet-4-6", "2025-01-15T10:01:00", "conv-1")],
    )
    write_conversation_to_db(db_path, "conv-1")

    first_count = _sync_messages(db_path, messages_file)
    assert first_count == 2

    second_count = _sync_messages(db_path, messages_file)
    assert second_count == 0

    lines = messages_file.read_text().strip().split("\n")
    assert len(lines) == 2


def test_sync_messages_returns_zero_for_missing_db(tmp_path: Path) -> None:
    messages_file = tmp_path / "messages" / "events.jsonl"

    result = _sync_messages(tmp_path / "nonexistent.db", messages_file)
    assert result == 0


def test_sync_messages_returns_zero_with_no_tracked_conversations(tmp_path: Path) -> None:
    messages_file = tmp_path / "messages" / "events.jsonl"

    db_path = tmp_path / "logs.db"
    create_test_llm_db(db_path, [])

    result = _sync_messages(db_path, messages_file)
    assert result == 0


def test_sync_messages_only_syncs_tracked_conversations(tmp_path: Path) -> None:
    messages_file = tmp_path / "messages" / "events.jsonl"
    messages_file.parent.mkdir(parents=True)

    db_path = tmp_path / "logs.db"
    create_test_llm_db(
        db_path,
        [
            ("resp-1", "Hi", "Hello!", "model", "2025-01-15T10:01:00", "conv-tracked"),
            ("resp-2", "Yo", "Hey!", "model", "2025-01-15T10:02:00", "conv-untracked"),
        ],
    )
    write_conversation_to_db(db_path, "conv-tracked")

    synced = _sync_messages(db_path, messages_file)
    assert synced == 2  # Only the tracked conversation

    lines = messages_file.read_text().strip().split("\n")
    events = [json.loads(line) for line in lines]
    for event in events:
        assert event["conversation_id"] == "conv-tracked"


def test_sync_messages_skips_preliminary_prompt_only_rows(tmp_path: Path) -> None:
    """Preliminary rows (prompt set, response empty) from llm live-chat are skipped.

    llm live-chat inserts a prompt-only row for crash safety before streaming
    the response, then deletes it after the real response is logged. If the
    conversation watcher polls during this window, it should skip the
    preliminary row to avoid duplicate user messages.
    """
    messages_file = tmp_path / "messages" / "events.jsonl"
    messages_file.parent.mkdir(parents=True)

    db_path = tmp_path / "logs.db"
    create_test_llm_db(
        db_path,
        [("resp-1", "Only a prompt", "", "model", "2025-01-15T10:01:00", "conv-1")],
    )
    write_conversation_to_db(db_path, "conv-1")

    synced = _sync_messages(db_path, messages_file)
    assert synced == 0


def test_sync_messages_handles_response_only(tmp_path: Path) -> None:
    """A response with only a response (no prompt) should still sync the assistant message."""
    messages_file = tmp_path / "messages" / "events.jsonl"
    messages_file.parent.mkdir(parents=True)

    db_path = tmp_path / "logs.db"
    create_test_llm_db(
        db_path,
        [("resp-1", "", "Only a response", "model", "2025-01-15T10:01:00", "conv-1")],
    )
    write_conversation_to_db(db_path, "conv-1")

    synced = _sync_messages(db_path, messages_file)
    assert synced == 1  # Only the assistant message

    lines = messages_file.read_text().strip().split("\n")
    event = json.loads(lines[0])
    assert event["role"] == "assistant"


def test_sync_messages_event_format(tmp_path: Path) -> None:
    """Verify the format of synced message events."""
    messages_file = tmp_path / "messages" / "events.jsonl"
    messages_file.parent.mkdir(parents=True)

    db_path = tmp_path / "logs.db"
    create_test_llm_db(
        db_path,
        [("resp-1", "Hello", "Hi!", "claude-sonnet-4-6", "2025-01-15T10:01:00", "conv-1")],
    )
    write_conversation_to_db(db_path, "conv-1")

    _sync_messages(db_path, messages_file)
    lines = messages_file.read_text().strip().split("\n")

    for line in lines:
        event = json.loads(line)
        assert event["type"] == "message"
        assert event["source"] == "messages"
        assert event["conversation_id"] == "conv-1"
        assert "event_id" in event
        assert "timestamp" in event
        assert event["role"] in ("user", "assistant")
        assert "content" in event
