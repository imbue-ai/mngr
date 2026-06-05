"""Focused, thorough live coverage of the documented session functions.

Targets behavior not covered by ``test_sdk_sessions.py`` / ``test_sdk_sessions_advanced.py``:
paging (``limit`` / ``offset``) on real sessions, ``list_sessions`` ordering, ``SDKSessionInfo``
field-value contracts, directory isolation, and rename/tag overwrite semantics.

Covered functions: ``list_sessions``, ``get_session_messages``, ``get_session_info``,
``rename_session``, ``tag_session``.
"""

from pathlib import Path

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk import ResultMessage
from claude_agent_sdk import SDKSessionInfo
from claude_agent_sdk import SessionMessage
from claude_agent_sdk import get_session_info
from claude_agent_sdk import get_session_messages
from claude_agent_sdk import list_sessions
from claude_agent_sdk import query
from claude_agent_sdk import rename_session
from claude_agent_sdk import tag_session

pytestmark = [pytest.mark.sdk_live, pytest.mark.asyncio, pytest.mark.timeout(600)]


async def _seed_session(model: str, cwd: Path, prompt: str, resume: str | None = None) -> str:
    """Run one real turn (optionally resuming) and return the session id."""
    options = ClaudeAgentOptions(model=model, cwd=str(cwd), setting_sources=[], resume=resume)
    session_id: str | None = None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            session_id = message.session_id
    assert session_id is not None and session_id != ""
    return session_id


async def _seed_two_turn_session(model: str, cwd: Path) -> str:
    """Create a session with two turns so it has several persisted messages."""
    session_id = await _seed_session(model, cwd, "Reply with TURNONE.")
    return await _seed_session(model, cwd, "Reply with TURNTWO.", resume=session_id)


# --- list_sessions -------------------------------------------------------------------------------


async def test_list_sessions_orders_most_recent_first(sdk_live_model: str, sdk_cwd: Path) -> None:
    older = await _seed_session(sdk_live_model, sdk_cwd, "Reply OLDER.")
    newer = await _seed_session(sdk_live_model, sdk_cwd, "Reply NEWER.")
    listed = list_sessions(directory=str(sdk_cwd))
    assert len(listed) == 2
    # Most recently modified session comes first.
    assert listed[0].session_id == newer
    assert listed[1].session_id == older


async def test_list_sessions_offset_skips_from_the_front(sdk_live_model: str, sdk_cwd: Path) -> None:
    older = await _seed_session(sdk_live_model, sdk_cwd, "Reply OLDER.")
    newer = await _seed_session(sdk_live_model, sdk_cwd, "Reply NEWER.")
    offset_listed = list_sessions(directory=str(sdk_cwd), offset=1)
    offset_ids = [info.session_id for info in offset_listed]
    assert newer not in offset_ids
    assert older in offset_ids


async def test_list_sessions_limit_larger_than_count_returns_all(sdk_live_model: str, sdk_cwd: Path) -> None:
    first = await _seed_session(sdk_live_model, sdk_cwd, "Reply ONE.")
    second = await _seed_session(sdk_live_model, sdk_cwd, "Reply TWO.")
    listed_ids = {info.session_id for info in list_sessions(directory=str(sdk_cwd), limit=100)}
    assert {first, second} <= listed_ids


async def test_list_sessions_limit_and_offset_select_middle(sdk_live_model: str, sdk_cwd: Path) -> None:
    _first = await _seed_session(sdk_live_model, sdk_cwd, "Reply A.")
    middle = await _seed_session(sdk_live_model, sdk_cwd, "Reply B.")
    _last = await _seed_session(sdk_live_model, sdk_cwd, "Reply C.")
    # Order is newest-first [C, B, A]; offset=1 limit=1 selects B.
    page = list_sessions(directory=str(sdk_cwd), limit=1, offset=1)
    assert len(page) == 1
    assert page[0].session_id == middle


async def test_list_sessions_returns_session_info_objects(sdk_live_model: str, sdk_cwd: Path) -> None:
    await _seed_session(sdk_live_model, sdk_cwd, "Reply INFOOBJ.")
    listed = list_sessions(directory=str(sdk_cwd))
    assert len(listed) >= 1
    assert all(isinstance(info, SDKSessionInfo) for info in listed)


# --- get_session_messages ------------------------------------------------------------------------


