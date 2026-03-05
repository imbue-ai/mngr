import logging
from datetime import datetime
from datetime import timezone

from imbue.slack_exporter.data_types import SlackApiCaller
from imbue.slack_exporter.data_types import StoredChannelInfo
from imbue.slack_exporter.data_types import StoredUser
from imbue.slack_exporter.errors import ChannelNotFoundError
from imbue.slack_exporter.latchkey import extract_next_cursor
from imbue.slack_exporter.primitives import SlackChannelId
from imbue.slack_exporter.primitives import SlackChannelName
from imbue.slack_exporter.primitives import SlackUserId

logger = logging.getLogger(__name__)


def fetch_channel_list(api_caller: SlackApiCaller) -> list[StoredChannelInfo]:
    """Fetch all non-archived channels from Slack and return them as StoredChannelInfo records."""
    all_channels: list[StoredChannelInfo] = []
    cursor: str | None = None
    now = datetime.now(timezone.utc)

    while True:
        params: dict[str, str] = {
            "exclude_archived": "true",
            "limit": "200",
            "types": "public_channel,private_channel",
        }
        if cursor:
            params["cursor"] = cursor

        data = api_caller("conversations.list", params)

        for channel_raw in data.get("channels", []):
            channel_info = StoredChannelInfo(
                channel_id=SlackChannelId(channel_raw["id"]),
                channel_name=SlackChannelName(channel_raw["name"]),
                fetched_at=now,
                raw=channel_raw,
            )
            all_channels.append(channel_info)

        next_cursor = extract_next_cursor(data)
        if not next_cursor:
            break
        cursor = next_cursor

    logger.info("Fetched %d channels from Slack", len(all_channels))
    return all_channels


def fetch_user_list(api_caller: SlackApiCaller) -> list[StoredUser]:
    """Fetch all users from Slack and return them as StoredUser records."""
    all_users: list[StoredUser] = []
    cursor: str | None = None
    now = datetime.now(timezone.utc)

    while True:
        params: dict[str, str] = {"limit": "200"}
        if cursor:
            params["cursor"] = cursor

        data = api_caller("users.list", params)

        for user_raw in data.get("members", []):
            user = StoredUser(
                user_id=SlackUserId(user_raw["id"]),
                fetched_at=now,
                raw=user_raw,
            )
            all_users.append(user)

        next_cursor = extract_next_cursor(data)
        if not next_cursor:
            break
        cursor = next_cursor

    logger.info("Fetched %d users from Slack", len(all_users))
    return all_users


def resolve_channel_id(
    channel_name: SlackChannelName,
    channel_info_records: list[StoredChannelInfo],
    cached_channel_id_by_name: dict[SlackChannelName, SlackChannelId],
) -> SlackChannelId:
    """Resolve a channel name to its ID, using fetched info or cached mappings.

    Raises ChannelNotFoundError if the channel cannot be found.
    """
    # Check freshly fetched channel info first
    for info in channel_info_records:
        if info.channel_name == channel_name:
            return info.channel_id

    # Fall back to cached mapping
    cached_id = cached_channel_id_by_name.get(channel_name)
    if cached_id is not None:
        return cached_id

    raise ChannelNotFoundError(channel_name)
