"""Unit tests for the web_server.py resource script.

Tests the pure/near-pure functions by loading the resource module via exec().
"""

import json
import types
from pathlib import Path
from typing import Any

import pytest

from imbue.mng_claude_changeling.conftest import create_changeling_conversations_table_in_test_db
from imbue.mng_claude_changeling.conftest import write_conversation_to_db
from imbue.mng_claude_changeling.provisioning import load_changeling_resource


def _create_test_db_with_conversations(db_path: Path, conversations: list[tuple[str, str, str]]) -> None:
    """Create a test llm DB with changeling_conversations table and rows.

    Each conversation tuple is (conversation_id, model, created_at).
    """
    create_changeling_conversations_table_in_test_db(db_path)
    for conversation_id, model, created_at in conversations:
        write_conversation_to_db(db_path, conversation_id, model=model, created_at=created_at)


@pytest.fixture()
def web_server_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Load web_server.py as a module for testing."""
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    llm_data_dir = agent_state_dir / "llm_data"
    llm_data_dir.mkdir(parents=True)

    monkeypatch.setenv("MNG_AGENT_STATE_DIR", str(agent_state_dir))
    monkeypatch.setenv("MNG_HOST_DIR", str(host_dir))
    monkeypatch.setenv("MNG_AGENT_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("MNG_AGENT_ID", "agent-test-82741")
    monkeypatch.setenv("MNG_AGENT_NAME", "test-agent-82741")
    monkeypatch.setenv("MNG_HOST_NAME", "test-host-82741")
    monkeypatch.setenv("LLM_USER_PATH", str(llm_data_dir))

    source = load_changeling_resource("web_server.py")
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


def test_read_conversations_empty_when_no_db(web_server_module: Any) -> None:
    result = web_server_module._read_conversations()
    assert result == []


def test_read_conversations_parses_from_db(web_server_module: Any) -> None:
    db_path = web_server_module.LLM_DB_PATH
    _create_test_db_with_conversations(
        db_path,
        [("conv-abc-82741", "claude-sonnet-4-6", "2026-01-01T00:00:00Z")],
    )

    result = web_server_module._read_conversations()

    assert len(result) == 1
    assert result[0]["conversation_id"] == "conv-abc-82741"
    assert result[0]["model"] == "claude-sonnet-4-6"


def test_read_conversations_sorted_by_most_recent(web_server_module: Any) -> None:
    db_path = web_server_module.LLM_DB_PATH
    _create_test_db_with_conversations(
        db_path,
        [
            ("conv-old-82741", "claude-sonnet-4-6", "2026-01-01T00:00:00Z"),
            ("conv-new-82741", "claude-sonnet-4-6", "2026-02-01T00:00:00Z"),
        ],
    )

    result = web_server_module._read_conversations()

    assert len(result) == 2
    assert result[0]["conversation_id"] == "conv-new-82741"
    assert result[1]["conversation_id"] == "conv-old-82741"


def test_read_conversations_updates_with_message_timestamps(web_server_module: Any) -> None:
    db_path = web_server_module.LLM_DB_PATH
    _create_test_db_with_conversations(
        db_path,
        [("conv-1-82741", "claude-sonnet-4-6", "2026-01-01T00:00:00Z")],
    )

    msg_path = web_server_module.MESSAGES_EVENTS_PATH
    msg_path.parent.mkdir(parents=True, exist_ok=True)
    msg_event = json.dumps(
        {
            "timestamp": "2026-03-01T00:00:00Z",
            "conversation_id": "conv-1-82741",
            "role": "user",
            "content": "hello",
            "type": "message",
            "event_id": "evt-msg-conv-1-82741",
            "source": "messages",
        }
    )
    msg_path.write_text(msg_event + "\n")

    result = web_server_module._read_conversations()
    assert result[0]["updated_at"] == "2026-03-01T00:00:00Z"


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


# -- Page rendering tests --


def test_render_conversations_page_contains_new_conversation_link(web_server_module: Any) -> None:
    page = web_server_module._render_conversations_page()
    assert "chat?cid=NEW" in page
    assert "New Conversation" in page


def test_render_conversations_page_contains_header_links(web_server_module: Any) -> None:
    page = web_server_module._render_conversations_page()
    assert "terminal" in page
    assert "Terminal" in page
    assert "agents-page" in page
    assert "Agents" in page
    assert "conversations" in page
    assert "Conversations" in page


def test_render_conversations_page_shows_empty_state_with_no_conversations(web_server_module: Any) -> None:
    page = web_server_module._render_conversations_page()
    assert "No conversations yet" in page


def test_render_conversations_page_lists_conversations(web_server_module: Any) -> None:
    db_path = web_server_module.LLM_DB_PATH
    _create_test_db_with_conversations(
        db_path,
        [("conv-render-82741", "claude-sonnet-4-6", "2026-01-01T00:00:00Z")],
    )

    page = web_server_module._render_conversations_page()
    assert "conv-render-82741" in page
    assert "chat?cid=conv-render-82741" in page


def test_render_agents_page_contains_header_links(web_server_module: Any) -> None:
    page = web_server_module._render_agents_page()
    assert "terminal" in page
    assert "Terminal" in page
    assert "conversations" in page
    assert "Conversations" in page


def test_render_agents_page_shows_empty_state(web_server_module: Any) -> None:
    page = web_server_module._render_agents_page()
    assert "No agents found" in page


def test_render_agents_page_lists_agents_with_state(web_server_module: Any) -> None:
    web_server_module._cached_agents = [
        {"name": "my-agent-82741", "state": "RUNNING"},
        {"name": "stopped-agent-82741", "state": "STOPPED"},
    ]
    try:
        page = web_server_module._render_agents_page()
        assert "my-agent-82741" in page
        assert "stopped-agent-82741" in page
        assert "RUNNING" in page
        assert "STOPPED" in page
    finally:
        web_server_module._cached_agents = []


# -- Iframe page rendering tests --


def test_render_iframe_page_contains_iframe_with_src(web_server_module: Any) -> None:
    page = web_server_module._render_iframe_page("TestAgent", "My Chat", "../chat/?arg=conv-1")
    assert "<iframe" in page
    assert "../chat/?arg=conv-1" in page
    assert "iframe-layout" in page
    assert "iframe-container" in page


def test_render_iframe_page_contains_header(web_server_module: Any) -> None:
    page = web_server_module._render_iframe_page("TestAgent", "My Chat", "../chat/?arg=conv-1", active="conversations")
    assert "TestAgent" in page
    assert "Conversations" in page
    assert "Terminal" in page
    assert "Agents" in page


def test_render_iframe_page_highlights_active_nav(web_server_module: Any) -> None:
    page = web_server_module._render_iframe_page("TestAgent", "Title", "../chat/", active="terminal")
    assert 'class="active"' in page


def test_render_iframe_page_escapes_src(web_server_module: Any) -> None:
    page = web_server_module._render_iframe_page("TestAgent", "Title", '../chat/?arg=a"b')
    assert "a&quot;b" in page


def test_render_iframe_page_escapes_title(web_server_module: Any) -> None:
    page = web_server_module._render_iframe_page("TestAgent", "</title><script>xss</script>", "../chat/")
    assert "<script>" not in page
    assert "&lt;/title&gt;" in page


# -- Main page tests --


def test_get_most_recent_conversation_id_returns_none_when_no_conversations(
    web_server_module: Any,
) -> None:
    result = web_server_module._get_most_recent_conversation_id()
    assert result is None


def test_get_most_recent_conversation_id_returns_most_recent(
    web_server_module: Any,
) -> None:
    db_path = web_server_module.LLM_DB_PATH
    _create_test_db_with_conversations(
        db_path,
        [
            ("conv-old-82741", "claude-sonnet-4-6", "2026-01-01T00:00:00Z"),
            ("conv-new-82741", "claude-sonnet-4-6", "2026-02-01T00:00:00Z"),
        ],
    )

    result = web_server_module._get_most_recent_conversation_id()
    assert result == "conv-new-82741"


# -- Header rendering tests --


def test_render_header_highlights_active_conversations(web_server_module: Any) -> None:
    header = web_server_module._render_header("Agent", active="conversations")
    assert 'class="active" href="conversations"' in header


def test_render_header_highlights_active_terminal(web_server_module: Any) -> None:
    header = web_server_module._render_header("Agent", active="terminal")
    assert 'class="active" href="terminal"' in header


def test_render_header_highlights_active_agents(web_server_module: Any) -> None:
    header = web_server_module._render_header("Agent", active="agents")
    assert 'class="active" href="agents-page"' in header


def test_render_header_no_active_when_unspecified(web_server_module: Any) -> None:
    header = web_server_module._render_header("Agent")
    assert 'class="active"' not in header


# -- API function tests (env-var dependent, only testable via dynamic module) --


def test_get_default_chat_model_reads_from_settings(web_server_module: Any, tmp_path: Path) -> None:
    work_dir = tmp_path / "chat_settings_work"
    work_dir.mkdir()
    (work_dir / "changelings.toml").write_text('[chat]\nmodel = "claude-sonnet-4-6"\n')
    web_server_module.AGENT_WORK_DIR = str(work_dir)

    result = web_server_module._get_default_chat_model()
    assert result == "claude-sonnet-4-6"


def test_get_system_prompt_reads_files(web_server_module: Any, tmp_path: Path) -> None:
    work_dir = tmp_path / "prompt_work"
    work_dir.mkdir()
    (work_dir / "GLOBAL.md").write_text("Global instructions")
    talking_dir = work_dir / "talking"
    talking_dir.mkdir()
    (talking_dir / "PROMPT.md").write_text("Talking prompt")
    web_server_module.AGENT_WORK_DIR = str(work_dir)

    result = web_server_module._get_system_prompt()
    assert "Global instructions" in result
    assert "Talking prompt" in result


def test_get_system_prompt_returns_empty_when_no_work_dir(web_server_module: Any) -> None:
    web_server_module.AGENT_WORK_DIR = ""
    result = web_server_module._get_system_prompt()
    assert result == ""


def test_read_message_history_skips_injected_prompts(web_server_module: Any) -> None:
    import sqlite3

    db_path = web_server_module.LLM_DB_PATH
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS responses ("
        "id TEXT PRIMARY KEY, prompt TEXT, response TEXT, "
        "model TEXT, datetime_utc TEXT, conversation_id TEXT)"
    )
    conn.execute(
        "INSERT INTO responses VALUES (?, ?, ?, ?, ?, ?)",
        ("r1-82741", "...", "injected response", "test-model", "2026-01-01T00:00:00Z", "conv-skip-82741"),
    )
    conn.commit()
    conn.close()

    result = web_server_module._read_message_history("conv-skip-82741")
    assert len(result) == 1
    assert result[0]["role"] == "assistant"
