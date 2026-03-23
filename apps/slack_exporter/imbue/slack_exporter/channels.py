import logging
from typing import Any

from pydantic import Field

from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import EventType
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.slack_exporter.data_types import ChannelEvent
from imbue.slack_exporter.data_types import SelfIdentityEvent
from imbue.slack_exporter.data_types import SlackApiCaller
from imbue.slack_exporter.data_types import UnreadMarkerEvent
from imbue.slack_exporter.data_types import UserEvent
from imbue.slack_exporter.data_types import make_event_id
from imbue.slack_exporter.data_types import make_iso_timestamp
from imbue.slack_exporter.errors import ChannelNotFoundError
from imbue.slack_exporter.latchkey import fetch_paginated
from imbue.slack_exporter.primitives import SlackChannelId
from imbue.slack_exporter.primitives import SlackChannelName
from imbue.slack_exporter.primitives import SlackMessageTimestamp
from imbue.slack_exporter.primitives import SlackUserId
from imbue.slack_exporter.primitives import SlackUserName

logger = logging.getLogger(__name__)

_SLACK_SOURCE = EventSource("slack")


def fetch_raw_channel_list(
    api_caller: SlackApiCaller,
    members_only: bool = True,
) -> list[dict[str, Any]]:
    """Fetch raw non-archived channel dicts from Slack via conversations.list.

    When members_only is True (default), only channels where the authenticated
    user is a member are returned.
    """
    raw_channels = fetch_paginated(
        api_caller=api_caller,
        method="conversations.list",
        base_params={"exclude_archived": "true", "limit": "200", "types": "public_channel,private_channel"},
        response_key="channels",
    )
    if members_only:
        raw_channels = [ch for ch in raw_channels if ch.get("is_member", False)]
    return raw_channels


def fetch_channel_list(api_caller: SlackApiCaller, members_only: bool = True) -> list[ChannelEvent]:
    """Fetch non-archived channels from Slack as ChannelEvent objects.

    When members_only is True (default), only channels where the authenticated
    user is a member are returned.
    """
    raw_channels = fetch_raw_channel_list(api_caller=api_caller, members_only=members_only)
    channels = [_make_channel_event(raw) for raw in raw_channels]
    logger.info("Fetched %d channels from Slack", len(channels))
    return channels


def fetch_user_list(api_caller: SlackApiCaller) -> list[UserEvent]:
    """Fetch all users from Slack."""
    raw_users = fetch_paginated(
        api_caller=api_caller,
        method="users.list",
        base_params={"limit": "200"},
        response_key="members",
    )
    users = [_make_user_event(raw) for raw in raw_users]
    logger.info("Fetched %d users from Slack", len(users))
    return users


def resolve_channel_id(
    channel_name: SlackChannelName,
    channel_events: list[ChannelEvent],
    cached_channel_id_by_name: dict[SlackChannelName, SlackChannelId],
) -> SlackChannelId:
    """Resolve a channel name to its ID, using fetched events or cached mappings.

    Raises ChannelNotFoundError if the channel cannot be found.
    """
    for event in channel_events:
        if event.channel_name == channel_name:
            return event.channel_id

    cached_id = cached_channel_id_by_name.get(channel_name)
    if cached_id is not None:
        return cached_id

    raise ChannelNotFoundError(channel_name)


def _make_channel_event(channel_raw: dict[str, Any]) -> ChannelEvent:
    return ChannelEvent(
        timestamp=make_iso_timestamp(),
        type=EventType("channel"),
        event_id=make_event_id(),
        source=_SLACK_SOURCE,
        channel_id=SlackChannelId(channel_raw["id"]),
        channel_name=SlackChannelName(channel_raw["name"]),
        raw=channel_raw,
    )


def fetch_self_identity(api_caller: SlackApiCaller) -> SelfIdentityEvent:
    """Fetch the authenticated user's identity via auth.test."""
    data = api_caller("auth.test", None)
    logger.info("Fetched self identity: user_id=%s, user=%s", data["user_id"], data["user"])
    return SelfIdentityEvent(
        timestamp=make_iso_timestamp(),
        type=EventType("self_identity"),
        event_id=make_event_id(),
        source=_SLACK_SOURCE,
        user_id=SlackUserId(data["user_id"]),
        user_name=SlackUserName(data["user"]),
        raw=data,
    )


class ChannelInfoResult(FrozenModel):
    """Result of fetching per-channel info via conversations.info."""

    unread_markers: tuple[UnreadMarkerEvent, ...] = Field(description="Unread marker events")
    updated_channels: tuple[ChannelEvent, ...] = Field(
        description="Channel events updated from conversations.info responses",
    )


def fetch_channel_info(
    api_caller: SlackApiCaller,
    channel_events: list[ChannelEvent],
) -> ChannelInfoResult:
    """Fetch per-channel info via conversations.info.

    Returns unread markers and updated channel events from the conversations.info
    responses (which include the full channel object).
    """
    markers: list[UnreadMarkerEvent] = []
    updated_channels: list[ChannelEvent] = []
    total_channels = len(channel_events)
    for channel_idx, event in enumerate(channel_events):
        if total_channels > 1:
            logger.info("  Fetching channel info %d/%d: %s", channel_idx + 1, total_channels, event.channel_name)
        data = api_caller("conversations.info", {"channel": str(event.channel_id)})
        channel_info = data.get("channel", {})

        # Build an updated channel event from the conversations.info response.
        # Strip user-specific fields (last_read, latest) so the raw dict is comparable
        # to what conversations.list returns for stable diff comparisons.
        if channel_info.get("id") and channel_info.get("name"):
            channel_raw_for_event = {k: v for k, v in channel_info.items() if k not in ("last_read", "latest")}
            updated_channels.append(_make_channel_event(channel_raw_for_event))

        last_read = channel_info.get("last_read")
        if last_read:
            markers.append(
                UnreadMarkerEvent(
                    timestamp=make_iso_timestamp(),
                    type=EventType("unread_marker"),
                    event_id=make_event_id(),
                    source=_SLACK_SOURCE,
                    channel_id=event.channel_id,
                    channel_name=event.channel_name,
                    last_read_ts=SlackMessageTimestamp(last_read),
                    raw={"channel_id": str(event.channel_id), "last_read": last_read},
                )
            )

    logger.info("Fetched info for %d channels (%d unread markers)", len(channel_events), len(markers))
    return ChannelInfoResult(
        unread_markers=tuple(markers),
        updated_channels=tuple(updated_channels),
    )


def _make_user_event(user_raw: dict[str, Any]) -> UserEvent:
    return UserEvent(
        timestamp=make_iso_timestamp(),
        type=EventType("user"),
        event_id=make_event_id(),
        source=_SLACK_SOURCE,
        user_id=SlackUserId(user_raw["id"]),
        raw=user_raw,
    )
