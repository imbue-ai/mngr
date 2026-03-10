from __future__ import annotations

from typing import Any
from typing import Final

from pydantic import Field

from imbue.imbue_common.event_envelope import EventEnvelope
from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.primitives import NonNegativeInt
from imbue.imbue_common.primitives import PositiveFloat
from imbue.imbue_common.primitives import PositiveInt
from imbue.mng_claude_changeling.resources.watcher_common import DEFAULT_CEL_FILTER


class ConversationId(NonEmptyStr):
    """Unique identifier for a conversation thread (matches llm's conversation_id format)."""


class ChatModel(NonEmptyStr):
    """Model name used for chat conversations (e.g. 'claude-sonnet-4-6')."""


class MessageRole(NonEmptyStr):
    """Role of a message sender (e.g. 'user', 'assistant')."""


# -- Event log sources --
# These constants define the source names and corresponding log paths.
# Event sources write to events/<SOURCE>/events.jsonl (proper EventEnvelope format).
# Log sources write to logs/<SOURCE>/events.jsonl (raw format, not EventEnvelope).

SOURCE_MESSAGES: Final[EventSource] = EventSource("messages")
SOURCE_SCHEDULED: Final[EventSource] = EventSource("scheduled")
SOURCE_MNG_AGENTS: Final[EventSource] = EventSource("mng/agents")
SOURCE_STOP: Final[EventSource] = EventSource("stop")
SOURCE_MONITOR: Final[EventSource] = EventSource("monitor")
SOURCE_DELIVERY_FAILURES: Final[EventSource] = EventSource("delivery_failures")
SOURCE_COMMON_TRANSCRIPT: Final[EventSource] = EventSource("common_transcript")


class MessageEvent(EventEnvelope):
    """An event in events/messages/events.jsonl recording a conversation message.

    Each event represents a single user or assistant message. All messages
    across all conversations go into the same file, with conversation_id
    identifying which conversation the message belongs to.
    """

    conversation_id: ConversationId
    role: MessageRole
    content: str


class ChangelingEvent(EventEnvelope):
    """A generic event with a data payload, used for sources like scheduled,
    mng/agents, stop, and monitor.

    The data field carries event-type-specific payload.
    """

    data: dict[str, Any] = {}


# -- Common transcript types --
# These define the agent-agnostic common message format written to
# events/common_transcript/events.jsonl. Inspired by oh-my-pi's session
# entry format, but adapted to our EventEnvelope conventions.
#
# The common format focuses on semantically important messages (user input,
# assistant output, tool calls/results) and drops noise like progress events,
# file-history snapshots, and system bookkeeping.
#
# NOTE: These types document the schema produced by transcript_watcher.py.
# The watcher runs as a standalone script on the host and produces matching
# JSON directly (it cannot import these classes).


class CommonToolCallSummary(FrozenModel):
    """Summary of a single tool invocation within an assistant message."""

    tool_call_id: NonEmptyStr = Field(description="Unique ID for this tool call (from the API response).")
    tool_name: NonEmptyStr = Field(description="Name of the tool invoked (e.g. 'Bash', 'Read', 'Edit').")
    input_preview: str = Field(description="Truncated serialization of the tool input arguments.")


class CommonTokenUsage(FrozenModel):
    """Token usage counts for an assistant message."""

    input_tokens: NonNegativeInt = Field(description="Number of input tokens consumed.")
    output_tokens: NonNegativeInt = Field(description="Number of output tokens generated.")
    cache_read_tokens: NonNegativeInt | None = Field(
        default=None, description="Cache-read input tokens (None if not reported)."
    )
    cache_write_tokens: NonNegativeInt | None = Field(
        default=None, description="Cache-write input tokens (None if not reported)."
    )


class CommonUserMessageEvent(EventEnvelope):
    """A user message in the common transcript.

    Emitted for each Claude ``type: "user"`` event that contains actual
    user-authored text (not tool results).
    """

    role: MessageRole = Field(default=MessageRole("user"), description="Always 'user'.")
    content: str = Field(description="The user's message text.")
    message_uuid: str | None = Field(
        default=None, description="UUID from the raw Claude JSONL event (for traceability)."
    )


class CommonAssistantMessageEvent(EventEnvelope):
    """An assistant response in the common transcript.

    Emitted for each Claude ``type: "assistant"`` event. Contains the
    concatenated text output, any tool calls, model info, and token usage.
    """

    role: MessageRole = Field(default=MessageRole("assistant"), description="Always 'assistant'.")
    model: ChatModel = Field(description="Model that generated this response (e.g. 'claude-opus-4.6').")
    text: str = Field(description="Concatenated text content blocks from the response.")
    tool_calls: tuple[CommonToolCallSummary, ...] = Field(default=(), description="Tool calls made in this response.")
    stop_reason: str | None = Field(default=None, description="Why the response ended (e.g. 'end_turn', 'tool_use').")
    usage: CommonTokenUsage | None = Field(default=None, description="Token usage for this response.")
    message_uuid: str | None = Field(
        default=None, description="UUID from the raw Claude JSONL event (for traceability)."
    )


class CommonToolResultEvent(EventEnvelope):
    """A tool result in the common transcript.

    Emitted for each tool result block found inside Claude ``type: "user"``
    events. These are the return values from tool invocations.
    """

    tool_call_id: NonEmptyStr = Field(description="ID of the tool call this result corresponds to.")
    tool_name: NonEmptyStr = Field(description="Name of the tool (resolved from the preceding assistant message).")
    output: str = Field(description="Truncated output text from the tool.")
    is_error: bool = Field(description="Whether the tool returned an error.")
    message_uuid: str | None = Field(
        default=None, description="UUID from the raw Claude JSONL event (for traceability)."
    )


# -- Settings types --
# These model the structure of changelings.toml.
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
        "When None, chat.sh falls back to the hardcoded default (claude-opus-4.6).",
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
    transcript_poll_interval_seconds: PositiveInt = Field(
        default=PositiveInt(5),
        description="Poll interval for the transcript watcher (seconds).",
    )
    event_cel_filter: str = Field(
        default=DEFAULT_CEL_FILTER,
        description="CEL filter expression passed to 'mng events --filter'. "
        "Controls which event sources the event watcher receives.",
    )
    event_burst_size: PositiveInt = Field(
        default=PositiveInt(5),
        description="Number of messages allowed in the initial burst before rate limiting kicks in.",
    )
    max_event_messages_per_minute: PositiveInt = Field(
        default=PositiveInt(10),
        description="Maximum event messages delivered to the agent per minute (sustained rate).",
    )
    high_rate_warning_threshold_per_minute: PositiveInt = Field(
        default=PositiveInt(8),
        description="When messages per minute exceeds this, include a rate warning in the delivery envelope.",
    )
    max_delivery_retries: PositiveInt = Field(
        default=PositiveInt(3),
        description="Maximum consecutive delivery failures before notifying the user. "
        "Uses exponential backoff between retries.",
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


class ClaudeChangelingSettings(FrozenModel):
    """Top-level settings loaded from changelings.toml.

    All fields have defaults, so an empty or missing settings file
    produces a valid settings object with the standard defaults.
    """

    agent_type: str | None = Field(
        default=None,
        description="Agent type for this changeling (e.g. 'elena-code', 'claude-changeling'). "
        "Used by changeling deploy to determine the agent type when --agent-type is not provided on the CLI.",
    )
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
