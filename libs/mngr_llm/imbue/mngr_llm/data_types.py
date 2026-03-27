from __future__ import annotations

from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.primitives import PositiveFloat
from imbue.imbue_common.primitives import PositiveInt


class ChatModel(NonEmptyStr):
    """Model name used for chat conversations (e.g. 'claude-sonnet-4-6')."""


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
    mngr_list_hard_timeout_seconds: PositiveFloat = Field(
        default=PositiveFloat(120.0),
        description="Hard timeout for mngr list command (seconds).",
    )
    mngr_list_warn_threshold_seconds: PositiveFloat = Field(
        default=PositiveFloat(15.0),
        description="Warning threshold for mngr list command (seconds).",
    )


DEFAULT_WELCOME_MESSAGE: Final[NonEmptyStr] = NonEmptyStr("Hi! How can I help?")


class ChatSettings(FrozenModel):
    """Settings for the [chat] TOML section."""

    model: ChatModel | None = Field(
        default=None,
        description="Default model for new conversation threads. "
        "When None, chat.sh falls back to the hardcoded default (claude-haiku-4.5).",
    )
    welcome_message: NonEmptyStr = Field(
        default=DEFAULT_WELCOME_MESSAGE,
        description="Initial welcome message displayed in the first daily conversation. "
        "Configurable in minds.toml under [chat].welcome_message.",
    )
    context: ContextSettings = Field(
        default_factory=ContextSettings,
        description="Context tool settings ([chat.context] section).",
    )
    extra_context: ExtraContextSettings = Field(
        default_factory=ExtraContextSettings,
        description="Extra context tool settings ([chat.extra_context] section).",
    )


class ProvisioningSettings(FrozenModel):
    """Timeout settings for provisioning operations.

    Used by both mngr_llm and mngr_claude_mind for consistent timeout handling
    when executing commands on hosts during provisioning.
    """

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


class LlmSettings(FrozenModel):
    """Settings for the llm agent, loaded from minds.toml or a similar config file.

    All fields have defaults, so an empty or missing settings file
    produces a valid settings object with the standard defaults.
    """

    chat: ChatSettings = Field(
        default_factory=ChatSettings,
        description="Chat-related settings ([chat] section).",
    )
    provisioning: ProvisioningSettings = Field(
        default_factory=ProvisioningSettings,
        description="Provisioning timeout settings ([provisioning] section).",
    )
    max_concurrent_workers: PositiveInt = Field(
        default=PositiveInt(10),
        description="Maximum number of concurrent working roles that the mind should run.",
    )
