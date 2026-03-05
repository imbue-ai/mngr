import logging
from datetime import datetime
from datetime import timezone

from imbue.slack_exporter.channels import fetch_channel_list
from imbue.slack_exporter.channels import fetch_user_list
from imbue.slack_exporter.channels import resolve_channel_id
from imbue.slack_exporter.data_types import ChannelConfig
from imbue.slack_exporter.data_types import ChannelExportState
from imbue.slack_exporter.data_types import ExporterSettings
from imbue.slack_exporter.data_types import SlackApiCaller
from imbue.slack_exporter.data_types import StoredChannelInfo
from imbue.slack_exporter.data_types import StoredMessage
from imbue.slack_exporter.latchkey import extract_next_cursor
from imbue.slack_exporter.primitives import SlackChannelId
from imbue.slack_exporter.primitives import SlackChannelName
from imbue.slack_exporter.primitives import SlackMessageTimestamp
from imbue.slack_exporter.store import load_existing_channels
from imbue.slack_exporter.store import load_existing_message_state
from imbue.slack_exporter.store import load_existing_user_ids
from imbue.slack_exporter.store import save_channels
from imbue.slack_exporter.store import save_messages
from imbue.slack_exporter.store import save_users

logger = logging.getLogger(__name__)


def run_export(settings: ExporterSettings, api_caller: SlackApiCaller) -> None:
    """Run the full export process: load state, resolve channels, fetch new messages, save."""
    # Load existing state from the output directory
    existing_channel_by_id = load_existing_channels(settings.output_dir)
    state_by_channel_id, known_message_keys = load_existing_message_state(settings.output_dir)
    existing_user_ids = load_existing_user_ids(settings.output_dir)

    # Build a name-to-id mapping from existing channels
    channel_id_by_name: dict[SlackChannelName, SlackChannelId] = {
        info.channel_name: info.channel_id for info in existing_channel_by_id.values()
    }

    # Fetch and save channels (only if changed)
    fresh_channels = fetch_channel_list(api_caller)
    changed_channels = _filter_changed_channels(fresh_channels, existing_channel_by_id)
    save_channels(settings.output_dir, changed_channels)
    if changed_channels:
        logger.info("Saved %d new/changed channels", len(changed_channels))

    # Update name-to-id mapping with fresh data
    for info in fresh_channels:
        channel_id_by_name[info.channel_name] = info.channel_id

    # Fetch and save users (only new ones)
    fresh_users = fetch_user_list(api_caller)
    new_users = [u for u in fresh_users if u.user_id not in existing_user_ids]
    save_users(settings.output_dir, new_users)
    if new_users:
        logger.info("Saved %d new users", len(new_users))

    # Export each configured channel
    for channel_config in settings.channels:
        _export_single_channel(
            channel_config=channel_config,
            fresh_channels=fresh_channels,
            channel_id_by_name=channel_id_by_name,
            state_by_channel_id=state_by_channel_id,
            known_message_keys=known_message_keys,
            settings=settings,
            api_caller=api_caller,
        )


def _filter_changed_channels(
    fresh_channels: list[StoredChannelInfo],
    existing_channel_by_id: dict[SlackChannelId, StoredChannelInfo],
) -> list[StoredChannelInfo]:
    """Return only channels whose raw data differs from what is already stored."""
    changed: list[StoredChannelInfo] = []
    for channel in fresh_channels:
        existing = existing_channel_by_id.get(channel.channel_id)
        if existing is None or existing.raw != channel.raw:
            changed.append(channel)
    return changed


def _export_single_channel(
    channel_config: ChannelConfig,
    fresh_channels: list[StoredChannelInfo],
    channel_id_by_name: dict[SlackChannelName, SlackChannelId],
    state_by_channel_id: dict[SlackChannelId, ChannelExportState],
    known_message_keys: set[tuple[SlackChannelId, SlackMessageTimestamp]],
    settings: ExporterSettings,
    api_caller: SlackApiCaller,
) -> None:
    """Export messages from a single channel."""
    channel_id = resolve_channel_id(
        channel_config.name,
        fresh_channels,
        channel_id_by_name,
    )
    logger.info("Exporting channel %s (ID: %s)", channel_config.name, channel_id)

    existing_state = state_by_channel_id.get(channel_id)

    # Determine the oldest timestamp to fetch from
    oldest_datetime = channel_config.oldest or settings.default_oldest
    oldest_ts = _datetime_to_slack_timestamp(oldest_datetime)

    # If we already have messages, fetch only newer ones
    if existing_state and existing_state.latest_message_timestamp:
        oldest_ts = existing_state.latest_message_timestamp
        logger.info(
            "  Resuming from timestamp %s for channel %s",
            oldest_ts,
            channel_config.name,
        )

    all_fetched = _fetch_all_messages_for_channel(
        channel_id=channel_id,
        channel_name=channel_config.name,
        oldest_ts=oldest_ts,
        # When resuming, we already have the message at oldest_ts, so exclude it
        is_inclusive=existing_state is None or existing_state.latest_message_timestamp is None,
        api_caller=api_caller,
    )

    # Filter to only messages we haven't seen before
    new_messages = [m for m in all_fetched if (m.channel_id, m.timestamp) not in known_message_keys]

    if new_messages:
        save_messages(settings.output_dir, new_messages)
        logger.info("  Saved %d new messages from channel %s", len(new_messages), channel_config.name)
    else:
        logger.info("  No new messages in channel %s", channel_config.name)


def _fetch_all_messages_for_channel(
    channel_id: SlackChannelId,
    channel_name: SlackChannelName,
    oldest_ts: SlackMessageTimestamp,
    is_inclusive: bool,
    api_caller: SlackApiCaller,
) -> list[StoredMessage]:
    """Fetch all messages from a channel newer than oldest_ts, handling pagination."""
    all_messages: list[StoredMessage] = []
    cursor: str | None = None
    now = datetime.now(timezone.utc)

    while True:
        params: dict[str, str] = {
            "channel": channel_id,
            "oldest": oldest_ts,
            "inclusive": "true" if is_inclusive else "false",
            "include_all_metadata": "true",
            "limit": "200",
        }
        if cursor:
            params["cursor"] = cursor

        data = api_caller("conversations.history", params)

        for message_raw in data.get("messages", []):
            ts = message_raw.get("ts", "")
            if not ts:
                continue
            stored_message = StoredMessage(
                channel_id=channel_id,
                channel_name=channel_name,
                timestamp=SlackMessageTimestamp(ts),
                fetched_at=now,
                raw=message_raw,
            )
            all_messages.append(stored_message)

        if not data.get("has_more", False):
            break

        next_cursor = extract_next_cursor(data)
        if not next_cursor:
            break
        cursor = next_cursor

    return all_messages


def _datetime_to_slack_timestamp(dt: datetime) -> SlackMessageTimestamp:
    """Convert a datetime to a Slack-style timestamp string."""
    return SlackMessageTimestamp(f"{dt.timestamp():.6f}")
