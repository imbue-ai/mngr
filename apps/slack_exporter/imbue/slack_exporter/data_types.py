from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import Field

from imbue.imbue_common.event_envelope import EventEnvelope
from imbue.imbue_common.event_envelope import EventId
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.slack_exporter.primitives import SlackChannelId
from imbue.slack_exporter.primitives import SlackChannelName
from imbue.slack_exporter.primitives import SlackMessageTimestamp
from imbue.slack_exporter.primitives import SlackUserId
from imbue.slack_exporter.primitives import SlackUserName

SlackApiCaller = Callable[[str, dict[str, str] | None], dict[str, Any]]


def make_event_id() -> EventId:
    return EventId(f"evt-{uuid4().hex}")


def make_iso_timestamp() -> IsoTimestamp:
    now = datetime.now(timezone.utc)
    return IsoTimestamp(now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond:06d}000Z")


class ChannelConfig(FrozenModel):
    """Per-channel export configuration."""

    name: SlackChannelName = Field(description="Channel name without '#'")
    oldest: datetime | None = Field(
        default=None,
        description="How far back to look for this channel (overrides global default)",
    )


class ExporterSettings(FrozenModel):
    """Top-level settings for the slack exporter."""

    channels: tuple[ChannelConfig, ...] | None = Field(
        default=None,
        description="Channels to export. When None, all channels from the fetched channel list are exported.",
    )
    recently_active_channels: int | None = Field(
        default=None,
        description="If set, restrict to the N channels with the most recent messages (from historical data).",
    )
    default_oldest: datetime = Field(
        description="Default earliest date to fetch messages from",
    )
    output_dir: Path = Field(
        default=Path("slack_export"),
        description="Directory for storing exported data",
    )
    refresh: bool = Field(
        default=False,
        description="Force re-fetch of all cached data (channels, users, self identity)",
    )
    members_only: bool = Field(
        default=True,
        description="Only export channels where the authenticated user is a member",
    )
    max_recent_threads_for_reactions: int = Field(
        default=50,
        description="Number of most recent relevant threads to check for reaction changes after export",
    )
    cache_ttl_seconds: int = Field(
        default=600,
        description="How long to cache channel/user/identity data before re-fetching (seconds)",
    )


class ChannelEvent(EventEnvelope):
    """An event envelope wrapping a Slack channel record."""

    channel_id: SlackChannelId = Field(description="Slack channel ID")
    channel_name: SlackChannelName = Field(description="Channel name")
    raw: dict[str, Any] = Field(description="Raw Slack API response for the channel")


class MessageEvent(EventEnvelope):
    """An event envelope wrapping a Slack message record."""

    channel_id: SlackChannelId = Field(description="Slack channel ID")
    channel_name: SlackChannelName = Field(description="Channel name at time of fetch")
    message_ts: SlackMessageTimestamp = Field(description="Slack message ts")
    raw: dict[str, Any] = Field(description="Raw Slack API message payload")


class UserEvent(EventEnvelope):
    """An event envelope wrapping a Slack user record."""

    user_id: SlackUserId = Field(description="Slack user ID")
    raw: dict[str, Any] = Field(description="Raw Slack API user payload")


class ReplyEvent(EventEnvelope):
    """An event envelope wrapping a Slack thread reply record."""

    channel_id: SlackChannelId = Field(description="Slack channel ID")
    channel_name: SlackChannelName = Field(description="Channel name at time of fetch")
    thread_ts: SlackMessageTimestamp = Field(description="Parent message ts (thread root)")
    reply_ts: SlackMessageTimestamp = Field(description="This reply's ts")
    raw: dict[str, Any] = Field(description="Raw Slack API reply payload")


class SelfIdentityEvent(EventEnvelope):
    """An event envelope wrapping the result of auth.test for the authenticated user."""

    user_id: SlackUserId = Field(description="Slack user ID of the authenticated user")
    user_name: SlackUserName = Field(description="Slack user name of the authenticated user")
    raw: dict[str, Any] = Field(description="Raw Slack API auth.test response")


class UnreadMarkerEvent(EventEnvelope):
    """An event envelope wrapping an unread marker (last_read position) for a conversation."""

    channel_id: SlackChannelId = Field(description="Slack channel ID")
    channel_name: SlackChannelName = Field(description="Channel name at time of fetch")
    last_read_ts: SlackMessageTimestamp = Field(description="Timestamp up to which the user has read")
    raw: dict[str, Any] = Field(description="Raw unread marker data")


class ReactionEvent(EventEnvelope):
    """An event envelope wrapping the reaction state of a Slack message or reply."""

    channel_id: SlackChannelId = Field(description="Slack channel ID")
    channel_name: SlackChannelName = Field(description="Channel name at time of extraction")
    message_ts: SlackMessageTimestamp = Field(description="Timestamp of the message with reactions")
    thread_ts: SlackMessageTimestamp | None = Field(
        default=None,
        description="Parent thread ts if this is a reply, None for top-level messages",
    )
    raw: dict[str, Any] = Field(description="Contains the reactions list from the message")


class RelevantThreadEvent(EventEnvelope):
    """An event envelope recording that a thread is relevant to the authenticated user."""

    channel_id: SlackChannelId = Field(description="Slack channel ID")
    channel_name: SlackChannelName = Field(description="Channel name at time of detection")
    thread_ts: SlackMessageTimestamp = Field(description="Thread root message ts")
    relevance_reasons: tuple[str, ...] = Field(
        description="Why this thread is relevant: 'mentioned', 'participated'",
    )
    raw: dict[str, Any] = Field(description="Relevance summary data")


class ChannelExportState(FrozenModel):
    """Tracks the export state for a single channel derived from message events."""

    channel_id: SlackChannelId = Field(description="Slack channel ID")
    channel_name: SlackChannelName = Field(description="Channel name")
    latest_message_timestamp: SlackMessageTimestamp | None = Field(
        default=None,
        description="The most recent message timestamp we have for this channel",
    )
