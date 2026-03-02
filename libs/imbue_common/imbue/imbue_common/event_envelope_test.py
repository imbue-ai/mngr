"""Unit tests for EventEnvelope base class."""

import json

import pytest

from imbue.imbue_common.event_envelope import EventEnvelope
from imbue.imbue_common.event_envelope import EventId
from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import EventType
from imbue.imbue_common.event_envelope import IsoTimestamp

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
    with pytest.raises(Exception):
        envelope.type = EventType("changed")  # type: ignore[misc]


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
