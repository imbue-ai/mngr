"""Live verification of the documented ``ClaudeSDKClient`` lifecycle and control surface.

Covers the async-context-manager form, explicit connect/disconnect, multi-turn on a single
connection, the lower-level ``receive_messages`` iterator, and the ``set_model`` /
``set_permission_mode`` control methods.
"""

from pathlib import Path
from types import ModuleType

import pytest
from claude_agent_sdk import AssistantMessage
from claude_agent_sdk import ResultMessage

from imbue.mngr_robinhood.testing import collect_assistant_text
from imbue.mngr_robinhood.testing import make_sdk_options

pytestmark = [pytest.mark.sdk_live, pytest.mark.tmux, pytest.mark.asyncio, pytest.mark.timeout(600)]


async def test_client_context_manager_receive_response(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    async with sdk.ClaudeSDKClient(options=make_sdk_options(sdk_live_model, sdk_cwd)) as client:
        await client.query("Reply with exactly the word ALPHATOKEN.")
        messages = [message async for message in client.receive_response()]

    # receive_response() must terminate at exactly one ResultMessage, which is the last item.
    result_messages = [m for m in messages if isinstance(m, ResultMessage)]
    assert len(result_messages) == 1
    assert isinstance(messages[-1], ResultMessage)
    assert result_messages[0].is_error is False
    assert "ALPHATOKEN" in collect_assistant_text(messages).upper()


async def test_client_explicit_connect_and_disconnect(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    # The documented manual lifecycle: construct, connect(), query, then disconnect().
    client = sdk.ClaudeSDKClient(options=make_sdk_options(sdk_live_model, sdk_cwd))
    await client.connect()
    try:
        await client.query("Reply with exactly the word BETATOKEN.")
        messages = [message async for message in client.receive_response()]
    finally:
        await client.disconnect()

    assert any(isinstance(m, ResultMessage) and not m.is_error for m in messages)
    assert "BETATOKEN" in collect_assistant_text(messages).upper()


async def test_client_supports_multiple_turns_on_one_connection(
    sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path
) -> None:
    async with sdk.ClaudeSDKClient(options=make_sdk_options(sdk_live_model, sdk_cwd)) as client:
        await client.query("Reply with exactly the word FIRSTTOKEN.")
        first_turn = [message async for message in client.receive_response()]

        await client.query("Reply with exactly the word SECONDTOKEN.")
        second_turn = [message async for message in client.receive_response()]

    assert "FIRSTTOKEN" in collect_assistant_text(first_turn).upper()
    assert "SECONDTOKEN" in collect_assistant_text(second_turn).upper()

    # The session id is stable across turns on a single connection.
    first_result = next(m for m in first_turn if isinstance(m, ResultMessage))
    second_result = next(m for m in second_turn if isinstance(m, ResultMessage))
    assert first_result.session_id == second_result.session_id


async def test_client_receive_messages_iterator(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    # receive_messages() is the lower-level stream; it does not stop on its own, so we break
    # at the ResultMessage that terminates the turn.
    async with sdk.ClaudeSDKClient(options=make_sdk_options(sdk_live_model, sdk_cwd)) as client:
        await client.query("Reply with exactly the word GAMMATOKEN.")
        collected: list[object] = []
        async for message in client.receive_messages():
            collected.append(message)
            if isinstance(message, ResultMessage):
                break

    assert isinstance(collected[-1], ResultMessage)
    assert "GAMMATOKEN" in collect_assistant_text(collected).upper()


async def test_client_set_model_then_continues(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    async with sdk.ClaudeSDKClient(options=make_sdk_options(sdk_live_model, sdk_cwd)) as client:
        await client.query("Reply with exactly the word PREMODEL.")
        _ = [message async for message in client.receive_response()]

        # Switching the model mid-session must not break the connection; the next turn still works.
        await client.set_model(sdk_live_model)
        await client.query("Reply with exactly the word POSTMODEL.")
        after = [message async for message in client.receive_response()]

    after_models = [m.model for m in after if isinstance(m, AssistantMessage)]
    assert len(after_models) >= 1
    assert all("haiku" in model.lower() for model in after_models)
    assert "POSTMODEL" in collect_assistant_text(after).upper()


async def test_client_set_permission_mode_then_continues(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    async with sdk.ClaudeSDKClient(options=make_sdk_options(sdk_live_model, sdk_cwd)) as client:
        await client.query("Reply with exactly the word PREMODE.")
        _ = [message async for message in client.receive_response()]

        # Changing the permission mode mid-session must succeed and leave the session usable.
        await client.set_permission_mode("default")
        await client.query("Reply with exactly the word POSTMODE.")
        after = [message async for message in client.receive_response()]

    assert any(isinstance(m, ResultMessage) and not m.is_error for m in after)
    assert "POSTMODE" in collect_assistant_text(after).upper()
