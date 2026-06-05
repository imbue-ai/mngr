"""Live verification of advanced documented session behavior.

Covers session continuation (``resume`` / ``fork_session`` / ``continue_conversation``), the
``SDKSessionInfo`` / ``SessionMessage`` field contracts, the ``rename`` / ``tag`` mutators, and
``list_sessions`` paging.
"""

from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk import SDKSessionInfo
from claude_agent_sdk import SessionMessage
from claude_agent_sdk import TextBlock
from claude_agent_sdk import get_session_info
from claude_agent_sdk import get_session_messages
from claude_agent_sdk import list_sessions
from claude_agent_sdk import query
from claude_agent_sdk import rename_session
from claude_agent_sdk import tag_session

from imbue.mngr_robinhood.testing import collect_assistant_text
from imbue.mngr_robinhood.testing import collect_query_messages
from imbue.mngr_robinhood.testing import find_result_message
from imbue.mngr_robinhood.testing import make_sdk_options

pytestmark = [pytest.mark.sdk_live, pytest.mark.asyncio, pytest.mark.timeout(600)]


async def _seed_session(model: str, cwd: Path, prompt: str) -> str:
    messages = await collect_query_messages(prompt, make_sdk_options(model, cwd))
    return find_result_message(messages).session_id


async def test_resume_continues_same_session_id(sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk_live_model, sdk_cwd, "Reply with OK.")
    messages = await collect_query_messages(
        "Reply with OK again.", make_sdk_options(sdk_live_model, sdk_cwd, resume=session_id)
    )
    assert find_result_message(messages).session_id == session_id


async def test_resume_preserves_conversation_memory(sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk_live_model, sdk_cwd, "Remember that the secret word is FALCONXYZ. Reply OK.")
    messages = await collect_query_messages(
        "What is the secret word? Reply with just the word.",
        make_sdk_options(sdk_live_model, sdk_cwd, resume=session_id),
    )
    assert "FALCONXYZ" in collect_assistant_text(messages).upper()


async def test_fork_session_creates_new_session_id(sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk_live_model, sdk_cwd, "Reply with OK.")
    messages = await collect_query_messages(
        "Reply with OK.", make_sdk_options(sdk_live_model, sdk_cwd, resume=session_id, fork_session=True)
    )
    assert find_result_message(messages).session_id != session_id


async def test_continue_conversation_preserves_memory(sdk_live_model: str, sdk_cwd: Path) -> None:
    await _seed_session(sdk_live_model, sdk_cwd, "Remember that the secret number is 73519. Reply OK.")
    messages = await collect_query_messages(
        "What is the secret number? Reply with just the number.",
        make_sdk_options(sdk_live_model, sdk_cwd, continue_conversation=True),
    )
    assert "73519" in collect_assistant_text(messages)


async def test_seed_session_appears_in_list_sessions(sdk_live_model: str, sdk_cwd: Path) -> None:
    seed_prompt = "Reply with LISTSEED."
    session_id = await _seed_session(sdk_live_model, sdk_cwd, seed_prompt)
    listed = list_sessions(directory=str(sdk_cwd))
    assert any(info.session_id == session_id for info in listed)


async def test_session_info_first_prompt_matches(sdk_live_model: str, sdk_cwd: Path) -> None:
    seed_prompt = "Reply with FIRSTPROMPTSEED."
    session_id = await _seed_session(sdk_live_model, sdk_cwd, seed_prompt)
    info = get_session_info(session_id, directory=str(sdk_cwd))
    assert isinstance(info, SDKSessionInfo)
    assert info.first_prompt == seed_prompt


async def test_session_info_reports_cwd(sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk_live_model, sdk_cwd, "Reply with CWDSEED.")
    info = get_session_info(session_id, directory=str(sdk_cwd))
    assert isinstance(info, SDKSessionInfo)
    assert info.cwd is not None
    assert Path(info.cwd).resolve() == sdk_cwd.resolve()


async def test_session_info_git_branch_field_type(sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk_live_model, sdk_cwd, "Reply with BRANCHSEED.")
    info = get_session_info(session_id, directory=str(sdk_cwd))
    assert isinstance(info, SDKSessionInfo)
    assert info.git_branch is None or isinstance(info.git_branch, str)


