"""Live, clean-room verification of the documented ``sdk.query()`` interface.

Exercises only documented public names imported from ``claude_agent_sdk`` (see
https://code.claude.com/docs/en/agent-sdk/python.md), end-to-end against the real API.
These tests are opt-in and never run in CI -- see the ``sdk_live`` marker.

Every test runs the agent in an isolated temp ``cwd`` with ``setting_sources=[]`` so it does
not inherit this repo's CLAUDE.md / .claude hooks / git state (which otherwise derail prompts).
"""

from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage
from claude_agent_sdk import ResultMessage

from imbue.mngr_robinhood.testing import collect_assistant_text
from imbue.mngr_robinhood.testing import make_sdk_options

pytestmark = [pytest.mark.sdk_live, pytest.mark.tmux, pytest.mark.asyncio, pytest.mark.timeout(600)]


async def test_query_with_string_prompt_yields_assistant_then_result(
    sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path
) -> None:
    options = make_sdk_options(sdk_live_model, sdk_cwd)
    messages = [
        message async for message in sdk.query(prompt="Reply with exactly the word PINGRESPONSE.", options=options)
    ]

    # The documented stream must contain at least one AssistantMessage and exactly one terminal ResultMessage.
    assistant_messages = [m for m in messages if isinstance(m, AssistantMessage)]
    result_messages = [m for m in messages if isinstance(m, ResultMessage)]
    assert len(assistant_messages) >= 1
    assert len(result_messages) == 1

    # The ResultMessage must be the final element of the stream, per the documented contract.
    assert isinstance(messages[-1], ResultMessage)

    result = result_messages[0]
    assert result.is_error is False
    assert result.subtype == "success"
    assert isinstance(result.session_id, str) and result.session_id != ""
    assert "PINGRESPONSE" in collect_assistant_text(messages).upper()


async def test_query_with_streaming_input_prompt(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    # The documented streaming-input form: prompt is an async iterable of user message dicts.
    async def _prompt_stream() -> AsyncIterator[dict[str, Any]]:
        yield {
            "type": "user",
            "message": {"role": "user", "content": "Reply with exactly the word STREAMOK."},
        }

    options = make_sdk_options(sdk_live_model, sdk_cwd)
    messages = [message async for message in sdk.query(prompt=_prompt_stream(), options=options)]

    result_messages = [m for m in messages if isinstance(m, ResultMessage)]
    assert len(result_messages) == 1
    assert result_messages[0].is_error is False
    assert "STREAMOK" in collect_assistant_text(messages).upper()


async def test_query_respects_system_prompt(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    # A system prompt should steer output: here it adds a required marker token to every reply.
    options = make_sdk_options(
        sdk_live_model,
        sdk_cwd,
        system_prompt="You are a test fixture. End every single response with the exact marker token KIWIFRUIT9000.",
    )
    messages = [message async for message in sdk.query(prompt="Say hello in one short sentence.", options=options)]
    assert "KIWIFRUIT9000" in collect_assistant_text(messages).upper()


async def test_query_reports_selected_model_on_assistant_message(
    sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path
) -> None:
    # AssistantMessage.model is documented; selecting the haiku alias must resolve to a haiku model id.
    options = make_sdk_options(sdk_live_model, sdk_cwd)
    assistant_models: list[str] = [
        message.model
        async for message in sdk.query(prompt="Say hi.", options=options)
        if isinstance(message, AssistantMessage)
    ]
    assert len(assistant_models) >= 1
    assert all("haiku" in model.lower() for model in assistant_models)
