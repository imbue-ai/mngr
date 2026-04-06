"""End-to-end tests for Claude Web Chat using Playwright.

These tests start a real FastAPI server with mocked agent discovery,
then use Playwright to interact with the web UI.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any
from typing import Generator
from unittest.mock import patch

import pytest
import uvicorn
from playwright.sync_api import Page
from playwright.sync_api import expect

from imbue.claude_web_chat.agent_discovery import AgentInfo
from imbue.claude_web_chat.config import Config
from imbue.claude_web_chat.server import create_application

pytestmark = pytest.mark.acceptance

_PORT = 18765
_BASE_URL = f"http://127.0.0.1:{_PORT}"


def _make_session_file(
    projects_dir: Path,
    session_id: str,
    events: list[dict[str, Any]],
) -> Path:
    """Create a session JSONL file with the given events."""
    session_dir = projects_dir / "hash123"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / f"{session_id}.jsonl"
    content = "\n".join(json.dumps(e) for e in events) + "\n"
    session_file.write_text(content)
    return session_file


def _make_agent_fixture(
    tmp_path: Path,
    agent_id: str = "agent-test-123",
    agent_name: str = "test-agent",
    session_events: list[dict[str, Any]] | None = None,
) -> tuple[AgentInfo, Path]:
    """Set up a mock agent with session files. Returns (agent_info, session_file_path)."""
    agent_state_dir = tmp_path / "agents" / agent_id
    agent_state_dir.mkdir(parents=True)

    claude_config_dir = tmp_path / "claude_config"
    projects_dir = claude_config_dir / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)

    session_id = "e2e-session-001"
    (agent_state_dir / "claude_session_id_history").write_text(f"{session_id}\n")

    if session_events is None:
        session_events = [
            {
                "type": "user",
                "uuid": "uuid-e2e-1",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"role": "user", "content": "Hello agent!"},
            },
            {
                "type": "assistant",
                "uuid": "uuid-e2e-2",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-6",
                    "content": [{"type": "text", "text": "Hello! How can I help you?"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": 8},
                },
            },
        ]

    session_file = _make_session_file(projects_dir, session_id, session_events)

    agent_info = AgentInfo(
        id=agent_id,
        name=agent_name,
        state="RUNNING",
        agent_state_dir=str(agent_state_dir),
        claude_config_dir=str(claude_config_dir),
    )
    return agent_info, session_file


@pytest.fixture
def e2e_server(tmp_path: Path) -> Generator[tuple[str, list[AgentInfo], Path], None, None]:
    """Start the web server with mock agents for e2e testing."""
    agent_info, session_file = _make_agent_fixture(tmp_path)
    agents = [agent_info]

    config = Config(claude_web_chat_host="127.0.0.1", claude_web_chat_port=_PORT)
    app = create_application(config)

    # Patch discover_agents globally to return our mock agents
    patcher = patch("imbue.claude_web_chat.server.discover_agents", return_value=agents)
    patcher.start()

    # Patch send_message to succeed
    send_patcher = patch("imbue.claude_web_chat.server.send_message", return_value=True)
    send_patcher.start()

    server = uvicorn.Server(uvicorn.Config(app=app, host="127.0.0.1", port=_PORT, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for server to start
    for _ in range(50):
        try:
            import urllib.request

            urllib.request.urlopen(f"{_BASE_URL}/api/agents", timeout=0.5)
            break
        except Exception:
            time.sleep(0.1)

    yield _BASE_URL, agents, session_file

    patcher.stop()
    send_patcher.stop()
    server.should_exit = True
    thread.join(timeout=5.0)


def test_page_loads_and_shows_title(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """The page loads and shows the app title."""
    base_url, _, _ = e2e_server
    page.goto(base_url)
    expect(page).to_have_title("Claude Web Chat")


def test_sidebar_shows_agent_list(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """The sidebar lists the available agents."""
    base_url, agents, _ = e2e_server
    page.goto(base_url)

    # Wait for the agent list to appear
    agent_item = page.locator(".conversation-selector-item-name")
    expect(agent_item.first).to_be_visible(timeout=5000)
    expect(agent_item.first).to_have_text("test-agent")


def test_sidebar_shows_agent_state(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """The sidebar shows the agent state."""
    base_url, _, _ = e2e_server
    page.goto(base_url)

    state_label = page.locator(".conversation-selector-item-model")
    expect(state_label.first).to_be_visible(timeout=5000)
    expect(state_label.first).to_have_text("running")


def test_selecting_agent_shows_conversation(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """Clicking an agent shows its conversation history."""
    base_url, _, _ = e2e_server
    page.goto(base_url)

    # Wait for auto-select to happen (first agent is selected by default)
    # The user message should appear
    user_message = page.locator(".message-user")
    expect(user_message.first).to_be_visible(timeout=5000)
    expect(user_message.first).to_contain_text("Hello agent!")


def test_assistant_message_renders(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """Assistant messages render with markdown content."""
    base_url, _, _ = e2e_server
    page.goto(base_url)

    assistant_message = page.locator(".message-assistant")
    expect(assistant_message.first).to_be_visible(timeout=5000)
    expect(assistant_message.first).to_contain_text("Hello! How can I help you?")


def test_header_shows_agent_name(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """The header shows the selected agent's name."""
    base_url, _, _ = e2e_server
    page.goto(base_url)

    header_title = page.locator(".app-header-title")
    expect(header_title).to_be_visible(timeout=5000)
    expect(header_title).to_have_text("test-agent")


