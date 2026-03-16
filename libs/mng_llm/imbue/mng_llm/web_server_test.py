"""Unit tests for the web_server.py resource script.

Tests the pure/near-pure functions by loading the resource module via exec().
"""

import json
import types
from pathlib import Path
from typing import Any

import pytest

from imbue.mng_llm.conftest import create_mind_conversations_table_in_test_db
from imbue.mng_llm.conftest import write_conversation_to_db
from imbue.mng_llm.provisioning import load_llm_resource


def _create_test_db_with_conversations(db_path: Path, conversations: list[tuple[str, str, str]]) -> None:
    """Create a test llm DB with mind_conversations table and rows.

    Each conversation tuple is (conversation_id, model, created_at).
    """
    create_mind_conversations_table_in_test_db(db_path)
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

    source = load_llm_resource("web_server.py")
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
    (work_dir / "minds.toml").write_text('[chat]\nmodel = "claude-sonnet-4-6"\n')
    web_server_module.AGENT_WORK_DIR = str(work_dir)

    result = web_server_module._get_default_chat_model()
    assert result == "claude-sonnet-4-6"


def test_build_template_writes_llm_template(web_server_module: Any, tmp_path: Path) -> None:
    work_dir = tmp_path / "prompt_work"
    work_dir.mkdir()
    (work_dir / "GLOBAL.md").write_text("Global instructions")
    talking_dir = work_dir / "talking"
    talking_dir.mkdir()
    (talking_dir / "PROMPT.md").write_text("Talking prompt")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    web_server_module.AGENT_WORK_DIR = str(work_dir)
    web_server_module.AGENT_STATE_DIR = str(state_dir)

    result = web_server_module._build_template()
    assert result is not None
    template_content = Path(result).read_text()
    assert template_content.startswith("system: |")
    assert "Global instructions" in template_content
    assert "Talking prompt" in template_content


