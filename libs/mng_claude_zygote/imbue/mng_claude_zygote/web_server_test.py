"""Unit tests for the web_server.py resource script.

Tests the pure/near-pure functions by loading the resource module via exec().
Uses monkeypatch.setenv (allowed) and SimpleNamespace (allowed sparingly)
per the style guide.
"""

import json
import types
from pathlib import Path
from typing import Any

import pytest

from imbue.mng_claude_zygote.provisioning import load_zygote_resource


@pytest.fixture()
def web_server_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Load web_server.py as a module for testing.

    Sets environment variables via monkeypatch.setenv so the module can be
    loaded without requiring a real agent state directory.
    """
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    host_dir = tmp_path / "host"
    host_dir.mkdir()

    monkeypatch.setenv("MNG_AGENT_STATE_DIR", str(agent_state_dir))
    monkeypatch.setenv("MNG_HOST_DIR", str(host_dir))
    monkeypatch.setenv("MNG_AGENT_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("MNG_AGENT_ID", "agent-test-82741")
    monkeypatch.setenv("MNG_AGENT_NAME", "test-agent-82741")
    monkeypatch.setenv("MNG_HOST_NAME", "test-host-82741")

    source = load_zygote_resource("web_server.py")
    module = types.ModuleType("web_server_test_module")
    module.__file__ = "web_server.py"
    exec(compile(source, "web_server.py", "exec"), module.__dict__)  # noqa: S102
    return module


# -- _html_escape tests --


def test_html_escape_escapes_ampersand(web_server_module: Any) -> None:
    assert web_server_module._html_escape("a&b") == "a&amp;b"


def test_html_escape_escapes_angle_brackets(web_server_module: Any) -> None:
    result = web_server_module._html_escape("<script>")
    assert "<" not in result
    assert ">" not in result


def test_html_escape_escapes_quotes(web_server_module: Any) -> None:
    assert "&quot;" in web_server_module._html_escape('say "hello"')


# -- _read_conversations tests --


def test_read_conversations_empty_when_no_event_files(web_server_module: Any) -> None:
    result = web_server_module._read_conversations()
    assert result == []


def test_read_conversations_parses_conversation_events(web_server_module: Any) -> None:
    events_path = web_server_module.CONVERSATIONS_EVENTS_PATH
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text(
        json.dumps(
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "conversation_created",
                "event_id": "evt-1",
                "source": "conversations",
                "conversation_id": "conv-abc-82741",
                "model": "claude-sonnet-4-6",
            }
        )
        + "\n"
    )

    result = web_server_module._read_conversations()

    assert len(result) == 1
    assert result[0]["conversation_id"] == "conv-abc-82741"
    assert result[0]["model"] == "claude-sonnet-4-6"


def test_read_conversations_sorted_by_most_recent(web_server_module: Any) -> None:
    events_path = web_server_module.CONVERSATIONS_EVENTS_PATH
    events_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "conversation_id": "conv-old-82741",
                "model": "m",
                "type": "conversation_created",
                "event_id": "e1",
                "source": "conversations",
            }
        ),
        json.dumps(
            {
                "timestamp": "2026-02-01T00:00:00Z",
                "conversation_id": "conv-new-82741",
                "model": "m",
                "type": "conversation_created",
                "event_id": "e2",
                "source": "conversations",
            }
        ),
    ]
    events_path.write_text("\n".join(lines) + "\n")

    result = web_server_module._read_conversations()

    assert len(result) == 2
    assert result[0]["conversation_id"] == "conv-new-82741"
    assert result[1]["conversation_id"] == "conv-old-82741"


def test_read_conversations_updates_with_message_timestamps(web_server_module: Any) -> None:
    conv_path = web_server_module.CONVERSATIONS_EVENTS_PATH
    conv_path.parent.mkdir(parents=True, exist_ok=True)
    conv_path.write_text(
        json.dumps(
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "conversation_id": "conv-1-82741",
                "model": "m",
                "type": "conversation_created",
                "event_id": "e1",
                "source": "conversations",
            }
        )
        + "\n"
    )
    msg_path = web_server_module.MESSAGES_EVENTS_PATH
    msg_path.parent.mkdir(parents=True, exist_ok=True)
    msg_path.write_text(
        json.dumps(
            {
                "timestamp": "2026-03-01T00:00:00Z",
                "conversation_id": "conv-1-82741",
                "role": "user",
                "content": "hello",
                "type": "message",
                "event_id": "e2",
                "source": "messages",
            }
        )
        + "\n"
    )

    result = web_server_module._read_conversations()
    assert result[0]["updated_at"] == "2026-03-01T00:00:00Z"


def test_read_conversations_skips_malformed_lines(web_server_module: Any) -> None:
    events_path = web_server_module.CONVERSATIONS_EVENTS_PATH
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text(
        "not valid json\n"
        + json.dumps(
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "conversation_id": "conv-good-82741",
                "model": "m",
                "type": "conversation_created",
                "event_id": "e1",
                "source": "conversations",
            }
        )
        + "\n"
    )

    result = web_server_module._read_conversations()

    assert len(result) == 1
    assert result[0]["conversation_id"] == "conv-good-82741"


# -- _register_server tests --


def test_register_server_appends_to_jsonl(web_server_module: Any) -> None:
    web_server_module._register_server("web", 8080)

    content = web_server_module.SERVERS_JSONL_PATH.read_text()
    record = json.loads(content.strip())
    assert record["server"] == "web"
    assert record["url"] == "http://127.0.0.1:8080"


def test_register_server_appends_multiple(web_server_module: Any) -> None:
    web_server_module._register_server("web", 8080)
    web_server_module._register_server("chat", 9090)

    lines = web_server_module.SERVERS_JSONL_PATH.read_text().strip().splitlines()
    assert len(lines) == 2


def test_register_server_does_nothing_when_path_is_none(web_server_module: Any) -> None:
    original = web_server_module.SERVERS_JSONL_PATH
    web_server_module.SERVERS_JSONL_PATH = None
    try:
        web_server_module._register_server("web", 8080)
    finally:
        web_server_module.SERVERS_JSONL_PATH = original


# -- HTTP handler tests --


def test_handler_class_has_get_method(web_server_module: Any) -> None:
    handler = web_server_module._WebServerHandler
    assert hasattr(handler, "do_GET")


# -- Template rendering tests --


def test_main_page_html_contains_conversation_dropdown(web_server_module: Any) -> None:
    rendered = web_server_module._MAIN_PAGE_HTML.format(agent_name="TestAgent")
    assert "conv-select" in rendered
    assert "TestAgent" in rendered
    assert "All Agents" in rendered
    assert "agents-page" in rendered


def test_main_page_html_uses_chat_ttyd_url_arg(web_server_module: Any) -> None:
    """Verify the main page links to the chat ttyd with ?arg= for conversation selection."""
    rendered = web_server_module._MAIN_PAGE_HTML.format(agent_name="Test")
    assert "../chat/?arg=" in rendered


def test_main_page_html_links_to_chat_new_for_new_conversations(web_server_module: Any) -> None:
    """Verify the new conversation button links to the chat ttyd with arg=NEW."""
    rendered = web_server_module._MAIN_PAGE_HTML.format(agent_name="Test")
    assert "../chat/?arg=NEW" in rendered


def test_agents_page_html_contains_agent_list(web_server_module: Any) -> None:
    rendered = web_server_module._AGENTS_PAGE_HTML.format(agent_name="TestAgent")
    assert "agent-list" in rendered
    assert "TestAgent" in rendered
    assert "Back to Conversations" in rendered


def test_agents_page_html_uses_agent_tmux_url_arg(web_server_module: Any) -> None:
    """Verify the agents page links to the agent-tmux ttyd with ?arg= for agent selection."""
    rendered = web_server_module._AGENTS_PAGE_HTML.format(agent_name="Test")
    assert "../agent-tmux/?arg=" in rendered
