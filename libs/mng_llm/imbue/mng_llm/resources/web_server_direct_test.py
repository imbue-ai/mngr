"""Direct-import tests for web_server.py pure functions.

Unlike web_server_test.py which loads the module dynamically (invisible to
pytest-cov), these tests import functions directly so coverage is tracked.
Only tests for functions that don't depend on module-level env var state
belong here.
"""

import http.client
import io
import json
import struct
import threading
from pathlib import Path

import pytest

from imbue.mng_llm.resources.web_server import _WsOutputCallback
from imbue.mng_llm.resources.web_server import _get_default_chat_model
from imbue.mng_llm.resources.web_server import _get_most_recent_conversation_id
from imbue.mng_llm.resources.web_server import _html_escape
from imbue.mng_llm.resources.web_server import _iso_timestamp
from imbue.mng_llm.resources.web_server import _log
from imbue.mng_llm.resources.web_server import _make_event_id
from imbue.mng_llm.resources.web_server import _poll_db_for_new_messages
from imbue.mng_llm.resources.web_server import _read_message_history
from imbue.mng_llm.resources.web_server import _render_agents_page
from imbue.mng_llm.resources.web_server import _render_conversations_page
from imbue.mng_llm.resources.web_server import _render_header
from imbue.mng_llm.resources.web_server import _render_iframe_page
from imbue.mng_llm.resources.web_server import _render_web_chat_page
from imbue.mng_llm.resources.web_server import _ws_accept_key
from imbue.mng_llm.resources.web_server import _ws_encode_frame
from imbue.mng_llm.resources.web_server import _ws_poll_loop
from imbue.mng_llm.resources.web_server import _ws_read_frame
from imbue.mng_llm.resources.web_server import _ws_send_json


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


def test_log_writes_to_stderr(capsys: "pytest.CaptureFixture[str]") -> None:
    _log("test message")
    captured = capsys.readouterr()
    assert "[web-server] test message" in captured.err


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
    import imbue.mng_llm.resources.web_server as ws

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


def test_register_server_creates_jsonl_file(tmp_path: Path) -> None:
    """_register_server should write a server record to the JSONL file."""
    import imbue.mng_llm.resources.web_server as ws

    original_path = ws.SERVERS_JSONL_PATH
    jsonl_path = tmp_path / "events" / "servers" / "events.jsonl"
    ws.SERVERS_JSONL_PATH = jsonl_path
    try:
        ws._register_server("web", 8080)
        assert jsonl_path.exists()
        record = json.loads(jsonl_path.read_text().strip())
        assert record["server"] == "web"
        assert record["url"] == "http://127.0.0.1:8080"
        assert record["type"] == "server_registered"
    finally:
        ws.SERVERS_JSONL_PATH = original_path


def test_get_most_recent_conversation_id_returns_none_when_empty() -> None:
    """Should return None when there are no conversations."""
    result = _get_most_recent_conversation_id()
    assert result is None


# -- Web chat function tests (only tests that don't depend on module-level state) --


def test_get_default_chat_model_returns_default() -> None:
    result = _get_default_chat_model()
    assert isinstance(result, str)
    assert len(result) > 0


def test_read_message_history_returns_empty_when_no_db() -> None:
    result = _read_message_history("nonexistent-conv")
    assert result == []


def test_render_web_chat_page_contains_chat_elements() -> None:
    page = _render_web_chat_page("TestAgent", "conv-123")
    assert "chat-messages" in page
    assert "chat-input" in page
    assert "sendMessage" in page
    assert "conv-123" in page
    assert "TestAgent" in page


def test_render_web_chat_page_escapes_script_closing_tag() -> None:
    """Verify that </script> in conversation_id cannot break out of the script block."""
    page = _render_web_chat_page("TestAgent", "</script><img src=x onerror=alert(1)>")
    # The </script> must be escaped so the HTML parser doesn't close the script tag
    assert "</script><img" not in page
    assert r"<\/script>" in page


def test_render_web_chat_page_escapes_js_injection() -> None:
    """Verify that backslash-based JS escape sequences are properly escaped."""
    page = _render_web_chat_page("TestAgent", "\\x22;alert(1)//")
    # json.dumps escapes backslashes, so \\x22 becomes \\\\x22 in the output
    assert "\\\\x22" in page


# -- HTTP handler tests --


@pytest.mark.parametrize(
    "path, expected_status, expected_in_body",
    [
        ("/", 200, None),
        ("/conversations", 200, "Conversations"),
        ("/chat?cid=test-conv-123", 200, "chat-messages"),
        ("/text_chat?cid=test-conv-456", 200, "<iframe"),
        ("/terminal", 200, "Terminal"),
        ("/agents-page", 200, "Agents"),
        ("/nonexistent", 404, None),
        ("/api/chat/history?cid=test-conv-789", 200, "messages"),
    ],
)
def test_handler_get_routes(
    web_server_test_server: tuple[object, int],
    path: str,
    expected_status: int,
    expected_in_body: str | None,
) -> None:
    _, port = web_server_test_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    assert resp.status == expected_status
    if expected_in_body is not None:
        body = resp.read().decode()
        assert expected_in_body in body
    conn.close()


def test_handler_get_chat_without_cid_redirects(
    web_server_test_server: tuple[object, int],
) -> None:
    _, port = web_server_test_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/chat")
    resp = conn.getresponse()
    assert resp.status == 302
    assert "conversations" in resp.getheader("Location", "")
    conn.close()


