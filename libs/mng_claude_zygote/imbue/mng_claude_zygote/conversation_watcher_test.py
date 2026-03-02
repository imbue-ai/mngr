"""Unit tests for conversation_watcher.py."""

import json
from pathlib import Path

import pytest

from imbue.mng_claude_zygote.conftest import create_test_llm_db
from imbue.mng_claude_zygote.conftest import write_conversation_event
from imbue.mng_claude_zygote.resources.conversation_watcher import _WatcherSettings
from imbue.mng_claude_zygote.resources.conversation_watcher import _get_existing_event_ids
from imbue.mng_claude_zygote.resources.conversation_watcher import _get_llm_db_path
from imbue.mng_claude_zygote.resources.conversation_watcher import _get_tracked_conversation_ids
from imbue.mng_claude_zygote.resources.conversation_watcher import _load_watcher_settings
from imbue.mng_claude_zygote.resources.conversation_watcher import _sync_messages
from imbue.mng_claude_zygote.resources.watcher_common import Logger

# -- _WatcherSettings tests --


def test_watcher_settings_defaults() -> None:
    settings = _WatcherSettings()
    assert settings.poll_interval == 5


def test_watcher_settings_is_frozen() -> None:
    settings = _WatcherSettings()
    with pytest.raises(AttributeError):
        settings.poll_interval = 10  # type: ignore[misc]


# -- _load_watcher_settings tests --


def test_load_settings_defaults_when_no_file(tmp_path: Path) -> None:
    settings = _load_watcher_settings(tmp_path)
    assert settings.poll_interval == 5


def test_load_settings_reads_from_file(tmp_path: Path) -> None:
    (tmp_path / "settings.toml").write_text("[watchers]\nconversation_poll_interval_seconds = 15\n")
    settings = _load_watcher_settings(tmp_path)
    assert settings.poll_interval == 15


def test_load_settings_handles_corrupt_file(tmp_path: Path) -> None:
    (tmp_path / "settings.toml").write_text("this is not valid toml {{{")
    settings = _load_watcher_settings(tmp_path)
    assert settings.poll_interval == 5


def test_load_settings_handles_empty_watchers_section(tmp_path: Path) -> None:
    (tmp_path / "settings.toml").write_text("[watchers]\n")
    settings = _load_watcher_settings(tmp_path)
    assert settings.poll_interval == 5


# -- _get_llm_db_path tests --


def test_get_llm_db_path_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_USER_PATH", raising=False)
    db_path = _get_llm_db_path()
    assert db_path.name == "logs.db"
    assert "io.datasette.llm" in str(db_path)


def test_get_llm_db_path_with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_USER_PATH", "/custom/llm/path")
    db_path = _get_llm_db_path()
    assert db_path == Path("/custom/llm/path/logs.db")


# -- _get_tracked_conversation_ids tests --


def test_get_tracked_conversation_ids_empty_file(tmp_path: Path) -> None:
    log = Logger(tmp_path / "test.log")
    conversations_file = tmp_path / "events.jsonl"
    assert _get_tracked_conversation_ids(conversations_file, log) == set()


def test_get_tracked_conversation_ids_reads_cids(tmp_path: Path) -> None:
    log = Logger(tmp_path / "test.log")
    conversations_file = tmp_path / "events.jsonl"
    write_conversation_event(conversations_file, "conv-1")
    write_conversation_event(conversations_file, "conv-2")

    cids = _get_tracked_conversation_ids(conversations_file, log)
    assert cids == {"conv-1", "conv-2"}


def test_get_tracked_conversation_ids_handles_malformed_json(tmp_path: Path) -> None:
    log = Logger(tmp_path / "test.log")
    conversations_file = tmp_path / "events.jsonl"
    conversations_file.parent.mkdir(parents=True, exist_ok=True)
    with conversations_file.open("w") as f:
        f.write("not valid json\n")
        f.write(json.dumps({"conversation_id": "conv-good"}) + "\n")

    cids = _get_tracked_conversation_ids(conversations_file, log)
    assert "conv-good" in cids