async def test_session_info_last_modified_is_int(sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk_live_model, sdk_cwd, "Reply with MODIFIEDSEED.")
    info = get_session_info(session_id, directory=str(sdk_cwd))
    assert isinstance(info, SDKSessionInfo)
    assert isinstance(info.last_modified, int)
    assert info.last_modified > 0


async def test_get_session_messages_returns_session_message_objects(sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk_live_model, sdk_cwd, "Reply with MSGSEED.")
    messages = get_session_messages(session_id, directory=str(sdk_cwd))
    assert len(messages) >= 1
    assert all(isinstance(m, SessionMessage) for m in messages)


async def test_session_message_fields(sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk_live_model, sdk_cwd, "Reply with FIELDSEED.")
    messages = get_session_messages(session_id, directory=str(sdk_cwd))
    for message in messages:
        assert message.type in ("user", "assistant")
        assert isinstance(message.uuid, str) and message.uuid != ""
        assert message.session_id == session_id


async def test_session_message_carries_message_payload(sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk_live_model, sdk_cwd, "Reply with PAYLOADSEED.")
    messages = get_session_messages(session_id, directory=str(sdk_cwd))
    # Each persisted message carries its underlying message payload.
    assert all(m.message is not None for m in messages)


async def test_rename_session_updates_custom_title(sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk_live_model, sdk_cwd, "Reply with RENAMESEED.")
    rename_session(session_id, "My Renamed Session", directory=str(sdk_cwd))
    info = get_session_info(session_id, directory=str(sdk_cwd))
    assert isinstance(info, SDKSessionInfo)
    assert info.custom_title == "My Renamed Session"


async def test_rename_reflected_in_list_sessions(sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk_live_model, sdk_cwd, "Reply with RENAMELISTSEED.")
    rename_session(session_id, "Listed Title", directory=str(sdk_cwd))
    listed = list_sessions(directory=str(sdk_cwd))
    match = next(info for info in listed if info.session_id == session_id)
    assert match.custom_title == "Listed Title"


async def test_tag_session_sets_then_clears_tag(sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk_live_model, sdk_cwd, "Reply with TAGSEED.")
    tag_session(session_id, "important", directory=str(sdk_cwd))
    after_set = get_session_info(session_id, directory=str(sdk_cwd))
    assert isinstance(after_set, SDKSessionInfo)
    assert after_set.tag == "important"

    tag_session(session_id, None, directory=str(sdk_cwd))
    after_clear = get_session_info(session_id, directory=str(sdk_cwd))
    assert isinstance(after_clear, SDKSessionInfo)
    assert after_clear.tag is None


async def test_list_sessions_limit_caps_results(sdk_live_model: str, sdk_cwd: Path) -> None:
    await _seed_session(sdk_live_model, sdk_cwd, "Reply with LIMITA.")
    await _seed_session(sdk_live_model, sdk_cwd, "Reply with LIMITB.")
    limited = list_sessions(directory=str(sdk_cwd), limit=1)
    assert len(limited) == 1


async def test_multiple_sessions_are_listed(sdk_live_model: str, sdk_cwd: Path) -> None:
    first_id = await _seed_session(sdk_live_model, sdk_cwd, "Reply with MULTIA.")
    second_id = await _seed_session(sdk_live_model, sdk_cwd, "Reply with MULTIB.")
    listed_ids = {info.session_id for info in list_sessions(directory=str(sdk_cwd))}
    assert first_id in listed_ids
    assert second_id in listed_ids


async def test_two_fresh_queries_create_distinct_sessions(sdk_live_model: str, sdk_cwd: Path) -> None:
    first_id = await _seed_session(sdk_live_model, sdk_cwd, "Reply with DISTINCTA.")
    options = ClaudeAgentOptions(model=sdk_live_model, cwd=str(sdk_cwd), setting_sources=[])
    second_messages = [m async for m in query(prompt="Reply with DISTINCTB.", options=options)]
    second_id = find_result_message(second_messages).session_id
    # Without resume/continue, each query gets its own session id.
    assert first_id != second_id
    # Sanity: the second turn really did produce assistant text.
    assert any(
        isinstance(m, AssistantMessage) and any(isinstance(b, TextBlock) for b in m.content) for m in second_messages
    )
