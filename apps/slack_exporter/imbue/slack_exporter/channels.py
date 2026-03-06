import logging
from typing import Any

from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import EventType
from imbue.slack_exporter.data_types import ChannelEvent
from imbue.slack_exporter.data_types import SlackApiCaller
from imbue.slack_exporter.data_types import UserEvent
from imbue.slack_exporter.data_types import make_event_id
from imbue.slack_exporter.data_types import make_iso_timestamp
from imbue.slack_exporter.errors import ChannelNotFoundError
from imbue.slack_exporter.latchkey import fetch_paginated
from imbue.slack_exporter.primitives import SlackChannelId
from imbue.slack_exporter.primitives import SlackChannelName
from imbue.slack_exporter.primitives import SlackUserId

logger = logging.getLogger(__name__)

_CHANNEL_SOURCE = EventSource("channels")
_USER_SOURCE = EventSource("users")


def fetch_channel_list(api_caller: SlackApiCaller) -> list[ChannelEvent]:
    """Fetch all non-archived channels from Slack."""
    raw_channels = fetch_paginated(
        api_caller=api_caller,
        method="conversations.list",
        base_params={"exclude_archived": "true", "limit": "200", "types": "public_channel,private_channel"},
        response_key="channels",
    )
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


def _make_user_event(user_raw: dict[str, Any]) -> UserEvent:
    return UserEvent(
        timestamp=make_iso_timestamp(),
        type=EventType("user_fetched"),
        event_id=make_event_id(),
        source=_USER_SOURCE,
        user_id=SlackUserId(user_raw["id"]),
        raw=user_raw,
    )
