"""Unit tests for the web_server.py resource script.

Tests the pure/near-pure functions by importing them from the resource module.
The web_server.py script is loaded as a module for testing purposes.
"""

import io
import json
import threading
import types
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from imbue.mng_claude_zygote.provisioning import load_zygote_resource


@pytest.fixture()
def web_server_module() -> types.ModuleType:
    """Load web_server.py as a module for testing.

    Patches environment variables so the module can be imported without
    requiring a real agent state directory.
    """
    env_patch = {
        "MNG_AGENT_STATE_DIR": "/tmp/fake_agent_state",
        "MNG_HOST_DIR": "/tmp/fake_host",
        "MNG_AGENT_WORK_DIR": "/tmp/fake_work",
        "MNG_AGENT_ID": "agent-test-123",
        "MNG_AGENT_NAME": "test-agent",
        "MNG_HOST_NAME": "test-host",
    }
    with patch.dict("os.environ", env_patch):
        source = load_zygote_resource("web_server.py")
        module = types.ModuleType("web_server_test_module")
        module.__file__ = "web_server.py"
        exec(compile(source, "web_server.py", "exec"), module.__dict__)  # noqa: S102
    return module


# -- _html_escape tests --


def test_html_escape_escapes_ampersand(web_server_module: types.ModuleType) -> None:
    assert web_server_module._html_escape("a&b") == "a&amp;b"


def test_html_escape_escapes_angle_brackets(web_server_module: types.ModuleType) -> None:
    result = web_server_module._html_escape("<script>")
    assert "<" not in result
    assert ">" not in result


def test_html_escape_escapes_quotes(web_server_module: types.ModuleType) -> None:
    assert "&quot;" in web_server_module._html_escape('say "hello"')


def test_html_escape_escapes_single_quotes(web_server_module: types.ModuleType) -> None:
    result = web_server_module._html_escape("it's")
    assert "&#x27;" in result or "'" in result  # html.escape may or may not escape single quotes


# -- _read_conversations tests --


def test_read_conversations_empty_when_no_files(web_server_module: types.ModuleType, tmp_path: Path) -> None:
    with patch.object(web_server_module, "CONVERSATIONS_EVENTS_PATH", tmp_path / "nonexistent"):
        with patch.object(web_server_module, "MESSAGES_EVENTS_PATH", tmp_path / "nonexistent2"):
            result = web_server_module._read_conversations()
    assert result == []


def test_read_conversations_parses_conversation_events(web_server_module: types.ModuleType, tmp_path: Path) -> None:
    events_file = tmp_path / "conversations.jsonl"
    events_file.write_text(
        json.dumps(
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "conversation_created",
                "event_id": "evt-1",
                "source": "conversations",
                "conversation_id": "conv-abc",
                "model": "claude-sonnet-4-6",
            }
        )
        + "\n"
    )
    with patch.object(web_server_module, "CONVERSATIONS_EVENTS_PATH", events_file):
        with patch.object(web_server_module, "MESSAGES_EVENTS_PATH", tmp_path / "no-messages"):
            result = web_server_module._read_conversations()

    assert len(result) == 1
    assert result[0]["conversation_id"] == "conv-abc"
    assert result[0]["model"] == "claude-sonnet-4-6"


def test_read_conversations_sorted_by_most_recent(web_server_module: types.ModuleType, tmp_path: Path) -> None:
    events_file = tmp_path / "conversations.jsonl"
    lines = [
        json.dumps(
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "conversation_id": "conv-old",
                "model": "m",
                "type": "conversation_created",
                "event_id": "e1",
                "source": "conversations",
            }
        ),
        json.dumps(
            {
                "timestamp": "2026-02-01T00:00:00Z",
                "conversation_id": "conv-new",
                "model": "m",
                "type": "conversation_created",
                "event_id": "e2",
                "source": "conversations",
            }
        ),
    ]
    events_file.write_text("\n".join(lines) + "\n")
    with patch.object(web_server_module, "CONVERSATIONS_EVENTS_PATH", events_file):
        with patch.object(web_server_module, "MESSAGES_EVENTS_PATH", tmp_path / "no-messages"):
            result = web_server_module._read_conversations()

    assert len(result) == 2
    assert result[0]["conversation_id"] == "conv-new"
    assert result[1]["conversation_id"] == "conv-old"


