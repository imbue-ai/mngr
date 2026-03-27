"""Base class for structured event log records.

All event log data should use EventEnvelope as a base class to ensure
consistent envelope fields across all event sources. See the style guide
section "Event logging to disk" for conventions.
"""

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.primitives import PositiveInt


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
    """Unique identifier for an event (e.g. 'evt-a1b2c3d4e5f67890a1b2c3d4e5f67890')."""


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


class LogEvent(EventEnvelope):
    """A diagnostic log event using the standard event envelope.

    Used by both Python (loguru) and bash scripts to emit structured log
    lines to logs/<source>/events.jsonl files. The type field identifies
    the program (e.g. 'mngr', 'minds', 'event_watcher'), while source
    identifies the component that produced the log.
    """

    level: NonEmptyStr = Field(description="Log level (e.g. DEBUG, INFO, WARNING, ERROR)")
    message: str = Field(description="The log message text")
    pid: PositiveInt = Field(description="Process ID of the emitting process")
    # Omitted from serialization when None (matching the JSONL formatter behavior)
    command: str | None = Field(
        default=None,
        description="CLI subcommand that produced this log (e.g. 'create', 'list')",
    )

    def to_jsonl_dict(self) -> dict[str, object]:
        """Serialize to a dict suitable for JSONL output, omitting command when None."""
        result = self.model_dump()
        if result.get("command") is None:
            del result["command"]
        return result