async def test_get_session_messages_limit_caps_count(sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_two_turn_session(sdk_live_model, sdk_cwd)
    total = len(get_session_messages(session_id, directory=str(sdk_cwd)))
    assert total >= 3
    limited = get_session_messages(session_id, directory=str(sdk_cwd), limit=2)
    assert len(limited) == 2


async def test_get_session_messages_offset_skips_messages(sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_two_turn_session(sdk_live_model, sdk_cwd)
    total = len(get_session_messages(session_id, directory=str(sdk_cwd)))
    skipped = get_session_messages(session_id, directory=str(sdk_cwd), offset=1)
    assert len(skipped) == total - 1


async def test_get_session_messages_offset_at_total_is_empty(sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_two_turn_session(sdk_live_model, sdk_cwd)
    total = len(get_session_messages(session_id, directory=str(sdk_cwd)))
    assert get_session_messages(session_id, directory=str(sdk_cwd), offset=total) == []


async def test_get_session_messages_count_grows_with_turns(sdk_live_model: str, sdk_cwd: Path) -> None:
    single_turn_dir = sdk_cwd / "single"
    single_turn_dir.mkdir()
    single_id = await _seed_session(sdk_live_model, single_turn_dir, "Reply SINGLE.")
    single_count = len(get_session_messages(single_id, directory=str(single_turn_dir)))

    two_turn_dir = sdk_cwd / "double"
    two_turn_dir.mkdir()
    two_id = await _seed_two_turn_session(sdk_live_model, two_turn_dir)
    two_count = len(get_session_messages(two_id, directory=str(two_turn_dir)))

    assert two_count > single_count


async def test_get_session_messages_first_message_is_user(sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk_live_model, sdk_cwd, "Reply FIRSTUSER.")
    messages = get_session_messages(session_id, directory=str(sdk_cwd))
    assert len(messages) >= 1
    # The transcript starts with the user's prompt.
    assert messages[0].type == "user"


async def test_get_session_messages_user_payload_contains_prompt(sdk_live_model: str, sdk_cwd: Path) -> None:
    seed_prompt = "Reply with PAYLOADPROMPTTOKEN."
    session_id = await _seed_session(sdk_live_model, sdk_cwd, seed_prompt)
    messages = get_session_messages(session_id, directory=str(sdk_cwd))
    user_messages = [m for m in messages if m.type == "user"]
    assert any("PAYLOADPROMPTTOKEN" in str(m.message) for m in user_messages)


async def test_get_session_messages_have_parent_tool_use_id_field(sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk_live_model, sdk_cwd, "Reply PARENTFIELD.")
    messages = get_session_messages(session_id, directory=str(sdk_cwd))
    assert all(isinstance(m, SessionMessage) for m in messages)
    # Top-level messages carry the documented parent_tool_use_id field, which is None here.
    assert all(m.parent_tool_use_id is None for m in messages)


async def test_get_session_messages_isolated_by_directory(sdk_live_model: str, sdk_cwd: Path, tmp_path: Path) -> None:
    session_id = await _seed_session(sdk_live_model, sdk_cwd, "Reply MSGISO.")
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    assert get_session_messages(session_id, directory=str(other_dir)) == []


# --- get_session_info ----------------------------------------------------------------------------


async def test_get_session_info_is_isolated_by_directory(sdk_live_model: str, sdk_cwd: Path, tmp_path: Path) -> None:
    session_id = await _seed_session(sdk_live_model, sdk_cwd, "Reply INFOISO.")
    other_dir = tmp_path / "other_info"
    other_dir.mkdir()
    assert get_session_info(session_id, directory=str(other_dir)) is None


async def test_get_session_info_summary_is_nonempty_string(sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk_live_model, sdk_cwd, "Reply SUMMARYSEED.")
    info = get_session_info(session_id, directory=str(sdk_cwd))
    assert isinstance(info, SDKSessionInfo)
    assert isinstance(info.summary, str)
    assert info.summary != ""


async def test_get_session_info_tag_is_none_initially(sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk_live_model, sdk_cwd, "Reply TAGNONE.")
    info = get_session_info(session_id, directory=str(sdk_cwd))
    assert isinstance(info, SDKSessionInfo)
    assert info.tag is None


async def test_get_session_info_custom_title_is_str_or_none(sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk_live_model, sdk_cwd, "Reply TITLETYPE.")
    info = get_session_info(session_id, directory=str(sdk_cwd))
    assert isinstance(info, SDKSessionInfo)
    assert info.custom_title is None or isinstance(info.custom_title, str)


async def test_get_session_info_file_size_is_positive(sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk_live_model, sdk_cwd, "Reply FILESIZE.")
    info = get_session_info(session_id, directory=str(sdk_cwd))
    assert isinstance(info, SDKSessionInfo)
    assert info.file_size is None or info.file_size > 0


async def test_get_session_info_created_at_not_after_last_modified(sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk_live_model, sdk_cwd, "Reply CREATEDAT.")
    info = get_session_info(session_id, directory=str(sdk_cwd))
    assert isinstance(info, SDKSessionInfo)
    assert isinstance(info.last_modified, int)
    if info.created_at is not None:
        assert info.created_at <= info.last_modified


async def test_get_session_info_session_id_matches(sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk_live_model, sdk_cwd, "Reply IDMATCH.")
    info = get_session_info(session_id, directory=str(sdk_cwd))
    assert isinstance(info, SDKSessionInfo)
    assert info.session_id == session_id


# --- rename_session ------------------------------------------------------------------------------


async def test_rename_session_overwrites_previous_title(sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk_live_model, sdk_cwd, "Reply RENAMETWICE.")
    rename_session(session_id, "First Title", directory=str(sdk_cwd))
    rename_session(session_id, "Second Title", directory=str(sdk_cwd))
    info = get_session_info(session_id, directory=str(sdk_cwd))
    assert isinstance(info, SDKSessionInfo)
    assert info.custom_title == "Second Title"


async def test_rename_session_accepts_unicode_title(sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk_live_model, sdk_cwd, "Reply UNICODETITLE.")
    unicode_title = "Café 日本語 🎉 Session"
    rename_session(session_id, unicode_title, directory=str(sdk_cwd))
    info = get_session_info(session_id, directory=str(sdk_cwd))
    assert isinstance(info, SDKSessionInfo)
    assert info.custom_title == unicode_title


async def test_rename_session_preserves_messages(sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk_live_model, sdk_cwd, "Reply RENAMEKEEPSMSGS.")
    before = len(get_session_messages(session_id, directory=str(sdk_cwd)))
    rename_session(session_id, "Renamed", directory=str(sdk_cwd))
    after = len(get_session_messages(session_id, directory=str(sdk_cwd)))
    assert before == after


# --- tag_session ---------------------------------------------------------------------------------


async def test_tag_session_reflected_in_list_sessions(sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk_live_model, sdk_cwd, "Reply TAGINLIST.")
    tag_session(session_id, "release-candidate", directory=str(sdk_cwd))
    listed = list_sessions(directory=str(sdk_cwd))
    match = next(info for info in listed if info.session_id == session_id)
    assert match.tag == "release-candidate"


async def test_tag_session_overwrites_previous_tag(sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk_live_model, sdk_cwd, "Reply TAGOVERWRITE.")
    tag_session(session_id, "first-tag", directory=str(sdk_cwd))
    tag_session(session_id, "second-tag", directory=str(sdk_cwd))
    info = get_session_info(session_id, directory=str(sdk_cwd))
    assert isinstance(info, SDKSessionInfo)
    assert info.tag == "second-tag"


async def test_tag_session_does_not_affect_other_sessions(sdk_live_model: str, sdk_cwd: Path) -> None:
    tagged = await _seed_session(sdk_live_model, sdk_cwd, "Reply TAGGEDONE.")
    untagged = await _seed_session(sdk_live_model, sdk_cwd, "Reply UNTAGGEDONE.")
    tag_session(tagged, "only-this-one", directory=str(sdk_cwd))
    untagged_info = get_session_info(untagged, directory=str(sdk_cwd))
    assert isinstance(untagged_info, SDKSessionInfo)
    assert untagged_info.tag is None


async def test_tag_then_clear_restores_none(sdk_live_model: str, sdk_cwd: Path) -> None:
    session_id = await _seed_session(sdk_live_model, sdk_cwd, "Reply TAGCLEARCYCLE.")
    tag_session(session_id, "temporary", directory=str(sdk_cwd))
    tag_session(session_id, None, directory=str(sdk_cwd))
    info = get_session_info(session_id, directory=str(sdk_cwd))
    assert isinstance(info, SDKSessionInfo)
    assert info.tag is None


# --- cross-function round trip with paging -------------------------------------------------------


async def test_list_then_read_back_each_listed_session(sdk_live_model: str, sdk_cwd: Path) -> None:
    first = await _seed_session(sdk_live_model, sdk_cwd, "Reply ROUNDA.")
    second = await _seed_session(sdk_live_model, sdk_cwd, "Reply ROUNDB.")
    listed = list_sessions(directory=str(sdk_cwd))
    listed_ids = {info.session_id for info in listed}
    assert {first, second} <= listed_ids
    # Every listed session can be independently read back by id.
    for info in listed:
        read_back = get_session_info(info.session_id, directory=str(sdk_cwd))
        assert isinstance(read_back, SDKSessionInfo)
        assert read_back.session_id == info.session_id
        assert len(get_session_messages(info.session_id, directory=str(sdk_cwd))) >= 1
