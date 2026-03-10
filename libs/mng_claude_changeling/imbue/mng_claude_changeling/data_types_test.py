import json

import pytest

from imbue.imbue_common.event_envelope import EventId
from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import EventType
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.mng_claude_changeling.data_types import AgentStateTransitionEvent
from imbue.mng_claude_changeling.data_types import ChangelingEvent
from imbue.mng_claude_changeling.data_types import ConversationId
from imbue.mng_claude_changeling.data_types import MessageEvent
from imbue.mng_claude_changeling.data_types import MessageRole
from imbue.mng_claude_changeling.data_types import SOURCE_MESSAGES
from imbue.mng_claude_changeling.data_types import SOURCE_MNG_AGENTS

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


# -- AgentStateTransitionEvent --


def test_agent_state_transition_event_serialization() -> None:
    event = AgentStateTransitionEvent(
        timestamp=_TS,
        type=EventType("agent_state_transition"),
        event_id=_EID,
        source=SOURCE_MNG_AGENTS,
        agent_id="agent-abc123",
        agent_name="my-helper",
        from_state="RUNNING",
        to_state="WAITING",
    )
    data = json.loads(event.model_dump_json())
    assert data["type"] == "agent_state_transition"
    assert data["source"] == "mng/agents"
    assert data["agent_id"] == "agent-abc123"
    assert data["agent_name"] == "my-helper"
    assert data["from_state"] == "RUNNING"
    assert data["to_state"] == "WAITING"


def test_agent_state_transition_event_roundtrips_from_json() -> None:
    raw = json.dumps(
        {
            "timestamp": "2026-03-04T00:00:00.000000000Z",
            "type": "agent_state_transition",
            "event_id": "evt-xyz",
            "source": "mng/agents",
            "agent_id": "agent-456",
            "agent_name": "worker",
            "from_state": "WAITING",
            "to_state": "RUNNING",
        }
    )
    event = AgentStateTransitionEvent.model_validate_json(raw)
    assert event.from_state == "WAITING"
    assert event.to_state == "RUNNING"
    assert event.agent_id == "agent-456"


# -- ChangelingEvent --


def test_changeling_event_with_data() -> None:
    event = ChangelingEvent(
        timestamp=_TS,
        type=EventType("sub_agent_waiting"),
        event_id=_EID,
        source=EventSource("mng/agents"),
        data={"agent_name": "helper-1"},
    )
    assert event.data["agent_name"] == "helper-1"
