"""Live verification of the documented field-level contracts of SDK message types.

Complements ``test_sdk_types.py`` (which checks the block/message classes) by asserting the
documented sub-fields: ``SystemMessage`` init data, ``ResultMessage`` usage/cost/duration/model
usage, and ``AssistantMessage`` metadata.
"""

from pathlib import Path
from types import ModuleType

import pytest
from claude_agent_sdk import AssistantMessage
from claude_agent_sdk import SystemMessage
from claude_agent_sdk import TextBlock
from claude_agent_sdk import ToolUseBlock

from imbue.mngr_robinhood.testing import collect_assistant_text
from imbue.mngr_robinhood.testing import collect_query_messages
from imbue.mngr_robinhood.testing import find_result_message
from imbue.mngr_robinhood.testing import make_sdk_options

pytestmark = [pytest.mark.sdk_live, pytest.mark.tmux, pytest.mark.asyncio, pytest.mark.timeout(600)]


async def test_system_init_message_is_emitted(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    messages = await collect_query_messages(sdk, "Say hi.", make_sdk_options(sdk_live_model, sdk_cwd))
    system_messages = [m for m in messages if isinstance(m, SystemMessage)]
    assert any(m.subtype == "init" for m in system_messages)


async def test_system_init_data_carries_session_and_model(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    messages = await collect_query_messages(sdk, "Say hi.", make_sdk_options(sdk_live_model, sdk_cwd))
    init = next(m for m in messages if isinstance(m, SystemMessage) and m.subtype == "init")
    assert isinstance(init.data, dict)
    assert isinstance(init.data["session_id"], str) and init.data["session_id"] != ""
    assert "haiku" in str(init.data["model"]).lower()


async def test_system_init_data_lists_available_tools(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    messages = await collect_query_messages(sdk, "Say hi.", make_sdk_options(sdk_live_model, sdk_cwd))
    init = next(m for m in messages if isinstance(m, SystemMessage) and m.subtype == "init")
    assert isinstance(init.data["tools"], list)
    assert "Bash" in init.data["tools"]


async def test_system_init_data_reports_cwd(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    messages = await collect_query_messages(sdk, "Say hi.", make_sdk_options(sdk_live_model, sdk_cwd))
    init = next(m for m in messages if isinstance(m, SystemMessage) and m.subtype == "init")
    # The reported cwd should resolve to the directory we asked the agent to run in.
    assert Path(init.data["cwd"]).resolve() == sdk_cwd.resolve()


async def test_result_usage_has_integer_token_counts(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    messages = await collect_query_messages(sdk, "Say hi.", make_sdk_options(sdk_live_model, sdk_cwd))
    result = find_result_message(messages)
    assert isinstance(result.usage, dict)
    assert isinstance(result.usage["input_tokens"], int)
    assert isinstance(result.usage["output_tokens"], int)
    assert result.usage["output_tokens"] > 0


async def test_result_total_cost_is_positive(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    # total_cost_usd is not present in claude's native session JSONL (it is a stream-json
    # ``result``-event field). The real SDK reports it directly; the mngr transport computes an
    # approximate cost from the turn's accumulated token usage times a per-model price table.
    messages = await collect_query_messages(sdk, "Say hi.", make_sdk_options(sdk_live_model, sdk_cwd))
    result = find_result_message(messages)
    assert isinstance(result.total_cost_usd, float)
    assert result.total_cost_usd > 0.0


async def test_result_model_usage_keyed_by_model(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    messages = await collect_query_messages(sdk, "Say hi.", make_sdk_options(sdk_live_model, sdk_cwd))
    result = find_result_message(messages)
    assert isinstance(result.model_usage, dict)
    assert len(result.model_usage) >= 1
    assert any("haiku" in model_id.lower() for model_id in result.model_usage)


async def test_result_durations_are_positive(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    messages = await collect_query_messages(sdk, "Say hi.", make_sdk_options(sdk_live_model, sdk_cwd))
    result = find_result_message(messages)
    assert result.duration_ms > 0
    assert result.duration_api_ms > 0


async def test_result_turn_count_is_at_least_one(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    messages = await collect_query_messages(sdk, "Say hi.", make_sdk_options(sdk_live_model, sdk_cwd))
    result = find_result_message(messages)
    assert result.num_turns >= 1


async def test_result_session_id_is_uuid_like(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    messages = await collect_query_messages(sdk, "Say hi.", make_sdk_options(sdk_live_model, sdk_cwd))
    result = find_result_message(messages)
    # Canonical UUID form: 36 chars, 4 hyphens.
    assert len(result.session_id) == 36
    assert result.session_id.count("-") == 4


async def test_result_uuid_field_is_present(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    messages = await collect_query_messages(sdk, "Say hi.", make_sdk_options(sdk_live_model, sdk_cwd))
    result = find_result_message(messages)
    assert isinstance(result.uuid, str) and result.uuid != ""


async def test_result_result_text_matches_assistant_text(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    messages = await collect_query_messages(
        sdk, "Reply with exactly the word RESULTECHO.", make_sdk_options(sdk_live_model, sdk_cwd)
    )
    result = find_result_message(messages)
    assert result.result is not None
    assert "RESULTECHO" in result.result.upper()
    assert "RESULTECHO" in collect_assistant_text(messages).upper()


async def test_successful_result_flags(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    messages = await collect_query_messages(sdk, "Say hi.", make_sdk_options(sdk_live_model, sdk_cwd))
    result = find_result_message(messages)
    assert result.subtype == "success"
    assert result.is_error is False
    assert result.permission_denials == [] or result.permission_denials is None


async def test_assistant_message_metadata(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    messages = await collect_query_messages(sdk, "Say hi.", make_sdk_options(sdk_live_model, sdk_cwd))
    assistant_messages = [m for m in messages if isinstance(m, AssistantMessage)]
    assert len(assistant_messages) >= 1
    for assistant in assistant_messages:
        assert assistant.message_id is None or isinstance(assistant.message_id, str)
        assert assistant.usage is None or isinstance(assistant.usage, dict)


async def test_assistant_message_parent_tool_use_id_none_at_top_level(
    sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path
) -> None:
    messages = await collect_query_messages(sdk, "Say hi.", make_sdk_options(sdk_live_model, sdk_cwd))
    assistant_messages = [m for m in messages if isinstance(m, AssistantMessage)]
    # Top-level (non-subagent) assistant turns have no parent tool use.
    assert all(m.parent_tool_use_id is None for m in assistant_messages)


async def test_text_reply_includes_text_blocks_and_no_tool_use(
    sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path
) -> None:
    messages = await collect_query_messages(
        sdk, "Reply with one short sentence and do not use any tools.", make_sdk_options(sdk_live_model, sdk_cwd)
    )
    assistant_blocks = [b for m in messages if isinstance(m, AssistantMessage) for b in m.content]
    # A plain text reply must produce at least one TextBlock (it may also contain a ThinkingBlock)
    # and must not invoke any tools.
    assert any(isinstance(b, TextBlock) for b in assistant_blocks)
    assert not any(isinstance(b, ToolUseBlock) for b in assistant_blocks)


async def test_text_block_has_no_type_attribute(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    # Documents the doc/implementation mismatch: the docs claim a `type` field on blocks, but the
    # real dataclass exposes none. This assertion passes today and will fail if the SDK adds it
    # (at which point the docs and implementation would finally agree).
    messages = await collect_query_messages(sdk, "Say hi.", make_sdk_options(sdk_live_model, sdk_cwd))
    text_blocks = [
        b for m in messages if isinstance(m, AssistantMessage) for b in m.content if isinstance(b, TextBlock)
    ]
    assert len(text_blocks) >= 1
    assert all(not hasattr(b, "type") for b in text_blocks)