def test_get_tracked_conversation_ids_handles_missing_key(tmp_path: Path) -> None:
    log = Logger(tmp_path / "test.log")
    conversations_file = tmp_path / "events.jsonl"
    conversations_file.parent.mkdir(parents=True, exist_ok=True)
    with conversations_file.open("w") as f:
        f.write(json.dumps({"no_cid_field": "value"}) + "\n")
        f.write(json.dumps({"conversation_id": "conv-ok"}) + "\n")

    cids = _get_tracked_conversation_ids(conversations_file, log)
    assert cids == {"conv-ok"}


# -- _get_existing_event_ids tests --


def test_get_existing_event_ids_empty_file(tmp_path: Path) -> None:
    log = Logger(tmp_path / "test.log")
    messages_file = tmp_path / "messages.jsonl"
    assert _get_existing_event_ids(messages_file, log) == set()


def test_get_existing_event_ids_reads_ids(tmp_path: Path) -> None:
    log = Logger(tmp_path / "test.log")
    messages_file = tmp_path / "messages.jsonl"
    messages_file.write_text(json.dumps({"event_id": "evt-1"}) + "\n" + json.dumps({"event_id": "evt-2"}) + "\n")

    ids = _get_existing_event_ids(messages_file, log)
    assert ids == {"evt-1", "evt-2"}


def test_get_existing_event_ids_handles_malformed_lines(tmp_path: Path) -> None:
    log = Logger(tmp_path / "test.log")
    messages_file = tmp_path / "messages.jsonl"
    messages_file.write_text("bad json\n" + json.dumps({"event_id": "evt-ok"}) + "\n")

    ids = _get_existing_event_ids(messages_file, log)
    assert ids == {"evt-ok"}


# -- _sync_messages tests --


def test_sync_messages_syncs_from_database(tmp_path: Path) -> None:
    log = Logger(tmp_path / "test.log")
    conversations_file = tmp_path / "conversations" / "events.jsonl"
    messages_file = tmp_path / "messages" / "events.jsonl"
    messages_file.parent.mkdir(parents=True)

    write_conversation_event(conversations_file, "conv-1")

    db_path = tmp_path / "logs.db"
    create_test_llm_db(
        db_path,
        [
            ("resp-1", "Hello", "Hi!", "claude-sonnet-4-6", "2025-01-15T10:01:00", "conv-1"),
        ],
    )

    synced = _sync_messages(db_path, conversations_file, messages_file, log)
    assert synced == 2  # 1 user + 1 assistant

    lines = messages_file.read_text().strip().split("\n")
    events = [json.loads(line) for line in lines]
    assert len(events) == 2
    roles = [e["role"] for e in events]
    assert "user" in roles
    assert "assistant" in roles


def test_sync_messages_is_idempotent(tmp_path: Path) -> None:
    log = Logger(tmp_path / "test.log")
    conversations_file = tmp_path / "conversations" / "events.jsonl"
    messages_file = tmp_path / "messages" / "events.jsonl"
    messages_file.parent.mkdir(parents=True)

    write_conversation_event(conversations_file, "conv-1")

    db_path = tmp_path / "logs.db"
    create_test_llm_db(
        db_path,
        [("resp-1", "Hello", "Hi!", "claude-sonnet-4-6", "2025-01-15T10:01:00", "conv-1")],
    )

    first_count = _sync_messages(db_path, conversations_file, messages_file, log)
    assert first_count == 2

    second_count = _sync_messages(db_path, conversations_file, messages_file, log)
    assert second_count == 0

    lines = messages_file.read_text().strip().split("\n")
    assert len(lines) == 2


def test_sync_messages_returns_zero_for_missing_db(tmp_path: Path) -> None:
    log = Logger(tmp_path / "test.log")
    conversations_file = tmp_path / "conversations" / "events.jsonl"
    messages_file = tmp_path / "messages" / "events.jsonl"

    write_conversation_event(conversations_file, "conv-1")

    result = _sync_messages(tmp_path / "nonexistent.db", conversations_file, messages_file, log)
    assert result == 0


