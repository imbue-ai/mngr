from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.slack_exporter.primitives import SlackChannelId
from imbue.slack_exporter.primitives import SlackChannelName
from imbue.slack_exporter.primitives import SlackMessageTimestamp
from imbue.slack_exporter.primitives import SlackUserId

SlackApiCaller = Callable[[str, dict[str, str] | None], dict[str, Any]]


class ChannelConfig(FrozenModel):
    """Per-channel export configuration."""

    name: SlackChannelName = Field(description="Channel name without '#'")
    oldest: datetime | None = Field(
        default=None,
        description="How far back to look for this channel (overrides global default)",
    )


class ExporterSettings(FrozenModel):
    """Top-level settings for the slack exporter."""

    channels: tuple[ChannelConfig, ...] = Field(
        default=(ChannelConfig(name=SlackChannelName("general")),),
        description="Channels to export",
    )
    default_oldest: datetime = Field(
        description="Default earliest date to fetch messages from",
    )
    output_dir: Path = Field(
        default=Path("slack_export"),
        description="Directory for storing exported data (channels/, messages/, users/ subdirs)",
    )


class StoredChannelInfo(FrozenModel):
    """A channel info record stored in channels/events.jsonl."""

    channel_id: SlackChannelId = Field(description="Slack channel ID")
    channel_name: SlackChannelName = Field(description="Channel name")
    fetched_at: datetime = Field(description="When this info was fetched")
    raw: dict[str, Any] = Field(description="Raw Slack API response for the channel")


class StoredMessage(FrozenModel):
    """A message record stored in messages/events.jsonl."""

    channel_id: SlackChannelId = Field(description="Slack channel ID")
    channel_name: SlackChannelName = Field(description="Channel name at time of fetch")
    timestamp: SlackMessageTimestamp = Field(description="Slack message ts")
    fetched_at: datetime = Field(description="When this message was fetched")
    raw: dict[str, Any] = Field(description="Raw Slack API message payload")


class StoredUser(FrozenModel):
    """A user info record stored in users/events.jsonl."""

    user_id: SlackUserId = Field(description="Slack user ID")
    fetched_at: datetime = Field(description="When this user info was fetched")
    raw: dict[str, Any] = Field(description="Raw Slack API user payload")


class ChannelExportState(FrozenModel):
    """Tracks the export state for a single channel derived from messages/events.jsonl."""

    channel_id: SlackChannelId = Field(description="Slack channel ID")
    channel_name: SlackChannelName = Field(description="Channel name")
    latest_message_timestamp: SlackMessageTimestamp | None = Field(
        default=None,
        description="The most recent message timestamp we have for this channel",
    )