def test_build_template_returns_none_when_no_work_dir(web_server_module: Any, tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    web_server_module.AGENT_WORK_DIR = ""
    web_server_module.AGENT_STATE_DIR = str(state_dir)
    result = web_server_module._build_template()
    assert result is None


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


# -- Web chat page audio button tests --


def test_render_web_chat_page_includes_audio_button_with_alert(web_server_module: Any) -> None:
    page = web_server_module._render_web_chat_page("TestAgent", "conv-audio-82741")
    assert 'id="audio-btn"' in page
    assert "Not implemented" in page


def test_render_web_chat_page_contains_sidebar(web_server_module: Any) -> None:
    page = web_server_module._render_web_chat_page("TestAgent", "conv-sidebar-82741")
    assert 'id="sidebar"' in page
    assert "sidebar-toggle" in page
    assert "TestAgent" in page


def test_render_web_chat_page_contains_chat_input(web_server_module: Any) -> None:
    page = web_server_module._render_web_chat_page("TestAgent", "conv-input-82741")
    assert 'id="chat-input"' in page
    assert "Reply..." in page
    assert "sendMessage" in page


def test_render_web_chat_page_contains_conversation_picker(web_server_module: Any) -> None:
    page = web_server_module._render_web_chat_page("TestAgent", "conv-picker-82741")
    assert "conv-picker" in page
    assert "toggleConvMenu" in page
    assert "loadConversations" in page


def test_render_web_chat_page_embeds_conversation_id(web_server_module: Any) -> None:
    page = web_server_module._render_web_chat_page("TestAgent", "conv-embed-82741")
    assert "conv-embed-82741" in page


def test_render_web_chat_page_escapes_conversation_id(web_server_module: Any) -> None:
    page = web_server_module._render_web_chat_page("TestAgent", '</script><script>alert("xss")')
    # json.dumps escapes the string and </ is replaced with <\/ to prevent script tag closing
    assert r"<\/script>" in page


def test_render_web_chat_page_contains_streaming_code(web_server_module: Any) -> None:
    page = web_server_module._render_web_chat_page("TestAgent", "conv-stream-82741")
    assert "isStreaming" in page
    assert "finishStreaming" in page
    assert "scrollToBottom" in page


def test_render_web_chat_page_contains_markdown_renderer(web_server_module: Any) -> None:
    page = web_server_module._render_web_chat_page("TestAgent", "conv-md-82741")
    assert "renderMarkdown" in page
    assert "inlineMarkdown" in page
    assert "escapeHtml" in page


# -- Sidebar rendering tests --


def test_render_sidebar_contains_nav_links(web_server_module: Any) -> None:
    sidebar = web_server_module._render_sidebar()
    assert "Conversations" in sidebar
    assert "Terminal" in sidebar
    assert "Agents" in sidebar
    assert 'id="sidebar"' in sidebar


def test_render_sidebar_highlights_active_link(web_server_module: Any) -> None:
    sidebar = web_server_module._render_sidebar(active="conversations")
    assert "sidebar-link active" in sidebar


def test_render_sidebar_shows_agent_name(web_server_module: Any) -> None:
    sidebar = web_server_module._render_sidebar(agent_name="my-agent-82741")
    assert "my-agent-82741" in sidebar
    assert "sidebar-agent-name" in sidebar


def test_render_sidebar_escapes_agent_name(web_server_module: Any) -> None:
    sidebar = web_server_module._render_sidebar(agent_name='<script>alert("xss")</script>')
    assert "<script>" not in sidebar
    assert "&lt;script&gt;" in sidebar


# -- Helper function tests --


def test_make_event_id_returns_deterministic_id(web_server_module: Any) -> None:
    id1 = web_server_module._make_event_id("test-data-82741")
    id2 = web_server_module._make_event_id("test-data-82741")
    assert id1 == id2
    assert id1.startswith("evt-")
    assert len(id1) == 4 + 32


def test_make_event_id_differs_for_different_input(web_server_module: Any) -> None:
    id1 = web_server_module._make_event_id("data-a-82741")
    id2 = web_server_module._make_event_id("data-b-82741")
    assert id1 != id2


def test_iso_timestamp_format(web_server_module: Any) -> None:
    ts = web_server_module._iso_timestamp()
    assert ts.endswith("Z")
    assert "T" in ts
    # Should have nanosecond precision (9 digits after the decimal)
    decimal_part = ts.split(".")[-1].rstrip("Z")
    assert len(decimal_part) == 9


# -- _get_default_chat_model tests --


def test_get_default_chat_model_returns_fallback_when_no_work_dir(web_server_module: Any) -> None:
    web_server_module.AGENT_WORK_DIR = ""
    result = web_server_module._get_default_chat_model()
    assert result == "claude-opus-4.6"


def test_get_default_chat_model_returns_fallback_when_no_settings_file(web_server_module: Any, tmp_path: Path) -> None:
    work_dir = tmp_path / "empty_work_82741"
    work_dir.mkdir()
    web_server_module.AGENT_WORK_DIR = str(work_dir)

    result = web_server_module._get_default_chat_model()
    assert result == "claude-opus-4.6"


# -- _read_message_history tests --


def test_read_message_history_returns_empty_when_no_db(web_server_module: Any) -> None:
    web_server_module.LLM_DB_PATH = Path("/nonexistent/path/logs.db")
    result = web_server_module._read_message_history("conv-no-db-82741")
    assert result == []


def test_read_message_history_returns_user_and_assistant_messages(web_server_module: Any) -> None:
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
        (
            "r-full-82741",
            "Hello there",
            "Hi! How can I help?",
            "test-model",
            "2026-01-01T00:00:00Z",
            "conv-full-82741",
        ),
    )
    conn.commit()
    conn.close()

    result = web_server_module._read_message_history("conv-full-82741")
    assert len(result) == 2
    assert result[0]["role"] == "user"
    assert result[0]["content"] == "Hello there"
    assert result[1]["role"] == "assistant"
    assert result[1]["content"] == "Hi! How can I help?"


