"""Live verification of additional documented ``ClaudeSDKClient`` capabilities.

Covers introspection (``get_server_info`` / ``get_mcp_status``), streaming-input queries, the
``connect(prompt=...)`` form, and partial-message streaming (``StreamEvent``).
"""

from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from claude_agent_sdk import ResultMessage
from claude_agent_sdk import StreamEvent
from claude_agent_sdk import SystemMessage

from imbue.mngr_robinhood.testing import collect_query_messages
from imbue.mngr_robinhood.testing import drain_response
from imbue.mngr_robinhood.testing import find_result_message
from imbue.mngr_robinhood.testing import make_sdk_options

pytestmark = [pytest.mark.sdk_live, pytest.mark.tmux, pytest.mark.asyncio, pytest.mark.timeout(600)]


async def test_get_server_info_returns_a_dict(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    async with sdk.ClaudeSDKClient(options=make_sdk_options(sdk_live_model, sdk_cwd)) as client:
        await client.query("Say hi.")
        await drain_response(client)
        info = await client.get_server_info()
    assert isinstance(info, dict)
    # Documented as server info; in practice it advertises the available slash commands and output style.
    assert "commands" in info
    assert "output_style" in info


async def test_get_mcp_status_reports_no_servers_by_default(
    sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path
) -> None:
    async with sdk.ClaudeSDKClient(options=make_sdk_options(sdk_live_model, sdk_cwd)) as client:
        await client.query("Say hi.")
        await drain_response(client)
        status = await client.get_mcp_status()
    # McpStatusResponse documents an `mcpServers` list; with no servers configured it is empty.
    assert "mcpServers" in status
    assert list(status["mcpServers"]) == []


async def test_client_query_accepts_streaming_input(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    async def _prompt_stream() -> AsyncIterator[dict[str, Any]]:
        yield {"type": "user", "message": {"role": "user", "content": "Reply with exactly the word STREAMINPUTOK."}}

    async with sdk.ClaudeSDKClient(options=make_sdk_options(sdk_live_model, sdk_cwd)) as client:
        await client.query(_prompt_stream())
        messages = await drain_response(client)

    assert find_result_message(messages).is_error is False


async def test_client_connect_with_initial_prompt(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    # connect() accepts an initial prompt; the response is then read via receive_response().
    client = sdk.ClaudeSDKClient(options=make_sdk_options(sdk_live_model, sdk_cwd))
    await client.connect("Reply with exactly the word CONNECTPROMPT.")
    try:
        messages = await drain_response(client)
    finally:
        await client.disconnect()
    assert any(isinstance(m, ResultMessage) and not m.is_error for m in messages)


async def test_receive_messages_includes_system_init(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    async with sdk.ClaudeSDKClient(options=make_sdk_options(sdk_live_model, sdk_cwd)) as client:
        await client.query("Say hi.")
        collected: list[object] = []
        async for message in client.receive_messages():
            collected.append(message)
            if isinstance(message, ResultMessage):
                break
    assert any(isinstance(m, SystemMessage) and m.subtype == "init" for m in collected)


# A deliberately long prompt: the mngr target streams approximately by polling the agent's tmux
# pane every ~0.25s, so the response must render over several poll intervals for partials to appear.
_STREAMING_PROMPT = "Write a detailed story of at least six paragraphs about a lighthouse keeper and the sea."


async def test_include_partial_messages_yields_stream_events(
    sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path
) -> None:
    options = make_sdk_options(sdk_live_model, sdk_cwd, include_partial_messages=True)
    messages = await collect_query_messages(sdk, _STREAMING_PROMPT, options)
    stream_events = [m for m in messages if isinstance(m, StreamEvent)]
    assert len(stream_events) >= 1


async def test_stream_event_has_documented_fields(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    options = make_sdk_options(sdk_live_model, sdk_cwd, include_partial_messages=True)
    messages = await collect_query_messages(sdk, _STREAMING_PROMPT, options)
    stream_events = [m for m in messages if isinstance(m, StreamEvent)]
    assert len(stream_events) >= 1
    for event in stream_events:
        assert isinstance(event.uuid, str) and event.uuid != ""
        assert isinstance(event.session_id, str) and event.session_id != ""
        assert isinstance(event.event, dict)


async def test_without_partial_messages_no_stream_events(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    # By default partial streaming is off, so no StreamEvent should appear.
    messages = await collect_query_messages(sdk, "Say hi.", make_sdk_options(sdk_live_model, sdk_cwd))
    assert [m for m in messages if isinstance(m, StreamEvent)] == []
