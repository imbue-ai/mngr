"""Live verification of advanced documented session behavior.

Covers session continuation (``resume`` / ``fork_session`` / ``continue_conversation``), the
``SDKSessionInfo`` / ``SessionMessage`` field contracts, the ``rename`` / ``tag`` mutators, and
``list_sessions`` paging.
"""

from pathlib import Path
from types import ModuleType

import pytest
from claude_agent_sdk import AssistantMessage
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk import SDKSessionInfo
from claude_agent_sdk import SessionMessage
from claude_agent_sdk import TextBlock

from imbue.mngr_robinhood.testing import collect_assistant_text
from imbue.mngr_robinhood.testing import collect_query_messages
from imbue.mngr_robinhood.testing import find_result_message
from imbue.mngr_robinhood.testing import make_sdk_options

pytestmark = [pytest.mark.sdk_live, pytest.mark.tmux, pytest.mark.asyncio, pytest.mark.timeout(600)]


async def _seed_session(sdk: ModuleType, model: str, cwd: Path, prompt: str) -> str:
    messages = await collect_query_messages(sdk, prompt, make_sdk_options(model, cwd))
    return find_result_message(messages).session_id


async def test_resume_continues_same_session_id(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk, sdk_live_model, sdk_cwd, "Reply with OK.")
    messages = await collect_query_messages(
        sdk, "Reply with OK again.", make_sdk_options(sdk_live_model, sdk_cwd, resume=session_id)
    )
    assert find_result_message(messages).session_id == session_id