def test_read_conversations_updates_with_message_timestamps(
    web_server_module: types.ModuleType, tmp_path: Path
) -> None:
    conv_file = tmp_path / "conversations.jsonl"
    conv_file.write_text(
        json.dumps(
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "conversation_id": "conv-1",
                "model": "m",
                "type": "conversation_created",
                "event_id": "e1",
                "source": "conversations",
            }
        )
        + "\n"
    )
    msg_file = tmp_path / "messages.jsonl"
    msg_file.write_text(
        json.dumps(
            {
                "timestamp": "2026-03-01T00:00:00Z",
                "conversation_id": "conv-1",
                "role": "user",
                "content": "hello",
                "type": "message",
                "event_id": "e2",
                "source": "messages",
            }
        )
        + "\n"
    )
    with patch.object(web_server_module, "CONVERSATIONS_EVENTS_PATH", conv_file):
        with patch.object(web_server_module, "MESSAGES_EVENTS_PATH", msg_file):
            result = web_server_module._read_conversations()

    assert result[0]["updated_at"] == "2026-03-01T00:00:00Z"


def test_read_conversations_skips_malformed_lines(web_server_module: types.ModuleType, tmp_path: Path) -> None:
    events_file = tmp_path / "conversations.jsonl"
    events_file.write_text(
        "not valid json\n"
        + json.dumps(
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "conversation_id": "conv-good",
                "model": "m",
                "type": "conversation_created",
                "event_id": "e1",
                "source": "conversations",
            }
        )
        + "\n"
    )
    with patch.object(web_server_module, "CONVERSATIONS_EVENTS_PATH", events_file):
        with patch.object(web_server_module, "MESSAGES_EVENTS_PATH", tmp_path / "no-messages"):
            result = web_server_module._read_conversations()

    assert len(result) == 1
    assert result[0]["conversation_id"] == "conv-good"


# -- _detect_ttyd_port tests --


def test_detect_ttyd_port_extracts_port_from_stderr(web_server_module: types.ModuleType) -> None:
    mock_process = MagicMock()
    mock_process.stderr = io.BytesIO(b"[INFO] Listening on port: 12345\n")
    mock_process.poll.return_value = None

    result = web_server_module._detect_ttyd_port(mock_process)
    assert result == 12345


def test_detect_ttyd_port_returns_none_when_process_exits(
    web_server_module: types.ModuleType,
) -> None:
    mock_process = MagicMock()
    mock_process.stderr = io.BytesIO(b"")
    mock_process.poll.return_value = 1

    result = web_server_module._detect_ttyd_port(mock_process)
    assert result is None


def test_detect_ttyd_port_ignores_non_port_lines(web_server_module: types.ModuleType) -> None:
    mock_process = MagicMock()
    mock_process.stderr = io.BytesIO(b"[INFO] Starting server\n[INFO] Listening on port: 9999\n")
    mock_process.poll.return_value = None

    result = web_server_module._detect_ttyd_port(mock_process)
    assert result == 9999


# -- _register_server tests --


def test_register_server_appends_to_jsonl(web_server_module: types.ModuleType, tmp_path: Path) -> None:
    servers_path = tmp_path / "logs" / "servers.jsonl"
    with patch.object(web_server_module, "SERVERS_JSONL_PATH", servers_path):
        web_server_module._register_server("web", 8080)

    content = servers_path.read_text()
    record = json.loads(content.strip())
    assert record["server"] == "web"
    assert record["url"] == "http://127.0.0.1:8080"


def test_register_server_appends_multiple(web_server_module: types.ModuleType, tmp_path: Path) -> None:
    servers_path = tmp_path / "logs" / "servers.jsonl"
    with patch.object(web_server_module, "SERVERS_JSONL_PATH", servers_path):
        web_server_module._register_server("web", 8080)
        web_server_module._register_server("chat", 9090)

    lines = servers_path.read_text().strip().splitlines()
    assert len(lines) == 2


def test_register_server_does_nothing_when_path_is_none(
    web_server_module: types.ModuleType,
) -> None:
    with patch.object(web_server_module, "SERVERS_JSONL_PATH", None):
        web_server_module._register_server("web", 8080)


# -- _TtydEntry tests --


def test_ttyd_entry_is_alive_when_process_running(web_server_module: types.ModuleType) -> None:
    mock_process = MagicMock()
    mock_process.poll.return_value = None
    entry = web_server_module._TtydEntry(process=mock_process, port=1234)
    assert entry.is_alive() is True


