"""Unit tests for changeling data types."""

import json

import pytest

from imbue.imbue_common.event_envelope import EventId
from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import EventType
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.mng_claude_zygote.data_types import ChangelingEvent
from imbue.mng_claude_zygote.data_types import ChatModel
from imbue.mng_claude_zygote.data_types import ConversationEvent
from imbue.mng_claude_zygote.data_types import ConversationId
from imbue.mng_claude_zygote.data_types import MessageEvent
from imbue.mng_claude_zygote.data_types import MessageRole
from imbue.mng_claude_zygote.data_types import SOURCE_CONVERSATIONS
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


# -- ConversationEvent --


def test_conversation_event_tags_default_to_empty() -> None:
    event = ConversationEvent(
        timestamp=_TS,
        type=EventType("conversation_created"),
        event_id=_EID,
        source=SOURCE_CONVERSATIONS,
        conversation_id=ConversationId("conv-1"),
        model=ChatModel("claude-opus-4.6"),
    )
    assert event.tags == {}
    data = json.loads(event.model_dump_json())
    assert data["tags"] == {}


def test_conversation_event_with_tags() -> None:
    event = ConversationEvent(
        timestamp=_TS,
        type=EventType("conversation_created"),
        event_id=_EID,
        source=SOURCE_CONVERSATIONS,
        conversation_id=ConversationId("daily-2026-03-04"),
        model=ChatModel("claude-opus-4.6"),
        tags={"daily": "2026-03-04"},
    )
    assert event.tags == {"daily": "2026-03-04"}
    data = json.loads(event.model_dump_json())
    assert data["tags"]["daily"] == "2026-03-04"


def test_conversation_event_roundtrips_with_tags() -> None:
    raw = json.dumps(
        {
            "timestamp": "2026-03-04T00:00:00.000000000Z",
            "type": "conversation_created",
            "event_id": "evt-abc",
            "source": "conversations",
            "conversation_id": "conv-1",
            "model": "claude-opus-4.6",
            "tags": {"daily": "2026-03-04"},
        }
    )
    event = ConversationEvent.model_validate_json(raw)
    assert event.tags == {"daily": "2026-03-04"}


def test_conversation_event_roundtrips_without_tags() -> None:
    raw = json.dumps(
        {
            "timestamp": "2026-03-04T00:00:00.000000000Z",
            "type": "conversation_created",
            "event_id": "evt-abc",
            "source": "conversations",
            "conversation_id": "conv-1",
            "model": "claude-opus-4.6",
        }
    )
    event = ConversationEvent.model_validate_json(raw)
    assert event.tags == {}


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
