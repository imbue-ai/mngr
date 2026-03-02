from __future__ import annotations

from typing import Any
from typing import Final

from pydantic import Field

from imbue.imbue_common.event_envelope import EventEnvelope
from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.primitives import PositiveFloat
from imbue.imbue_common.primitives import PositiveInt


class ConversationId(NonEmptyStr):
    """Unique identifier for a conversation thread (matches llm's conversation_id format)."""


class ChatModel(NonEmptyStr):
    """Model name used for chat conversations (e.g. 'claude-sonnet-4-6')."""


class MessageRole(NonEmptyStr):
    """Role of a message sender (e.g. 'user', 'assistant')."""


# -- Event log sources --
# These constants define the source names and corresponding log paths.
# Each source writes to logs/<SOURCE>/events.jsonl.

SOURCE_CONVERSATIONS: Final[EventSource] = EventSource("conversations")
SOURCE_MESSAGES: Final[EventSource] = EventSource("messages")
SOURCE_SCHEDULED: Final[EventSource] = EventSource("scheduled")
SOURCE_MNG_AGENTS: Final[EventSource] = EventSource("mng_agents")
SOURCE_STOP: Final[EventSource] = EventSource("stop")
SOURCE_MONITOR: Final[EventSource] = EventSource("monitor")
SOURCE_CLAUDE_TRANSCRIPT: Final[EventSource] = EventSource("claude_transcript")


class ConversationEvent(EventEnvelope):
    """An event in logs/conversations/events.jsonl tracking conversation lifecycle.

    Emitted when a conversation is created or its model is changed.
    """

    conversation_id: ConversationId
    model: ChatModel


class MessageEvent(EventEnvelope):
    """An event in logs/messages/events.jsonl recording a conversation message.

    Each event represents a single user or assistant message. All messages
    across all conversations go into the same file, with conversation_id
    identifying which conversation the message belongs to.
    """

    conversation_id: ConversationId
    role: MessageRole
    content: str


class ChangelingEvent(EventEnvelope):
    """A generic event with a data payload, used for sources like scheduled,
    mng_agents, stop, and monitor.

    The data field carries event-type-specific payload.
    """

    data: dict[str, Any] = {}


# -- Settings types --
# These model the structure of .changelings/settings.toml.
# Each section corresponds to a TOML table.


class ContextSettings(FrozenModel):
    """Settings for the [chat.context] TOML section (used by context_tool.py)."""

    max_transcript_line_count: PositiveInt = Field(
        default=PositiveInt(10),
        description="Maximum number of inner monologue lines to include in context.",
    )
    max_messages_line_count: PositiveInt = Field(
        default=PositiveInt(20),
        description="Maximum number of recent message lines to include in context.",
    )
    max_messages_per_conversation: PositiveInt = Field(
        default=PositiveInt(3),
        description="Maximum messages to show per conversation in context.",
    )
    max_trigger_line_count: PositiveInt = Field(
        default=PositiveInt(5),
        description="Maximum trigger event lines per source in context.",
    )
    max_content_length: PositiveInt = Field(
        default=PositiveInt(200),
        description="Maximum character length for truncated event content in context_tool.",
    )


class ExtraContextSettings(FrozenModel):
    """Settings for the [chat.extra_context] TOML section (used by extra_context_tool.py)."""

    max_content_length: PositiveInt = Field(
        default=PositiveInt(300),
        description="Maximum character length for truncated event content in extra_context_tool.",
    )
    transcript_line_count: PositiveInt = Field(
        default=PositiveInt(50),
        description="Number of inner monologue lines to show in extended history.",
    )
    mng_list_hard_timeout_seconds: PositiveFloat = Field(
        default=PositiveFloat(120.0),
        description="Hard timeout for mng list command (seconds).",
    )
    mng_list_warn_threshold_seconds: PositiveFloat = Field(
        default=PositiveFloat(15.0),
        description="Warning threshold for mng list command (seconds).",
    )


class ChatSettings(FrozenModel):
    """Settings for the [chat] TOML section."""

    model: ChatModel | None = Field(
        default=None,
        description="Default model for new conversation threads. "
        "When None, chat.sh falls back to the hardcoded default (claude-opus-4-6).",
    )
    context: ContextSettings = Field(
        default_factory=ContextSettings,
        description="Context tool settings ([chat.context] section).",
    )
    extra_context: ExtraContextSettings = Field(
        default_factory=ExtraContextSettings,
        description="Extra context tool settings ([chat.extra_context] section).",
    )


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
    watched_event_sources: tuple[str, ...] = Field(
        default=("messages", "scheduled", "mng_agents", "stop"),
        description="Event sources monitored by the event watcher.",
    )


class ProvisioningSettings(FrozenModel):
    """Settings for the [provisioning] TOML section."""

    fs_hard_timeout_seconds: PositiveFloat = Field(
        default=PositiveFloat(16.0),
        description="Hard timeout for filesystem operations (seconds).",
    )
    fs_warn_threshold_seconds: PositiveFloat = Field(
        default=PositiveFloat(4.0),
        description="Warning threshold for filesystem operations (seconds).",
    )
    command_check_hard_timeout_seconds: PositiveFloat = Field(
        default=PositiveFloat(30.0),
        description="Hard timeout for command existence checks (seconds).",
    )
    command_check_warn_threshold_seconds: PositiveFloat = Field(
        default=PositiveFloat(5.0),
        description="Warning threshold for command existence checks (seconds).",
    )
    install_hard_timeout_seconds: PositiveFloat = Field(
        default=PositiveFloat(300.0),
        description="Hard timeout for package installations (seconds).",
    )
    install_warn_threshold_seconds: PositiveFloat = Field(
        default=PositiveFloat(60.0),
        description="Warning threshold for package installations (seconds).",
    )


class ClaudeZygoteSettings(FrozenModel):
    """Top-level settings loaded from .changelings/settings.toml.

    All fields have defaults, so an empty or missing settings file
    produces a valid settings object with the standard defaults.
    """

    chat: ChatSettings = Field(
        default_factory=ChatSettings,
        description="Chat-related settings ([chat] section).",
    )
    watchers: WatcherSettings = Field(
        default_factory=WatcherSettings,
        description="Watcher settings ([watchers] section).",
    )
    provisioning: ProvisioningSettings = Field(
        default_factory=ProvisioningSettings,
        description="Provisioning timeout settings ([provisioning] section).",
    )
