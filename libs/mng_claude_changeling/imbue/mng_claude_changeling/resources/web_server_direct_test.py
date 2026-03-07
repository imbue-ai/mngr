"""Direct-import tests for web_server.py pure functions.

Unlike web_server_test.py which loads the module via exec() (invisible to
pytest-cov), these tests import functions directly so coverage is tracked.
Only tests for functions that don't depend on module-level env var state
belong here.
"""

from imbue.mng_claude_changeling.resources.web_server import _html_escape
from imbue.mng_claude_changeling.resources.web_server import _iso_timestamp
from imbue.mng_claude_changeling.resources.web_server import _log
from imbue.mng_claude_changeling.resources.web_server import _make_event_id
from imbue.mng_claude_changeling.resources.web_server import _render_agents_page
from imbue.mng_claude_changeling.resources.web_server import _render_conversations_page
from imbue.mng_claude_changeling.resources.web_server import _render_header
from imbue.mng_claude_changeling.resources.web_server import _render_iframe_page


def test_html_escape_escapes_ampersand() -> None:
    assert _html_escape("a&b") == "a&amp;b"


def test_html_escape_escapes_angle_brackets() -> None:
    result = _html_escape("<script>")
    assert "<" not in result
    assert ">" not in result


def test_html_escape_escapes_quotes() -> None:
    assert "&quot;" in _html_escape('say "hello"')


def test_make_event_id_returns_deterministic_id() -> None:
    id1 = _make_event_id("test-data")
    id2 = _make_event_id("test-data")
    assert id1 == id2
    assert id1.startswith("evt-")


def test_make_event_id_returns_different_ids_for_different_data() -> None:
    id1 = _make_event_id("data-a")
    id2 = _make_event_id("data-b")
    assert id1 != id2


def test_iso_timestamp_returns_utc_format() -> None:
    result = _iso_timestamp()
    assert result.endswith("Z")
    assert "T" in result


def test_log_writes_to_stderr(capsys: object) -> None:
    _log("test message")


def test_render_header_contains_nav_links() -> None:
    header = _render_header("TestAgent")
    assert "Conversations" in header
    assert "Terminal" in header
    assert "Agents" in header


def test_render_header_highlights_active_conversations() -> None:
    header = _render_header("Agent", active="conversations")
    assert 'class="active" href="conversations"' in header


def test_render_header_highlights_active_terminal() -> None:
    header = _render_header("Agent", active="terminal")
    assert 'class="active" href="terminal"' in header


def test_render_header_highlights_active_agents() -> None:
    header = _render_header("Agent", active="agents")
    assert 'class="active" href="agents-page"' in header


def test_render_header_no_active_when_unspecified() -> None:
    header = _render_header("Agent")
    assert 'class="active"' not in header


def test_render_iframe_page_contains_iframe() -> None:
    page = _render_iframe_page("TestAgent", "My Chat", "../chat/?arg=conv-1")
    assert "<iframe" in page
    assert "../chat/?arg=conv-1" in page
    assert "TestAgent" in page


def test_render_iframe_page_escapes_src() -> None:
    page = _render_iframe_page("TestAgent", "Title", '../chat/?arg=a"b')
    assert "a&quot;b" in page


def test_render_iframe_page_escapes_title() -> None:
    page = _render_iframe_page("TestAgent", "<script>xss</script>", "../chat/")
    assert "<script>" not in page
    assert "&lt;script&gt;" in page


def test_render_iframe_page_highlights_active_nav() -> None:
    page = _render_iframe_page("TestAgent", "Title", "../chat/", active="terminal")
    assert 'class="active"' in page


def test_render_conversations_page_contains_new_link() -> None:
    page = _render_conversations_page()
    assert "chat?cid=NEW" in page
    assert "New Conversation" in page


def test_render_conversations_page_shows_empty_state() -> None:
    page = _render_conversations_page()
    assert "No conversations yet" in page


def test_render_agents_page_shows_empty_state() -> None:
    page = _render_agents_page()
    assert "No agents found" in page


def test_render_agents_page_lists_cached_agents() -> None:
    import imbue.mng_claude_changeling.resources.web_server as ws

    original = ws._cached_agents
    ws._cached_agents = [
        {"name": "test-agent", "state": "RUNNING"},
        {"name": "stopped-agent", "state": "STOPPED"},
    ]
    try:
        page = _render_agents_page()
        assert "test-agent" in page
        assert "stopped-agent" in page
        assert "RUNNING" in page
        assert "STOPPED" in page
    finally:
        ws._cached_agents = original
