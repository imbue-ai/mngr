"""Live verification of the documented session functions.

A real turn is run inside an isolated ``cwd`` to create a session on disk, which is then read
back and mutated via ``list_sessions`` / ``get_session_info`` / ``get_session_messages`` /
``rename_session`` / ``tag_session`` with ``directory=cwd``.
"""

from pathlib import Path
from types import ModuleType

import pytest
from claude_agent_sdk import ResultMessage
from claude_agent_sdk import SDKSessionInfo
from claude_agent_sdk import SessionMessage

from imbue.mngr_robinhood.testing import make_sdk_options

pytestmark = [pytest.mark.sdk_live, pytest.mark.tmux, pytest.mark.asyncio, pytest.mark.timeout(600)]


async def _seed_session(sdk: ModuleType, model: str, cwd: Path, seed_prompt: str) -> str:
    """Run one real turn in ``cwd`` and return the created session id."""
    options = make_sdk_options(model, cwd)
    session_id: str | None = None
    async for message in sdk.query(prompt=seed_prompt, options=options):
        if isinstance(message, ResultMessage):
            session_id = message.session_id
    assert session_id is not None and session_id != ""
    return session_id


async def test_session_functions_round_trip(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    seed_prompt = "Reply with exactly the word SESSIONSEEDTOKEN."
    session_id = await _seed_session(sdk, sdk_live_model, sdk_cwd, seed_prompt)

    # list_sessions must surface the just-created session for this directory.
    listed = sdk.list_sessions(directory=str(sdk_cwd))
    assert all(isinstance(info, SDKSessionInfo) for info in listed)
    matching = [info for info in listed if info.session_id == session_id]
    assert len(matching) == 1
    assert matching[0].first_prompt == seed_prompt

    # get_session_info must return the same session.
    info = sdk.get_session_info(session_id, directory=str(sdk_cwd))
    assert isinstance(info, SDKSessionInfo)
    assert info.session_id == session_id

    # get_session_messages must return the persisted transcript as SessionMessage objects.
    messages = sdk.get_session_messages(session_id, directory=str(sdk_cwd))
    assert len(messages) >= 1
    assert all(isinstance(m, SessionMessage) for m in messages)
    assert all(m.session_id == session_id for m in messages)
    assert all(m.type in ("user", "assistant") for m in messages)

    # rename_session and tag_session must persist and be visible on the next read.
    sdk.rename_session(session_id, "Renamed By Live Test", directory=str(sdk_cwd))
    sdk.tag_session(session_id, "live-test-tag", directory=str(sdk_cwd))
    updated = sdk.get_session_info(session_id, directory=str(sdk_cwd))
    assert isinstance(updated, SDKSessionInfo)
    assert updated.custom_title == "Renamed By Live Test"
    assert updated.tag == "live-test-tag"
