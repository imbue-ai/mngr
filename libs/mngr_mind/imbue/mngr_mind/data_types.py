from __future__ import annotations

from typing import Any
from typing import Final

from pydantic import Field

from imbue.imbue_common.event_envelope import EventEnvelope
from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.primitives import PositiveInt
from imbue.mngr_recursive.watcher_common import DEFAULT_CEL_EXCLUDE_FILTERS
from imbue.mngr_recursive.watcher_common import DEFAULT_CEL_INCLUDE_FILTERS


class ConversationId(NonEmptyStr):
    """Unique identifier for a conversation thread (matches llm's conversation_id format)."""


class MessageRole(NonEmptyStr):
    """Role of a message sender (e.g. 'user', 'assistant')."""


# -- Event log sources --
# These constants define the source names and corresponding log paths.
# Event sources write to events/<SOURCE>/events.jsonl (proper EventEnvelope format).
# Log sources write to logs/<SOURCE>/events.jsonl (raw format, not EventEnvelope).

SOURCE_MESSAGES: Final[EventSource] = EventSource("messages")
SOURCE_SCHEDULED: Final[EventSource] = EventSource("scheduled")
SOURCE_MNGR_AGENTS: Final[EventSource] = EventSource("mngr/agents")
SOURCE_STOP: Final[EventSource] = EventSource("stop")
SOURCE_MONITOR: Final[EventSource] = EventSource("monitor")
SOURCE_DELIVERY_FAILURES: Final[EventSource] = EventSource("delivery_failures")
SOURCE_COMMON_TRANSCRIPT: Final[EventSource] = EventSource("claude/common_transcript")
SOURCE_MIND_IDLE: Final[EventSource] = EventSource("mind/idle")
SOURCE_MIND_SCHEDULE: Final[EventSource] = EventSource("mind/schedule")
SOURCE_MIND_ONBOARDING: Final[EventSource] = EventSource("mind/onboarding")


class MessageEvent(EventEnvelope):
    """An event in events/messages/events.jsonl recording a conversation message."""

    conversation_id: ConversationId
    role: MessageRole
    content: str


class MindEvent(EventEnvelope):
    """A generic event with a data payload, used for sources like scheduled,
    mngr/agents, stop, and monitor.
    """

    data: dict[str, Any] = {}


# -- Settings types --


class WatcherSettings(FrozenModel):
    """Settings for the [watchers] TOML section."""

    conversation_poll_interval_seconds: PositiveInt = Field(
        default=PositiveInt(5),
        description="Poll interval for the conversation watcher (seconds).",
    )
    event_poll_interval_seconds: PositiveInt = Field(
        default=PositiveInt(3),
        description="Poll interval for the event watcher (seconds).",
    )
    transcript_poll_interval_seconds: PositiveInt = Field(
        default=PositiveInt(5),
        description="Poll interval for the transcript watcher (seconds).",
    )
    event_cel_include_filters: tuple[str, ...] = Field(
        default=DEFAULT_CEL_INCLUDE_FILTERS,
        description="CEL include filter expressions passed to 'mngr events --include'. "
        "All include filters must match for an event to be included.",
    )
    event_cel_exclude_filters: tuple[str, ...] = Field(
        default=DEFAULT_CEL_EXCLUDE_FILTERS,
        description="CEL exclude filter expressions passed to 'mngr events --exclude'. "
        "Events matching any exclude filter are dropped.",
    )
    event_exclude_sources: tuple[str, ...] = Field(
        default=(),
        description="Event sources to unconditionally exclude from delivery. "
        "Events from these sources are dropped before the delivery loop processes them. "
        "Set during provisioning to prevent agents from observing their own output.",
    )
    event_burst_size: PositiveInt = Field(
        default=PositiveInt(5),
        description="Number of messages allowed in the initial burst before rate limiting kicks in.",
    )
    max_event_messages_per_minute: PositiveInt = Field(
        default=PositiveInt(10),
        description="Maximum event messages delivered to the agent per minute (sustained rate).",
    )
    max_delivery_retries: PositiveInt = Field(
        default=PositiveInt(3),
        description="Maximum consecutive delivery failures before notifying the user. "
        "Uses exponential backoff between retries.",
    )
    max_event_length: PositiveInt = Field(
        default=PositiveInt(50_000),
        description="Maximum length of a single event line in characters. "
        "If any event from a source exceeds this, all events from that source "
        "are aggregated into a single file reference.",
    )
    max_same_source_events_per_batch: PositiveInt = Field(
        default=PositiveInt(20),
        description="Maximum number of events from the same source in a single delivery batch. "
        "If exceeded, all events from that source are aggregated into a single file reference.",
    )
    idle_event_delay_minutes_schedule: tuple[int, ...] = Field(
        default=(),
        description="Schedule of delays (in minutes) between consecutive idle events. "
        "For example, [1, 10, 60] means: send the first idle event after 1 minute of "
        "inactivity, then after 11 minutes total, then every 60 minutes thereafter. "
        "An empty tuple disables idle events.",
    )
    scheduled_events: dict[str, str] = Field(
        default_factory=dict,
        description="Map of event names to time-of-day strings (e.g. '13:37:30', '15:00') "
        "in the user's timezone. Each event fires once per day at the specified time.",
    )
    user_timezone: str = Field(
        default="UTC",
        description="IANA timezone name (e.g. 'America/New_York', 'Europe/London'). "
        "Used for scheduled events and local time reporting in idle events.",
    )
    is_message_batching_enabled: bool = Field(
        default=True,
        description="When True, user messages from the 'messages' source are held "
        "until the corresponding assistant response arrives so the agent sees both "
        "together. When False, messages are delivered immediately as they arrive.",
    )
    # Any paths should be relative to the repo root, since the event watcher runs
    # in a tmux window whose working directory is the agent's work_dir (the repo root).
    event_batch_filter_command: str | None = Field(
        default=None,
        description="Optional command that filters event batches before delivery. "
        "The command receives JSONL lines on stdin (one event per line) and must output "
        "the same number of lines on stdout. To filter out an event, the script should "
        "output an empty line or '{}' for that position. If all events are filtered out, "
        "the batch is skipped entirely.",
    )
