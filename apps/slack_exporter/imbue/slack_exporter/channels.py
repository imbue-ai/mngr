import logging
from typing import Any

from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import EventType
from imbue.slack_exporter.data_types import ChannelEvent
from imbue.slack_exporter.data_types import ReactionItemEvent
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

_CHANNEL_SOURCE = EventSource("channels")
_REACTION_SOURCE = EventSource("reactions")
_SELF_IDENTITY_SOURCE = EventSource("self_identity")
_UNREAD_MARKER_SOURCE = EventSource("unread_markers")
_USER_SOURCE = EventSource("users")


def fetch_channel_list(api_caller: SlackApiCaller, members_only: bool = True) -> list[ChannelEvent]:
    """Fetch non-archived channels from Slack.

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
        type=EventType("channel_fetched"),
        event_id=make_event_id(),
        source=_CHANNEL_SOURCE,
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
        type=EventType("self_identity_fetched"),
        event_id=make_event_id(),
        source=_SELF_IDENTITY_SOURCE,
        user_id=SlackUserId(data["user_id"]),
        user_name=SlackUserName(data["user"]),
        raw=data,
    )


def extract_unread_markers(channel_events: list[ChannelEvent]) -> list[UnreadMarkerEvent]:
    """Extract unread markers from fetched channel events.

    Only channels with a non-empty last_read field are included (i.e. channels the
    authenticated user has joined).
    """
    markers: list[UnreadMarkerEvent] = []
    for event in channel_events:
        last_read = event.raw.get("last_read")
        if not last_read:
            continue
        markers.append(
            UnreadMarkerEvent(
                timestamp=make_iso_timestamp(),
                type=EventType("unread_marker_fetched"),
                event_id=make_event_id(),
                source=_UNREAD_MARKER_SOURCE,
                channel_id=event.channel_id,
                channel_name=event.channel_name,
                last_read_ts=SlackMessageTimestamp(last_read),
                raw={"channel_id": str(event.channel_id), "last_read": last_read},
            )
        )
    logger.info("Extracted %d unread markers from channel data", len(markers))
    return markers


def fetch_user_reactions(api_caller: SlackApiCaller, user_id: SlackUserId) -> list[ReactionItemEvent]:
    """Fetch all items the given user has reacted to via reactions.list."""
    raw_items = fetch_paginated(
        api_caller=api_caller,
        method="reactions.list",
        base_params={"user": user_id, "limit": "1000"},
        response_key="items",
    )
    logger.info("Fetched %d reaction items from Slack", len(raw_items))
    return [
        ReactionItemEvent(
            timestamp=make_iso_timestamp(),
            type=EventType("reaction_item_fetched"),
            event_id=make_event_id(),
            source=_REACTION_SOURCE,
            user_id=user_id,
            raw=raw,
        )
        for raw in raw_items
    ]


def _make_user_event(user_raw: dict[str, Any]) -> UserEvent:
    return UserEvent(
        timestamp=make_iso_timestamp(),
        type=EventType("user_fetched"),
        event_id=make_event_id(),
        source=_USER_SOURCE,
        user_id=SlackUserId(user_raw["id"]),
        raw=user_raw,
    )
