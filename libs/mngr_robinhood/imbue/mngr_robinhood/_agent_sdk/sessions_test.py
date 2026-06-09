from typing import Any

from claude_agent_sdk import SessionMessage

from imbue.mngr_robinhood._agent_sdk.sessions import _build_session_message
from imbue.mngr_robinhood._agent_sdk.sessions import _epoch_seconds
from imbue.mngr_robinhood._agent_sdk.sessions import _event_timestamps
from imbue.mngr_robinhood._agent_sdk.sessions import _first_user_prompt
from imbue.mngr_robinhood._agent_sdk.sessions import _parse_raw_events


def test_parse_raw_events_keeps_dicts_skips_malformed() -> None:
    content = '{"type": "user", "uuid": "u1"}\n\nnot json\n{"type": "assistant", "uuid": "a1"}\n123\n'
    events = _parse_raw_events(content)
    assert [e["uuid"] for e in events] == ["u1", "a1"]


def test_epoch_seconds_parses_iso_with_z() -> None:
    epoch = _epoch_seconds("2026-01-01T00:00:00Z")
    assert isinstance(epoch, int)
    assert epoch > 0


def test_epoch_seconds_none_and_invalid() -> None:
    assert _epoch_seconds(None) is None
    assert _epoch_seconds("not-a-timestamp") is None


def test_first_user_prompt_returns_first_non_meta_user_text() -> None:
    events: list[dict[str, Any]] = [
        {"type": "assistant", "message": {"content": []}},
        {"type": "user", "isMeta": True, "message": {"content": "stop hook"}},
        {"type": "user", "message": {"content": "the real prompt"}},
        {"type": "user", "message": {"content": "later prompt"}},
    ]
    assert _first_user_prompt(events) == "the real prompt"


def test_first_user_prompt_none_when_no_user_text() -> None:
    assert _first_user_prompt([{"type": "assistant", "message": {"content": []}}]) is None


def test_event_timestamps_collects_valid_epochs() -> None:
    events: list[dict[str, Any]] = [
        {"timestamp": "2026-01-01T00:00:00Z"},
        {"timestamp": "2026-01-01T00:00:05Z"},
        {"timestamp": 12345},
        {},
    ]
    stamps = _event_timestamps(events)
    assert len(stamps) == 2
    assert stamps[1] > stamps[0]


def test_build_session_message_for_user_event() -> None:
    raw = {"type": "user", "uuid": "u1", "message": {"role": "user", "content": "hi"}}
    message = _build_session_message(raw, "sess-1")
    assert isinstance(message, SessionMessage)
    assert message.type == "user"
    assert message.uuid == "u1"
    assert message.session_id == "sess-1"
    assert message.message == {"role": "user", "content": "hi"}
    assert message.parent_tool_use_id is None


def test_build_session_message_skips_meta_and_non_conversational() -> None:
    assert _build_session_message({"type": "user", "uuid": "u1", "isMeta": True}, "s") is None
    assert _build_session_message({"type": "summary", "uuid": "s1"}, "s") is None
    # An assistant event missing a uuid is skipped.
    assert _build_session_message({"type": "assistant"}, "s") is None