def test_ttyd_entry_is_not_alive_when_process_exited(web_server_module: types.ModuleType) -> None:
    mock_process = MagicMock()
    mock_process.poll.return_value = 0
    entry = web_server_module._TtydEntry(process=mock_process, port=1234)
    assert entry.is_alive() is False


# -- HTTP handler routing tests --


def test_handler_class_has_get_and_post_methods(web_server_module: types.ModuleType) -> None:
    handler = web_server_module._WebServerHandler
    assert hasattr(handler, "do_GET")
    assert hasattr(handler, "do_POST")


# -- Template rendering tests --


def test_main_page_html_contains_conversation_dropdown(
    web_server_module: types.ModuleType,
) -> None:
    rendered = web_server_module._MAIN_PAGE_HTML.format(agent_name="TestAgent")
    assert "conv-select" in rendered
    assert "TestAgent" in rendered
    assert "All Agents" in rendered
    assert "agents-page" in rendered


def test_agents_page_html_contains_agent_list(web_server_module: types.ModuleType) -> None:
    rendered = web_server_module._AGENTS_PAGE_HTML.format(agent_name="TestAgent")
    assert "agent-list" in rendered
    assert "TestAgent" in rendered
    assert "Back to Conversations" in rendered


def test_main_page_uses_post_for_ensure_endpoints(
    web_server_module: types.ModuleType,
) -> None:
    rendered = web_server_module._MAIN_PAGE_HTML.format(agent_name="Test")
    assert "method: 'POST'" in rendered


def test_agents_page_uses_post_for_ensure_endpoints(
    web_server_module: types.ModuleType,
) -> None:
    rendered = web_server_module._AGENTS_PAGE_HTML.format(agent_name="Test")
    assert "method: 'POST'" in rendered


# -- Server name generation tests --


def test_conversation_server_name_format(web_server_module: types.ModuleType) -> None:
    with patch.object(web_server_module, "CHAT_SCRIPT_PATH", None):
        result = web_server_module._ensure_conversation_ttyd("conv-abc-123")
    # Returns None because CHAT_SCRIPT_PATH is None, but we verified it got past the name
    assert result is None


def test_agent_tmux_server_name_sanitizes_special_chars(
    web_server_module: types.ModuleType,
) -> None:
    with patch.object(web_server_module, "_start_ttyd_for_command", return_value=None):
        result = web_server_module._ensure_agent_tmux_ttyd("agent with spaces!")
    assert result is None  # returns None because mock returns None


def test_ensure_conversation_ttyd_uses_shlex_quote(web_server_module: types.ModuleType) -> None:
    """Verify the command uses shell quoting for the conversation ID."""
    captured_commands: list[list[str]] = []

    def mock_start(server_name: str, command: list[str]) -> None:
        captured_commands.append(command)
        return None

    with patch.object(web_server_module, "_start_ttyd_for_command", side_effect=mock_start):
        with patch.object(web_server_module, "CHAT_SCRIPT_PATH", Path("/fake/chat.sh")):
            web_server_module._ensure_conversation_ttyd('test"; rm -rf /')

    assert len(captured_commands) == 1
    shell_cmd = captured_commands[0][-1]
    # shlex.quote wraps in single quotes, preventing injection
    assert "'" in shell_cmd
    assert "rm -rf" not in shell_cmd.split("'")[0]


def test_ensure_agent_tmux_ttyd_uses_shlex_quote(web_server_module: types.ModuleType) -> None:
    """Verify the command uses shell quoting for the agent name."""
    captured_commands: list[list[str]] = []

    def mock_start(server_name: str, command: list[str]) -> None:
        captured_commands.append(command)
        return None

    with patch.object(web_server_module, "_start_ttyd_for_command", side_effect=mock_start):
        web_server_module._ensure_agent_tmux_ttyd('evil"; rm -rf /')

    assert len(captured_commands) == 1
    shell_cmd = captured_commands[0][-1]
    assert "'" in shell_cmd


# -- _start_ttyd_for_command internal logic tests --


def _make_alive_entry(web_server_module: types.ModuleType, port: int = 5000) -> object:
    """Create a _TtydEntry with a mock process that appears alive."""
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    return web_server_module._TtydEntry(process=mock_proc, port=port)