async def test_resume_preserves_conversation_memory(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(
        sdk, sdk_live_model, sdk_cwd, "Remember that the important word for this session is FALCONXYZ. Reply OK."
    )
    messages = await collect_query_messages(
        sdk,
        "What is the important word? Reply with just the word.",
        make_sdk_options(sdk_live_model, sdk_cwd, resume=session_id),
    )
    assert "FALCONXYZ" in collect_assistant_text(messages).upper()


async def test_fork_session_creates_new_session_id(
    sdk: ModuleType, requires_native_sdk: None, sdk_live_model: str, sdk_cwd: Path
) -> None:
    # fork_session is real-SDK-only: claude's --fork-session does not assign a new session id when
    # driven interactively over an adopted, resumed session (the mngr transport), so the mngr-backed
    # SDK raises AgentSdkNotImplementedError for it instead of producing a wrong/duplicate id.
    session_id = await _seed_session(sdk, sdk_live_model, sdk_cwd, "Reply with OK.")
    messages = await collect_query_messages(
        sdk, "Reply with OK.", make_sdk_options(sdk_live_model, sdk_cwd, resume=session_id, fork_session=True)
    )
    assert find_result_message(messages).session_id != session_id


async def test_continue_conversation_preserves_memory(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    await _seed_session(
        sdk, sdk_live_model, sdk_cwd, "Remember that the important number for this session is 73519. Reply OK."
    )
    messages = await collect_query_messages(
        sdk,
        "What is the important number? Reply with just the number.",
        make_sdk_options(sdk_live_model, sdk_cwd, continue_conversation=True),
    )
    assert "73519" in collect_assistant_text(messages)


async def test_seed_session_appears_in_list_sessions(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    seed_prompt = "Reply with LISTSEED."
    session_id = await _seed_session(sdk, sdk_live_model, sdk_cwd, seed_prompt)
    listed = sdk.list_sessions(directory=str(sdk_cwd))
    assert any(info.session_id == session_id for info in listed)


async def test_session_info_first_prompt_matches(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    seed_prompt = "Reply with FIRSTPROMPTSEED."
    session_id = await _seed_session(sdk, sdk_live_model, sdk_cwd, seed_prompt)
    info = sdk.get_session_info(session_id, directory=str(sdk_cwd))
    assert isinstance(info, SDKSessionInfo)
    assert info.first_prompt == seed_prompt


async def test_session_info_reports_cwd(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk, sdk_live_model, sdk_cwd, "Reply with CWDSEED.")
    info = sdk.get_session_info(session_id, directory=str(sdk_cwd))
    assert isinstance(info, SDKSessionInfo)
    assert info.cwd is not None
    assert Path(info.cwd).resolve() == sdk_cwd.resolve()


async def test_session_info_git_branch_field_type(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk, sdk_live_model, sdk_cwd, "Reply with BRANCHSEED.")
    info = sdk.get_session_info(session_id, directory=str(sdk_cwd))
    assert isinstance(info, SDKSessionInfo)
    assert info.git_branch is None or isinstance(info.git_branch, str)


async def test_session_info_last_modified_is_int(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk, sdk_live_model, sdk_cwd, "Reply with MODIFIEDSEED.")
    info = sdk.get_session_info(session_id, directory=str(sdk_cwd))
    assert isinstance(info, SDKSessionInfo)
    assert isinstance(info.last_modified, int)
    assert info.last_modified > 0


async def test_get_session_messages_returns_session_message_objects(
    sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path
) -> None:
    session_id = await _seed_session(sdk, sdk_live_model, sdk_cwd, "Reply with MSGSEED.")
    messages = sdk.get_session_messages(session_id, directory=str(sdk_cwd))
    assert len(messages) >= 1
    assert all(isinstance(m, SessionMessage) for m in messages)


async def test_session_message_fields(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk, sdk_live_model, sdk_cwd, "Reply with FIELDSEED.")
    messages = sdk.get_session_messages(session_id, directory=str(sdk_cwd))
    for message in messages:
        assert message.type in ("user", "assistant")
        assert isinstance(message.uuid, str) and message.uuid != ""
        assert message.session_id == session_id


async def test_session_message_carries_message_payload(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk, sdk_live_model, sdk_cwd, "Reply with PAYLOADSEED.")
    messages = sdk.get_session_messages(session_id, directory=str(sdk_cwd))
    # Each persisted message carries its underlying message payload.
    assert all(m.message is not None for m in messages)


async def test_rename_session_updates_custom_title(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk, sdk_live_model, sdk_cwd, "Reply with RENAMESEED.")
    sdk.rename_session(session_id, "My Renamed Session", directory=str(sdk_cwd))
    info = sdk.get_session_info(session_id, directory=str(sdk_cwd))
    assert isinstance(info, SDKSessionInfo)
    assert info.custom_title == "My Renamed Session"


async def test_rename_reflected_in_list_sessions(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk, sdk_live_model, sdk_cwd, "Reply with RENAMELISTSEED.")
    sdk.rename_session(session_id, "Listed Title", directory=str(sdk_cwd))
    listed = sdk.list_sessions(directory=str(sdk_cwd))
    match = next(info for info in listed if info.session_id == session_id)
    assert match.custom_title == "Listed Title"


async def test_tag_session_sets_then_clears_tag(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk, sdk_live_model, sdk_cwd, "Reply with TAGSEED.")
    sdk.tag_session(session_id, "important", directory=str(sdk_cwd))
    after_set = sdk.get_session_info(session_id, directory=str(sdk_cwd))
    assert isinstance(after_set, SDKSessionInfo)
    assert after_set.tag == "important"

    sdk.tag_session(session_id, None, directory=str(sdk_cwd))
    after_clear = sdk.get_session_info(session_id, directory=str(sdk_cwd))
    assert isinstance(after_clear, SDKSessionInfo)
    assert after_clear.tag is None


async def test_list_sessions_limit_caps_results(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    await _seed_session(sdk, sdk_live_model, sdk_cwd, "Reply with LIMITA.")
    await _seed_session(sdk, sdk_live_model, sdk_cwd, "Reply with LIMITB.")
    limited = sdk.list_sessions(directory=str(sdk_cwd), limit=1)
    assert len(limited) == 1


async def test_multiple_sessions_are_listed(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    first_id = await _seed_session(sdk, sdk_live_model, sdk_cwd, "Reply with MULTIA.")
    second_id = await _seed_session(sdk, sdk_live_model, sdk_cwd, "Reply with MULTIB.")
    listed_ids = {info.session_id for info in sdk.list_sessions(directory=str(sdk_cwd))}
    assert first_id in listed_ids
    assert second_id in listed_ids


async def test_two_fresh_queries_create_distinct_sessions(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    first_id = await _seed_session(sdk, sdk_live_model, sdk_cwd, "Reply with DISTINCTA.")
    options = ClaudeAgentOptions(model=sdk_live_model, cwd=str(sdk_cwd), setting_sources=[])
    second_messages = [m async for m in sdk.query(prompt="Reply with DISTINCTB.", options=options)]
    second_id = find_result_message(second_messages).session_id
    # Without resume/continue, each query gets its own session id.
    assert first_id != second_id
    # Sanity: the second turn really did produce assistant text.
    assert any(
        isinstance(m, AssistantMessage) and any(isinstance(b, TextBlock) for b in m.content) for m in second_messages
    )
