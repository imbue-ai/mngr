from datetime import datetime
from datetime import timezone
from typing import Any

from imbue.slack_exporter.data_types import SlackApiCaller
from imbue.slack_exporter.data_types import StoredChannelInfo
from imbue.slack_exporter.data_types import StoredMessage
from imbue.slack_exporter.data_types import StoredUser
from imbue.slack_exporter.primitives import SlackChannelId
from imbue.slack_exporter.primitives import SlackChannelName
from imbue.slack_exporter.primitives import SlackMessageTimestamp
from imbue.slack_exporter.primitives import SlackUserId

FIXED_FETCH_TIME = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def make_stored_message(
    channel_id: str = "C123",
    channel_name: str = "general",
    ts: str = "1700000000.000001",
) -> StoredMessage:
    return StoredMessage(
        channel_id=SlackChannelId(channel_id),
        channel_name=SlackChannelName(channel_name),
        timestamp=SlackMessageTimestamp(ts),
        fetched_at=FIXED_FETCH_TIME,
        raw={"ts": ts, "text": "hello"},
    )


def make_stored_channel_info(
    channel_id: str = "C123",
    channel_name: str = "general",
) -> StoredChannelInfo:
    return StoredChannelInfo(
        channel_id=SlackChannelId(channel_id),
        channel_name=SlackChannelName(channel_name),
        fetched_at=FIXED_FETCH_TIME,
        raw={"id": channel_id, "name": channel_name},
    )


def make_fake_api_caller(
    response_by_method: dict[str, list[dict[str, Any]]],
) -> SlackApiCaller:
    """Create a fake SlackApiCaller that returns pre-configured responses per method.

    Each method maps to a list of responses returned in order (for pagination).
    """
    call_index_by_method: dict[str, int] = {}

    def fake_api_caller(method: str, query_params: dict[str, str] | None = None) -> dict[str, Any]:
        responses = response_by_method.get(method, [])
        idx = call_index_by_method.get(method, 0)
        call_index_by_method[method] = idx + 1
        return responses[idx]

    return fake_api_caller


def make_stored_user(
    user_id: str = "U123",
) -> StoredUser:
    return StoredUser(
        user_id=SlackUserId(user_id),
        fetched_at=FIXED_FETCH_TIME,
        raw={"id": user_id, "name": "testuser"},
    )