def test_read_message_history_returns_empty_for_unknown_conversation(web_server_module: Any) -> None:
    import sqlite3

    db_path = web_server_module.LLM_DB_PATH
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS responses ("
        "id TEXT PRIMARY KEY, prompt TEXT, response TEXT, "
        "model TEXT, datetime_utc TEXT, conversation_id TEXT)"
    )
    conn.commit()
    conn.close()

    result = web_server_module._read_message_history("nonexistent-conv-82741")
    assert result == []


# -- Conversation page rendering with named conversations --


def test_render_conversations_page_shows_conversation_name(web_server_module: Any) -> None:
    db_path = web_server_module.LLM_DB_PATH
    create_mind_conversations_table_in_test_db(db_path)
    write_conversation_to_db(
        db_path,
        "conv-named-82741",
        model="claude-sonnet-4-6",
        tags='{"name":"My Named Chat"}',
        created_at="2026-01-01T00:00:00Z",
    )

    page = web_server_module._render_conversations_page()
    assert "My Named Chat" in page
    assert "conv-named-82741" in page


# -- _render_header tests --


def test_render_header_with_extra_right_content(web_server_module: Any) -> None:
    header = web_server_module._render_header("Agent", extra_right='<button id="custom-82741">Click</button>')
    assert 'id="custom-82741"' in header


def test_render_header_with_left_content(web_server_module: Any) -> None:
    header = web_server_module._render_header("Agent", left_content='<div id="left-82741">Custom</div>')
    assert 'id="left-82741"' in header
    # default h1 with agent name should not appear
    assert "<h1>" not in header


def test_render_header_hides_nav_when_show_nav_false(web_server_module: Any) -> None:
    header = web_server_module._render_header("Agent", show_nav=False)
    assert "Conversations" not in header
    assert "Terminal" not in header


# -- _register_conversation tests --


def test_register_conversation_creates_record(web_server_module: Any) -> None:
    import sqlite3

    web_server_module._register_conversation("conv-reg-82741")

    db_path = web_server_module.LLM_DB_PATH
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT conversation_id, tags FROM mind_conversations WHERE conversation_id = ?",
        ("conv-reg-82741",),
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == "conv-reg-82741"
    tags = json.loads(rows[0][1])
    assert tags["name"] == "(new chat)"


def test_register_conversation_ignores_duplicate(web_server_module: Any) -> None:
    import sqlite3

    web_server_module._register_conversation("conv-dup-82741")
    web_server_module._register_conversation("conv-dup-82741")

    db_path = web_server_module.LLM_DB_PATH
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT conversation_id FROM mind_conversations WHERE conversation_id = ?",
        ("conv-dup-82741",),
    ).fetchall()
    conn.close()
    assert len(rows) == 1


def test_register_conversation_noop_when_no_db_path(web_server_module: Any) -> None:
    web_server_module.LLM_DB_PATH = None
    web_server_module._register_conversation("conv-noop-82741")


# -- _get_max_response_rowid tests --


def test_get_max_response_rowid_returns_zero_when_no_db(web_server_module: Any) -> None:
    web_server_module.LLM_DB_PATH = Path("/nonexistent/path/logs.db")
    result = web_server_module._get_max_response_rowid()
    assert result == 0


def test_get_max_response_rowid_returns_zero_when_empty(web_server_module: Any) -> None:
    import sqlite3

    db_path = web_server_module.LLM_DB_PATH
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS responses ("
        "id TEXT PRIMARY KEY, prompt TEXT, response TEXT, "
        "model TEXT, datetime_utc TEXT, conversation_id TEXT)"
    )
    conn.commit()
    conn.close()

    result = web_server_module._get_max_response_rowid()
    assert result == 0


