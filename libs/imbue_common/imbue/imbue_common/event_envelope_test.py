"""Unit tests for EventEnvelope base class and LogEvent."""

import json

import pytest
from pydantic import ValidationError

from imbue.imbue_common.event_envelope import EventEnvelope
from imbue.imbue_common.event_envelope import EventId
from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import EventType
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.imbue_common.event_envelope import LogEvent
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.primitives import PositiveInt

_TS = IsoTimestamp("2026-02-28T00:00:00.000000000Z")
_EID = EventId("evt-1234")
_SRC = EventSource("test_source")


def test_event_envelope_requires_all_fields() -> None:
    envelope = EventEnvelope(
        timestamp=_TS,
        type=EventType("test_event"),
        event_id=_EID,
        source=_SRC,
    )
    assert envelope.timestamp == _TS
    assert envelope.type == "test_event"
    assert envelope.event_id == _EID
    assert envelope.source == _SRC


def test_event_envelope_serializes_all_fields() -> None:
    envelope = EventEnvelope(
        timestamp=_TS,
        type=EventType("test_event"),
        event_id=_EID,
        source=_SRC,
    )
    data = json.loads(envelope.model_dump_json())
    assert "timestamp" in data
    assert "type" in data
    assert "event_id" in data
    assert "source" in data


def test_event_envelope_is_frozen() -> None:
    envelope = EventEnvelope(
        timestamp=_TS,
        type=EventType("test_event"),
        event_id=_EID,
        source=_SRC,
    )
    with pytest.raises(ValidationError):
        envelope.type = EventType("changed")


def test_iso_timestamp_rejects_empty() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        IsoTimestamp("")


def test_event_type_rejects_empty() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        EventType("")


def test_event_source_rejects_empty() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        EventSource("")


def test_event_id_rejects_empty() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        EventId("")


def test_log_event_includes_envelope_and_log_fields() -> None:
    event = LogEvent(
        timestamp=_TS,
        type=EventType("mng"),
        event_id=_EID,
        source=_SRC,
        level=NonEmptyStr("DEBUG"),
        message="Saving agent to repository",
        pid=PositiveInt(12345),
        command="create",
    )
    assert event.timestamp == _TS
    assert event.type == "mng"
    assert event.level == "DEBUG"
    assert event.message == "Saving agent to repository"
    assert event.pid == 12345
    assert event.command == "create"


def test_log_event_serializes_to_json_with_all_fields() -> None:
    event = LogEvent(
        timestamp=_TS,
        type=EventType("mng"),
        event_id=_EID,
        source=_SRC,
        level=NonEmptyStr("INFO"),
        message="Listed 3 agents",
        pid=PositiveInt(99999),
        command="list",
    )
    data = json.loads(event.model_dump_json())
    assert data["timestamp"] == str(_TS)
    assert data["type"] == "mng"
    assert data["event_id"] == str(_EID)
    assert data["source"] == str(_SRC)
    assert data["level"] == "INFO"
    assert data["message"] == "Listed 3 agents"
    assert data["pid"] == 99999
    assert data["command"] == "list"


def test_log_event_command_defaults_to_none() -> None:
    event = LogEvent(
        timestamp=_TS,
        type=EventType("event_watcher"),
        event_id=_EID,
        source=EventSource("event_watcher"),
        level=NonEmptyStr("DEBUG"),
        message="Watching for events",
        pid=PositiveInt(1000),
    )
    assert event.command is None


def test_log_event_to_jsonl_dict_omits_command_when_none() -> None:
    event = LogEvent(
        timestamp=_TS,
        type=EventType("event_watcher"),
        event_id=_EID,
        source=EventSource("event_watcher"),
        level=NonEmptyStr("DEBUG"),
        message="Watching for events",
        pid=PositiveInt(1000),
    )
    data = event.to_jsonl_dict()
    assert "command" not in data


def test_log_event_to_jsonl_dict_includes_command_when_set() -> None:
    event = LogEvent(
        timestamp=_TS,
        type=EventType("mng"),
        event_id=_EID,
        source=_SRC,
        level=NonEmptyStr("INFO"),
        message="test",
        pid=PositiveInt(1),
        command="create",
    )
    data = event.to_jsonl_dict()
    assert data["command"] == "create"


def test_log_event_is_frozen() -> None:
    event = LogEvent(
        timestamp=_TS,
        type=EventType("mng"),
        event_id=_EID,
        source=_SRC,
        level=NonEmptyStr("DEBUG"),
        message="test",
        pid=PositiveInt(1),
    )
    with pytest.raises(ValidationError):
        event.level = "INFO"  # type: ignore[misc]
