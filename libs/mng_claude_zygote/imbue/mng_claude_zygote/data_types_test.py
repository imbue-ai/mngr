"""Unit tests for changeling data types."""

import json

import pytest

from imbue.imbue_common.event_envelope import EventId
from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import EventType
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.mng_claude_zygote.data_types import ChangelingEvent
from imbue.mng_claude_zygote.data_types import ConversationId
from imbue.mng_claude_zygote.data_types import MessageEvent
from imbue.mng_claude_zygote.data_types import MessageRole
from imbue.mng_claude_zygote.data_types import SOURCE_MESSAGES

_TS = IsoTimestamp("2026-02-28T00:00:00.000000000Z")
_EID = EventId("evt-1234")


# -- Primitive types --


def test_conversation_id_rejects_empty() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        ConversationId("")


# -- MessageEvent --


def test_message_event_is_self_describing() -> None:
    event = MessageEvent(
        timestamp=_TS,
        type=EventType("message"),
        event_id=_EID,
        source=SOURCE_MESSAGES,
        conversation_id=ConversationId("conv-1"),
        role=MessageRole("user"),
        content="Hello",
    )
    data = json.loads(event.model_dump_json())
    assert data["conversation_id"] == "conv-1"
    assert data["role"] == "user"
    assert data["source"] == "messages"


# -- ChangelingEvent --


def test_changeling_event_with_data() -> None:
    event = ChangelingEvent(
        timestamp=_TS,
        type=EventType("sub_agent_waiting"),
        event_id=_EID,
        source=EventSource("mng_agents"),
        data={"agent_name": "helper-1"},
    )
    assert event.data["agent_name"] == "helper-1"