def test_sync_messages_returns_zero_with_no_tracked_conversations(tmp_path: Path) -> None:
    log = Logger(tmp_path / "test.log")
    conversations_file = tmp_path / "conversations" / "events.jsonl"
    messages_file = tmp_path / "messages" / "events.jsonl"

    db_path = tmp_path / "logs.db"
    create_test_llm_db(db_path, [])

    result = _sync_messages(db_path, conversations_file, messages_file, log)
    assert result == 0


def test_sync_messages_only_syncs_tracked_conversations(tmp_path: Path) -> None:
    log = Logger(tmp_path / "test.log")
    conversations_file = tmp_path / "conversations" / "events.jsonl"
    messages_file = tmp_path / "messages" / "events.jsonl"
    messages_file.parent.mkdir(parents=True)

    write_conversation_event(conversations_file, "conv-tracked")

    db_path = tmp_path / "logs.db"
    create_test_llm_db(
        db_path,
        [
            ("resp-1", "Hi", "Hello!", "model", "2025-01-15T10:01:00", "conv-tracked"),
            ("resp-2", "Yo", "Hey!", "model", "2025-01-15T10:02:00", "conv-untracked"),
        ],
    )

    synced = _sync_messages(db_path, conversations_file, messages_file, log)
    assert synced == 2  # Only the tracked conversation

    lines = messages_file.read_text().strip().split("\n")
    events = [json.loads(line) for line in lines]
    for event in events:
        assert event["conversation_id"] == "conv-tracked"


def test_sync_messages_handles_prompt_only_response(tmp_path: Path) -> None:
    """A response with only a prompt (no response text) should still sync the user message."""
    log = Logger(tmp_path / "test.log")
    conversations_file = tmp_path / "conversations" / "events.jsonl"
    messages_file = tmp_path / "messages" / "events.jsonl"
    messages_file.parent.mkdir(parents=True)

    write_conversation_event(conversations_file, "conv-1")

    db_path = tmp_path / "logs.db"
    create_test_llm_db(
        db_path,
        [("resp-1", "Only a prompt", "", "model", "2025-01-15T10:01:00", "conv-1")],
    )

    synced = _sync_messages(db_path, conversations_file, messages_file, log)
    assert synced == 1  # Only the user message

    lines = messages_file.read_text().strip().split("\n")
    event = json.loads(lines[0])
    assert event["role"] == "user"


def test_sync_messages_handles_response_only(tmp_path: Path) -> None:
    """A response with only a response (no prompt) should still sync the assistant message."""
    log = Logger(tmp_path / "test.log")
    conversations_file = tmp_path / "conversations" / "events.jsonl"
    messages_file = tmp_path / "messages" / "events.jsonl"
    messages_file.parent.mkdir(parents=True)

    write_conversation_event(conversations_file, "conv-1")

    db_path = tmp_path / "logs.db"
    create_test_llm_db(
        db_path,
        [("resp-1", "", "Only a response", "model", "2025-01-15T10:01:00", "conv-1")],
    )

    synced = _sync_messages(db_path, conversations_file, messages_file, log)
    assert synced == 1  # Only the assistant message

    lines = messages_file.read_text().strip().split("\n")
    event = json.loads(lines[0])
    assert event["role"] == "assistant"


def test_sync_messages_event_format(tmp_path: Path) -> None:
    """Verify the format of synced message events."""
    log = Logger(tmp_path / "test.log")
    conversations_file = tmp_path / "conversations" / "events.jsonl"
    messages_file = tmp_path / "messages" / "events.jsonl"
    messages_file.parent.mkdir(parents=True)

    write_conversation_event(conversations_file, "conv-1")

    db_path = tmp_path / "logs.db"
    create_test_llm_db(
        db_path,
        [("resp-1", "Hello", "Hi!", "claude-sonnet-4-6", "2025-01-15T10:01:00", "conv-1")],
    )

    _sync_messages(db_path, conversations_file, messages_file, log)
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