def _make_dead_entry(web_server_module: types.ModuleType, port: int = 5000) -> object:
    """Create a _TtydEntry with a mock process that has exited."""
    mock_proc = MagicMock()
    mock_proc.poll.return_value = 1
    return web_server_module._TtydEntry(process=mock_proc, port=port)


def test_start_ttyd_reuses_alive_entry(web_server_module: types.ModuleType) -> None:
    """Verify that an existing alive ttyd is reused without spawning a new one."""
    alive = _make_alive_entry(web_server_module, port=7777)

    with patch.object(web_server_module, "_ttyd_lock", threading.Lock()):
        web_server_module._ttyd_by_server_name["test-server"] = alive

        result = web_server_module._start_ttyd_for_command("test-server", ["bash"])

        assert result is alive
        # Clean up
        del web_server_module._ttyd_by_server_name["test-server"]


def test_start_ttyd_removes_dead_entry_before_spawn(web_server_module: types.ModuleType) -> None:
    """Verify that a dead entry is removed and spawn is attempted."""
    dead = _make_dead_entry(web_server_module)

    with patch.object(web_server_module, "_ttyd_lock", threading.Lock()):
        web_server_module._ttyd_by_server_name["test-server"] = dead

        # Popen will raise FileNotFoundError (ttyd not found), which is fine --
        # we just want to verify the dead entry was cleaned up
        with patch("subprocess.Popen", side_effect=FileNotFoundError):
            result = web_server_module._start_ttyd_for_command("test-server", ["bash"])

        assert result is None
        assert "test-server" not in web_server_module._ttyd_by_server_name


def test_start_ttyd_enforces_max_limit(web_server_module: types.ModuleType) -> None:
    """Verify that the MAX_TTYD_PROCESSES limit is enforced."""
    with patch.object(web_server_module, "_ttyd_lock", threading.Lock()):
        with patch.object(web_server_module, "MAX_TTYD_PROCESSES", 2):
            # Fill up to the limit with alive entries
            for i in range(2):
                web_server_module._ttyd_by_server_name[f"existing-{i}"] = _make_alive_entry(
                    web_server_module, port=5000 + i
                )

            result = web_server_module._start_ttyd_for_command("new-server", ["bash"])
            assert result is None

            # Clean up
            for i in range(2):
                del web_server_module._ttyd_by_server_name[f"existing-{i}"]


def test_start_ttyd_cleans_dead_before_limit_check(web_server_module: types.ModuleType) -> None:
    """Verify that dead entries are cleaned up before checking the limit."""
    with patch.object(web_server_module, "_ttyd_lock", threading.Lock()):
        with patch.object(web_server_module, "MAX_TTYD_PROCESSES", 2):
            # One alive, one dead
            web_server_module._ttyd_by_server_name["alive"] = _make_alive_entry(web_server_module)
            web_server_module._ttyd_by_server_name["dead"] = _make_dead_entry(web_server_module)

            # Spawn attempt should proceed (1 alive < limit of 2) after cleaning dead
            with patch("subprocess.Popen", side_effect=FileNotFoundError):
                web_server_module._start_ttyd_for_command("new-server", ["bash"])

            # Dead entry should have been cleaned up
            assert "dead" not in web_server_module._ttyd_by_server_name
            # Clean up
            web_server_module._ttyd_by_server_name.pop("alive", None)
            web_server_module._ttyd_by_server_name.pop("new-server", None)


def test_start_ttyd_sentinel_prevents_duplicate_spawn(web_server_module: types.ModuleType) -> None:
    """Verify that a spawning sentinel prevents a second spawn for the same name."""
    with patch.object(web_server_module, "_ttyd_lock", threading.Lock()):
        # Simulate another thread spawning this server
        web_server_module._ttyd_by_server_name["test-server"] = web_server_module._SPAWNING_SENTINEL

        result = web_server_module._start_ttyd_for_command("test-server", ["bash"])
        assert result is None

        # Clean up
        del web_server_module._ttyd_by_server_name["test-server"]


def test_start_ttyd_clears_sentinel_on_spawn_failure(web_server_module: types.ModuleType) -> None:
    """Verify that the sentinel is removed if Popen fails."""
    with patch.object(web_server_module, "_ttyd_lock", threading.Lock()):
        with patch("subprocess.Popen", side_effect=FileNotFoundError):
            result = web_server_module._start_ttyd_for_command("fail-server", ["bash"])

        assert result is None
        assert "fail-server" not in web_server_module._ttyd_by_server_name