def test_message_input_visible(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """The message input is visible when an agent is selected."""
    base_url, _, _ = e2e_server
    page.goto(base_url)

    textarea = page.locator(".message-input-textbox")
    expect(textarea).to_be_visible(timeout=5000)


def test_send_button_appears_on_input(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """The send button appears when text is entered."""
    base_url, _, _ = e2e_server
    page.goto(base_url)

    textarea = page.locator(".message-input-textbox")
    expect(textarea).to_be_visible(timeout=5000)

    # Initially no send button
    send_button = page.locator(".message-input-send-button")
    expect(send_button).not_to_be_visible()

    # Type some text
    textarea.fill("test message")
    expect(send_button).to_be_visible()


def test_tool_calls_render_as_collapsible(tmp_path: Path, page: Page) -> None:
    """Tool calls render as collapsible blocks."""
    session_events = [
        {
            "type": "user",
            "uuid": "uuid-tc-1",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content": "Read test.txt"},
        },
        {
            "type": "assistant",
            "uuid": "uuid-tc-2",
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-6",
                "content": [
                    {"type": "text", "text": "Let me read that file."},
                    {"type": "tool_use", "id": "toolu_tc1", "name": "Read", "input": {"file": "test.txt"}},
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        },
        {
            "type": "user",
            "uuid": "uuid-tc-3",
            "timestamp": "2026-01-01T00:00:02Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_tc1", "content": "file contents here"},
                ],
            },
        },
    ]
    agent_info, _ = _make_agent_fixture(tmp_path, session_events=session_events)
    agents = [agent_info]

    config = Config(claude_web_chat_host="127.0.0.1", claude_web_chat_port=_PORT + 1)
    app = create_application(config)

    with (
        patch("imbue.claude_web_chat.server.discover_agents", return_value=agents),
        patch("imbue.claude_web_chat.server.send_message", return_value=True),
    ):
        server = uvicorn.Server(uvicorn.Config(app=app, host="127.0.0.1", port=_PORT + 1, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()

        for _ in range(50):
            try:
                import urllib.request

                urllib.request.urlopen(f"http://127.0.0.1:{_PORT + 1}/api/agents", timeout=0.5)
                break
            except Exception:
                time.sleep(0.1)

        try:
            page.goto(f"http://127.0.0.1:{_PORT + 1}")

            # Wait for the assistant message to render first
            assistant_msg = page.locator(".message-assistant")
            expect(assistant_msg.first).to_be_visible(timeout=10000)

            # Wait for assistant message with tool call
            tool_block = page.locator(".tool-call-block")
            expect(tool_block.first).to_be_visible(timeout=5000)

            # Tool call should show the tool name
            expect(tool_block.first).to_contain_text("Read")

            # Click to expand
            tool_header = page.locator(".tool-call-header")
            tool_header.first.click()

            # Details should be visible after expanding
            tool_details = page.locator(".tool-call-details")
            expect(tool_details.first).to_be_visible()
            expect(tool_details.first).to_contain_text("file contents here")
        finally:
            server.should_exit = True
            thread.join(timeout=5.0)


def test_sse_stream_delivers_new_events(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """New events written to the session file appear in the UI via SSE."""
    base_url, agents, session_file = e2e_server
    page.goto(base_url)

    # Wait for initial content
    expect(page.locator(".message-user").first).to_be_visible(timeout=5000)

    # Count initial messages
    initial_count = page.locator(".message-user").count()

    # Append a new event to the session file
    new_event = {
        "type": "user",
        "uuid": "uuid-new-1",
        "timestamp": "2026-01-01T00:01:00Z",
        "message": {"role": "user", "content": "This is a new message via SSE!"},
    }
    with open(session_file, "a") as f:
        f.write(json.dumps(new_event) + "\n")

    # Wait for the new message to appear (watcher polls every 1 second)
    new_message = page.locator(".message-user", has_text="This is a new message via SSE!")
    expect(new_message).to_be_visible(timeout=10000)


def test_no_agents_shows_empty_state(page: Page, tmp_path: Path) -> None:
    """When there are no agents, the sidebar shows an empty message."""
    config = Config(claude_web_chat_host="127.0.0.1", claude_web_chat_port=_PORT + 2)
    app = create_application(config)

    with (
        patch("imbue.claude_web_chat.server.discover_agents", return_value=[]),
        patch("imbue.claude_web_chat.server.send_message", return_value=True),
    ):
        server = uvicorn.Server(uvicorn.Config(app=app, host="127.0.0.1", port=_PORT + 2, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()

        for _ in range(50):
            try:
                import urllib.request

                urllib.request.urlopen(f"http://127.0.0.1:{_PORT + 2}/api/agents", timeout=0.5)
                break
            except Exception:
                time.sleep(0.1)

        try:
            page.goto(f"http://127.0.0.1:{_PORT + 2}")
            empty_msg = page.locator(".conversation-selector-empty")
            expect(empty_msg).to_be_visible(timeout=5000)
            expect(empty_msg).to_contain_text("No agents found")
        finally:
            server.should_exit = True
            thread.join(timeout=5.0)