def test_handler_get_text_chat_without_cid_redirects(
    web_server_test_server: tuple[object, int],
) -> None:
    _, port = web_server_test_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/text_chat")
    resp = conn.getresponse()
    assert resp.status == 302
    conn.close()


def test_handler_get_api_chat_history_missing_cid(
    web_server_test_server: tuple[object, int],
) -> None:
    _, port = web_server_test_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/api/chat/history")
    resp = conn.getresponse()
    assert resp.status == 400
    body = json.loads(resp.read().decode())
    assert "error" in body
    conn.close()


def test_handler_post_unknown_endpoint_returns_404(
    web_server_test_server: tuple[object, int],
) -> None:
    _, port = web_server_test_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", "/api/chat/new")
    resp = conn.getresponse()
    assert resp.status == 404
    conn.close()


@pytest.mark.parametrize(
    "post_body, expected_status",
    [
        (json.dumps({"conversation_id": "test"}), 400),
        ("not json", 400),
    ],
)
def test_handler_post_api_chat_send_validation(
    web_server_test_server: tuple[object, int],
    post_body: str,
    expected_status: int,
) -> None:
    _, port = web_server_test_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(
        "POST",
        "/api/chat/send",
        body=post_body.encode(),
        headers={"Content-Type": "application/json"},
    )
    resp = conn.getresponse()
    assert resp.status == expected_status
    conn.close()


def test_handler_post_404(
    web_server_test_server: tuple[object, int],
) -> None:
    _, port = web_server_test_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", "/nonexistent")
    resp = conn.getresponse()
    assert resp.status == 404
    conn.close()


# -- WebSocket protocol direct tests (coverage-tracked) --


def test_ws_accept_key_produces_base64() -> None:
    result = _ws_accept_key("test-key-12345")
    assert len(result) > 0
    assert result.endswith("=") or result[-1].isalnum()


def test_ws_encode_frame_small() -> None:
    frame = _ws_encode_frame(b"hi", 0x1)
    assert frame[0] == 0x81
    assert frame[1] == 2
    assert frame[2:] == b"hi"


def test_ws_encode_frame_medium() -> None:
    data = b"a" * 130
    frame = _ws_encode_frame(data, 0x1)
    assert frame[1] == 126
    length = struct.unpack(">H", frame[2:4])[0]
    assert length == 130


def test_ws_encode_frame_large() -> None:
    data = b"b" * 70000
    frame = _ws_encode_frame(data, 0x1)
    assert frame[1] == 127
    length = struct.unpack(">Q", frame[2:10])[0]
    assert length == 70000


def test_ws_read_frame_text() -> None:
    frame = bytes([0x81, 0x03]) + b"hey"
    result = _ws_read_frame(io.BytesIO(frame))
    assert result is not None
    assert result == (0x1, b"hey")


def test_ws_read_frame_masked() -> None:
    mask = b"\x11\x22\x33\x44"
    raw = b"test"
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(raw))
    frame = bytes([0x81, 0x84]) + mask + masked
    result = _ws_read_frame(io.BytesIO(frame))
    assert result is not None
    assert result[1] == b"test"


def test_ws_read_frame_empty_input() -> None:
    assert _ws_read_frame(io.BytesIO(b"")) is None


def test_ws_read_frame_close() -> None:
    result = _ws_read_frame(io.BytesIO(bytes([0x88, 0x00])))
    assert result is not None
    assert result[0] == 0x8


def test_ws_send_json_success() -> None:
    wfile = io.BytesIO()
    lock = threading.Lock()
    ok = _ws_send_json(wfile, lock, {"hello": "world"})
    assert ok is True
    assert len(wfile.getvalue()) > 0


def test_ws_send_json_failure() -> None:
    class BadWriter:
        def write(self, data: bytes) -> None:
            raise OSError("broken")

        def flush(self) -> None:
            pass

    ok = _ws_send_json(BadWriter(), threading.Lock(), {"x": 1})
    assert ok is False


def test_ws_output_callback_sends_chunk() -> None:
    wfile = io.BytesIO()
    lock = threading.Lock()
    lines: list[str] = []
    cb = _WsOutputCallback(wfile, lock, lines)
    cb("hello\n", is_stdout=True)
    assert lines == ["hello\n"]
    assert len(wfile.getvalue()) > 0


def test_ws_output_callback_ignores_stderr() -> None:
    wfile = io.BytesIO()
    lock = threading.Lock()
    lines: list[str] = []
    cb = _WsOutputCallback(wfile, lock, lines)
    cb("error", is_stdout=False)
    assert lines == []
    assert wfile.getvalue() == b""


def test_poll_db_empty_when_no_db() -> None:
    import imbue.mng_llm.resources.web_server as ws

    original = ws.LLM_DB_PATH
    ws.LLM_DB_PATH = Path("/nonexistent/db.db")
    try:
        msgs, rowid = _poll_db_for_new_messages("conv-1", 0, set())
        assert msgs == []
        assert rowid == 0
    finally:
        ws.LLM_DB_PATH = original


def test_ws_poll_loop_exits_immediately_when_stopped() -> None:
    stop = threading.Event()
    stop.set()
    _ws_poll_loop(
        io.BytesIO(),
        threading.Lock(),
        stop,
        [False],
        [0],
        ["c"],
        set(),
    )


def test_ws_endpoint_rejects_non_websocket(
    web_server_test_server: tuple[object, int],
) -> None:
    _, port = web_server_test_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/ws?cid=test")
    resp = conn.getresponse()
    assert resp.status == 400
    conn.close()
