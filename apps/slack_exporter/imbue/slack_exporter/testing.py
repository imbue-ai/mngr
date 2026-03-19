from typing import Any

from imbue.imbue_common.event_envelope import EventId
from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import EventType
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.slack_exporter.data_types import ChannelEvent
from imbue.slack_exporter.data_types import MessageEvent
from imbue.slack_exporter.data_types import ReactionItemEvent
from imbue.slack_exporter.data_types import ReplyEvent
from imbue.slack_exporter.data_types import SelfIdentityEvent
from imbue.slack_exporter.data_types import SlackApiCaller
from imbue.slack_exporter.data_types import UnreadMarkerEvent
from imbue.slack_exporter.data_types import UserEvent
from imbue.slack_exporter.primitives import SlackChannelId
from imbue.slack_exporter.primitives import SlackChannelName
from imbue.slack_exporter.primitives import SlackMessageTimestamp
from imbue.slack_exporter.primitives import SlackUserId
from imbue.slack_exporter.primitives import SlackUserName

FIXED_TIMESTAMP = IsoTimestamp("2025-01-15T12:00:00.000000000Z")
FIXED_EVENT_ID = EventId("evt-test00000000000000000000000000")


def make_channel_event(
    channel_id: str = "C123",
    channel_name: str = "general",
    raw: dict[str, Any] | None = None,
) -> ChannelEvent:
    return ChannelEvent(
        timestamp=FIXED_TIMESTAMP,
        type=EventType("channel_fetched"),
        event_id=FIXED_EVENT_ID,
        source=EventSource("channels"),
        channel_id=SlackChannelId(channel_id),
        channel_name=SlackChannelName(channel_name),
        raw=raw if raw is not None else {"id": channel_id, "name": channel_name},
    )


def make_message_event(
    channel_id: str = "C123",
    channel_name: str = "general",
    ts: str = "1700000000.000001",
) -> MessageEvent:
    return MessageEvent(
        timestamp=FIXED_TIMESTAMP,
        type=EventType("message_fetched"),
        event_id=FIXED_EVENT_ID,
        source=EventSource("messages"),
        channel_id=SlackChannelId(channel_id),
        channel_name=SlackChannelName(channel_name),
        message_ts=SlackMessageTimestamp(ts),
        raw={"ts": ts, "text": "hello"},
    )


def make_user_event(
    user_id: str = "U123",
) -> UserEvent:
    return UserEvent(
        timestamp=FIXED_TIMESTAMP,
        type=EventType("user_fetched"),
        event_id=FIXED_EVENT_ID,
        source=EventSource("users"),
        user_id=SlackUserId(user_id),
        raw={"id": user_id, "name": "testuser"},
    )


def make_reply_event(
    channel_id: str = "C123",
    channel_name: str = "general",
    thread_ts: str = "1700000000.000001",
    reply_ts: str = "1700000000.000002",
) -> ReplyEvent:
    return ReplyEvent(
        timestamp=FIXED_TIMESTAMP,
        type=EventType("reply_fetched"),
        event_id=FIXED_EVENT_ID,
        source=EventSource("replies"),
        channel_id=SlackChannelId(channel_id),
        channel_name=SlackChannelName(channel_name),
        thread_ts=SlackMessageTimestamp(thread_ts),
        reply_ts=SlackMessageTimestamp(reply_ts),
        raw={"ts": reply_ts, "thread_ts": thread_ts, "text": "reply"},
    )


def make_self_identity_event(
    user_id: str = "U123",
    user_name: str = "testuser",
    raw: dict[str, Any] | None = None,
) -> SelfIdentityEvent:
    return SelfIdentityEvent(
        timestamp=FIXED_TIMESTAMP,
        type=EventType("self_identity_fetched"),
        event_id=FIXED_EVENT_ID,
        source=EventSource("self_identity"),
        user_id=SlackUserId(user_id),
        user_name=SlackUserName(user_name),
        raw=raw if raw is not None else {"ok": True, "user_id": user_id, "user": user_name},
    )


def make_unread_marker_event(
    channel_id: str = "C123",
    channel_name: str = "general",
    last_read_ts: str = "1700000000.000001",
) -> UnreadMarkerEvent:
    return UnreadMarkerEvent(
        timestamp=FIXED_TIMESTAMP,
        type=EventType("unread_marker_fetched"),
        event_id=FIXED_EVENT_ID,
        source=EventSource("unread_markers"),
        channel_id=SlackChannelId(channel_id),
        channel_name=SlackChannelName(channel_name),
        last_read_ts=SlackMessageTimestamp(last_read_ts),
        raw={"channel_id": channel_id, "last_read": last_read_ts},
    )


def make_reaction_item_event(
    user_id: str = "U123",
    channel: str = "C123",
    message_ts: str = "1700000000.000001",
    reaction_name: str = "thumbsup",
) -> ReactionItemEvent:
    return ReactionItemEvent(
        timestamp=FIXED_TIMESTAMP,
        type=EventType("reaction_item_fetched"),
        event_id=FIXED_EVENT_ID,
        source=EventSource("reactions"),
        user_id=SlackUserId(user_id),
        raw={
            "type": "message",
            "channel": channel,
            "message": {
                "ts": message_ts,
                "text": "hello",
                "reactions": [{"name": reaction_name, "users": [user_id], "count": 1}],
            },
        },
    )


def make_slack_response(
    response_key: str,
    items: list[dict[str, Any]],
    has_more: bool = False,
    next_cursor: str = "",
) -> dict[str, Any]:
    """Build a fake Slack API response with the given items under response_key."""
    response: dict[str, Any] = {
        "ok": True,
        response_key: items,
        "has_more": has_more,
        "response_metadata": {"next_cursor": next_cursor},
    }
    return response


def make_fake_api_caller(
    response_by_method: dict[str, list[dict[str, Any]]],
) -> SlackApiCaller:
    """Create a fake SlackApiCaller that returns pre-configured responses per method."""
    call_index_by_method: dict[str, int] = {}

    def fake_api_caller(method: str, query_params: dict[str, str] | None = None) -> dict[str, Any]:
        responses = response_by_method.get(method, [])
        idx = call_index_by_method.get(method, 0)
        call_index_by_method[method] = idx + 1
        return responses[idx]

    return fake_api_caller
