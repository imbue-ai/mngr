"""Base class for structured event log records.

All event log data should use EventEnvelope as a base class to ensure
consistent envelope fields across all event sources. See the style guide
section "Event logging to disk" for conventions.
"""

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr


class IsoTimestamp(NonEmptyStr):
    """An ISO 8601 formatted timestamp string with nanosecond precision.

    Example: '2026-02-28T00:00:00.123456789Z'
    """


class EventType(NonEmptyStr):
    """Type of an event (e.g. 'conversation_created', 'message', 'scheduled')."""


class EventSource(NonEmptyStr):
    """Source identifier for an event, matching the log folder name.

    Must match the folder under logs/ where the event is stored.
    Examples: 'conversations', 'messages', 'scheduled'
    """


class EventId(NonEmptyStr):
    """Unique identifier for an event (typically timestamp + random hex)."""


class EventEnvelope(FrozenModel):
    """Base class for all structured event log records.

    Every event written to a logs/<source>/events.jsonl file must include
    these envelope fields. Subclasses add domain-specific fields.

    The envelope ensures that every event line is self-describing: you never
    need to know the filename to understand the event.
    """

    timestamp: IsoTimestamp
    type: EventType
    event_id: EventId
    source: EventSource