def test_get_max_response_rowid_returns_max(web_server_module: Any) -> None:
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
        ("r-max-1-82741", "q1", "a1", "model", "2026-01-01T00:00:00Z", "conv-max-82741"),
    )
    conn.execute(
        "INSERT INTO responses VALUES (?, ?, ?, ?, ?, ?)",
        ("r-max-2-82741", "q2", "a2", "model", "2026-01-02T00:00:00Z", "conv-max-82741"),
    )
    conn.commit()
    conn.close()

    result = web_server_module._get_max_response_rowid()
    assert result >= 2


# -- _find_conversation_id_after_rowid tests --


def test_find_conversation_id_after_rowid_returns_none_when_no_db(web_server_module: Any) -> None:
    web_server_module.LLM_DB_PATH = Path("/nonexistent/path/logs.db")
    result = web_server_module._find_conversation_id_after_rowid(0)
    assert result is None


def test_find_conversation_id_after_rowid_returns_none_when_no_match(web_server_module: Any) -> None:
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
        ("r-find-82741", "q", "a", "model", "2026-01-01T00:00:00Z", "conv-find-82741"),
    )
    conn.commit()
    conn.close()

    # Query with a rowid higher than what exists
    result = web_server_module._find_conversation_id_after_rowid(999999)
    assert result is None


def test_find_conversation_id_after_rowid_returns_match(web_server_module: Any) -> None:
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
        ("r-after-82741", "q", "a", "model", "2026-01-01T00:00:00Z", "conv-after-82741"),
    )
    conn.commit()
    conn.close()

    result = web_server_module._find_conversation_id_after_rowid(0)
    assert result == "conv-after-82741"


# -- _SseOutputCallback tests --


def test_sse_callback_collects_stdout_lines(web_server_module: Any) -> None:
    import io

    wfile = io.BytesIO()
    lines: list[str] = []
    callback = web_server_module._SseOutputCallback(wfile, lines)

    callback("Hello world\n", is_stdout=True)

    assert len(lines) == 1
    assert lines[0] == "Hello world\n"
    assert b"event: chunk" in wfile.getvalue()
    assert b"Hello world" in wfile.getvalue()


def test_sse_callback_ignores_stderr(web_server_module: Any) -> None:
    import io

    wfile = io.BytesIO()
    lines: list[str] = []
    callback = web_server_module._SseOutputCallback(wfile, lines)

    callback("error message", is_stdout=False)

    assert len(lines) == 0
    assert wfile.getvalue() == b""


def test_sse_callback_handles_write_failure(web_server_module: Any) -> None:
    class FailingWriter:
        def write(self, data: bytes) -> None:
            raise OSError("connection reset")

        def flush(self) -> None:
            pass

    lines: list[str] = []
    callback = web_server_module._SseOutputCallback(FailingWriter(), lines)

    callback("test line", is_stdout=True)

    assert callback.write_failed is True
    assert len(lines) == 1


# -- _read_conversations with tags tests --


def test_read_conversations_includes_conversation_name_from_tags(web_server_module: Any) -> None:
    db_path = web_server_module.LLM_DB_PATH
    create_mind_conversations_table_in_test_db(db_path)
    write_conversation_to_db(
        db_path,
        "conv-tags-82741",
        model="claude-sonnet-4-6",
        tags='{"name":"Tagged Chat"}',
        created_at="2026-01-01T00:00:00Z",
    )

    result = web_server_module._read_conversations()
    assert len(result) == 1
    assert result[0]["name"] == "Tagged Chat"


def test_read_conversations_handles_malformed_message_events(web_server_module: Any) -> None:
    db_path = web_server_module.LLM_DB_PATH
    create_mind_conversations_table_in_test_db(db_path)
    write_conversation_to_db(
        db_path,
        "conv-malform-82741",
        model="claude-sonnet-4-6",
        created_at="2026-01-01T00:00:00Z",
    )

    msg_path = web_server_module.MESSAGES_EVENTS_PATH
    msg_path.parent.mkdir(parents=True, exist_ok=True)
    msg_path.write_text("not valid json\n")

    result = web_server_module._read_conversations()
    assert len(result) == 1
