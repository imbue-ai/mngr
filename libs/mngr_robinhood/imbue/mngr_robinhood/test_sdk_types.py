"""Live verification of the documented message and content-block type shapes.

These assert the real, documented field contracts of the dataclasses returned by the SDK.

Note (doc/implementation mismatch, flagged separately): the docs' reference table claims each
content block carries a ``type: Literal[...]`` field. The actual dataclasses do NOT expose a
``type`` attribute -- blocks are discriminated by class via ``isinstance``, which is what these
tests assert.
"""

from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage
from claude_agent_sdk import ResultMessage
from claude_agent_sdk import TextBlock
from claude_agent_sdk import ToolResultBlock
from claude_agent_sdk import ToolUseBlock
from claude_agent_sdk import UserMessage

from imbue.mngr_robinhood.testing import make_sdk_options

pytestmark = [pytest.mark.sdk_live, pytest.mark.tmux, pytest.mark.asyncio, pytest.mark.timeout(600)]


async def test_text_and_result_message_shapes(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    options = make_sdk_options(sdk_live_model, sdk_cwd)
    messages = [message async for message in sdk.query(prompt="Reply with exactly the word SHAPEOK.", options=options)]

    assistant_messages = [m for m in messages if isinstance(m, AssistantMessage)]
    assert len(assistant_messages) >= 1
    for assistant in assistant_messages:
        # AssistantMessage.content is a list of content blocks; model is a non-empty string.
        assert isinstance(assistant.content, list)
        assert isinstance(assistant.model, str) and assistant.model != ""

    text_blocks = [b for m in assistant_messages for b in m.content if isinstance(b, TextBlock)]
    assert len(text_blocks) >= 1
    for block in text_blocks:
        assert isinstance(block.text, str)

    # Exactly one terminal ResultMessage with the documented field types.
    result_messages = [m for m in messages if isinstance(m, ResultMessage)]
    assert len(result_messages) == 1
    result = result_messages[0]
    assert isinstance(result.subtype, str)
    assert isinstance(result.is_error, bool)
    assert isinstance(result.num_turns, int)
    assert isinstance(result.duration_ms, int)
    assert isinstance(result.duration_api_ms, int)
    assert isinstance(result.session_id, str) and result.session_id != ""
    assert result.total_cost_usd is None or isinstance(result.total_cost_usd, float)
    assert result.usage is None or isinstance(result.usage, dict)


async def test_tool_use_and_tool_result_block_shapes(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    options = make_sdk_options(sdk_live_model, sdk_cwd, permission_mode="bypassPermissions")
    collected: list[Any] = []
    async with sdk.ClaudeSDKClient(options=options) as client:
        await client.query("Use the Bash tool to run exactly: echo TYPECHECKTOKEN")
        async for message in client.receive_response():
            collected.append(message)

    # ToolUseBlock appears in assistant content with the documented id/name/input fields.
    tool_use_blocks = [
        block
        for message in collected
        if isinstance(message, AssistantMessage)
        for block in message.content
        if isinstance(block, ToolUseBlock)
    ]
    assert len(tool_use_blocks) >= 1
    for tool_use in tool_use_blocks:
        assert isinstance(tool_use.id, str) and tool_use.id != ""
        assert isinstance(tool_use.name, str) and tool_use.name != ""
        assert isinstance(tool_use.input, dict)

    # ToolResultBlock is delivered back (in a user message) with the documented fields.
    user_message_blocks: list[Any] = [
        block
        for message in collected
        if isinstance(message, UserMessage) and isinstance(message.content, list)
        for block in message.content
    ]
    tool_result_blocks = [block for block in user_message_blocks if isinstance(block, ToolResultBlock)]
    assert len(tool_result_blocks) >= 1
    for tool_result in tool_result_blocks:
        assert isinstance(tool_result.tool_use_id, str) and tool_result.tool_use_id != ""
        assert tool_result.content is None or isinstance(tool_result.content, (str, list))
