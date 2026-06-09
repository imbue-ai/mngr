from collections.abc import AsyncIterator
from typing import Any

import claude_agent_sdk
import pytest

from imbue.mngr_robinhood import agent_sdk
from imbue.mngr_robinhood._agent_sdk.client import _coerce_prompt_to_text

# Each pair is (our re-exported object, the real SDK object) that must be the *same* object so
# that isinstance checks and field shapes are identical across both implementations.
_REEXPORTED_TYPE_PAIRS = (
    (agent_sdk.ClaudeAgentOptions, claude_agent_sdk.ClaudeAgentOptions),
    (agent_sdk.AssistantMessage, claude_agent_sdk.AssistantMessage),
    (agent_sdk.UserMessage, claude_agent_sdk.UserMessage),
    (agent_sdk.SystemMessage, claude_agent_sdk.SystemMessage),
    (agent_sdk.ResultMessage, claude_agent_sdk.ResultMessage),
    (agent_sdk.StreamEvent, claude_agent_sdk.StreamEvent),
    (agent_sdk.TextBlock, claude_agent_sdk.TextBlock),
    (agent_sdk.ThinkingBlock, claude_agent_sdk.ThinkingBlock),
    (agent_sdk.ToolUseBlock, claude_agent_sdk.ToolUseBlock),
    (agent_sdk.ToolResultBlock, claude_agent_sdk.ToolResultBlock),
    (agent_sdk.SDKSessionInfo, claude_agent_sdk.SDKSessionInfo),
    (agent_sdk.SessionMessage, claude_agent_sdk.SessionMessage),
    (agent_sdk.HookMatcher, claude_agent_sdk.HookMatcher),
    (agent_sdk.HookContext, claude_agent_sdk.HookContext),
    (agent_sdk.PermissionResultAllow, claude_agent_sdk.PermissionResultAllow),
    (agent_sdk.PermissionResultDeny, claude_agent_sdk.PermissionResultDeny),
    (agent_sdk.ToolPermissionContext, claude_agent_sdk.ToolPermissionContext),
)

# Each pair is (our behavioral entry point, the real SDK's) that must be re-implemented by mngr
# (i.e. NOT the same object as the real SDK's).
_OVERRIDDEN_PAIRS = (
    (agent_sdk.query, claude_agent_sdk.query),
    (agent_sdk.ClaudeSDKClient, claude_agent_sdk.ClaudeSDKClient),
    (agent_sdk.list_sessions, claude_agent_sdk.list_sessions),
    (agent_sdk.get_session_info, claude_agent_sdk.get_session_info),
    (agent_sdk.get_session_messages, claude_agent_sdk.get_session_messages),
    (agent_sdk.rename_session, claude_agent_sdk.rename_session),
    (agent_sdk.tag_session, claude_agent_sdk.tag_session),
)


@pytest.mark.parametrize(("ours", "real"), _REEXPORTED_TYPE_PAIRS)
def test_types_are_reexported_identically(ours: object, real: object) -> None:
    assert ours is real


@pytest.mark.parametrize(("ours", "real"), _OVERRIDDEN_PAIRS)
def test_behavioral_entry_points_are_overridden(ours: object, real: object) -> None:
    assert ours is not real


def test_options_is_reused_verbatim() -> None:
    options = agent_sdk.ClaudeAgentOptions(model="haiku", cwd="/tmp", setting_sources=[])
    assert isinstance(options, claude_agent_sdk.ClaudeAgentOptions)


@pytest.mark.asyncio
async def test_coerce_string_prompt_returns_the_string() -> None:
    assert await _coerce_prompt_to_text("hello") == "hello"


@pytest.mark.asyncio
async def test_coerce_streaming_prompt_flattens_user_text() -> None:
    async def _stream() -> AsyncIterator[dict[str, Any]]:
        yield {"type": "user", "message": {"role": "user", "content": "first"}}
        yield {"type": "user", "message": {"role": "user", "content": "second"}}

    assert await _coerce_prompt_to_text(_stream()) == "first\nsecond"


@pytest.mark.asyncio
async def test_coerce_streaming_prompt_handles_block_list_content() -> None:
    async def _stream() -> AsyncIterator[dict[str, Any]]:
        yield {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": "block text"}]},
        }

    assert await _coerce_prompt_to_text(_stream()) == "block text"
