import logging
from datetime import datetime

from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import EventType
from imbue.slack_exporter.channels import fetch_channel_list
from imbue.slack_exporter.channels import fetch_user_list
from imbue.slack_exporter.channels import resolve_channel_id
from imbue.slack_exporter.data_types import ChannelConfig
from imbue.slack_exporter.data_types import ChannelEvent
from imbue.slack_exporter.data_types import ChannelExportState
from imbue.slack_exporter.data_types import ExporterSettings
from imbue.slack_exporter.data_types import MessageEvent
from imbue.slack_exporter.data_types import SlackApiCaller
from imbue.slack_exporter.data_types import make_event_id
from imbue.slack_exporter.data_types import make_iso_timestamp
from imbue.slack_exporter.latchkey import extract_next_cursor
from imbue.slack_exporter.primitives import SlackChannelId
from imbue.slack_exporter.primitives import SlackChannelName
from imbue.slack_exporter.primitives import SlackMessageTimestamp
from imbue.slack_exporter.store import load_existing_channels
from imbue.slack_exporter.store import load_existing_message_state
from imbue.slack_exporter.store import load_existing_user_ids
from imbue.slack_exporter.store import save_channel_events
from imbue.slack_exporter.store import save_message_events
from imbue.slack_exporter.store import save_user_events

logger = logging.getLogger(__name__)

_MESSAGE_SOURCE = EventSource("messages")


def run_export(settings: ExporterSettings, api_caller: SlackApiCaller) -> None:
    """Run the full export process: load state, resolve channels, fetch new messages, save."""
    existing_channel_by_id = load_existing_channels(settings.output_dir)
    state_by_channel_id, known_message_keys = load_existing_message_state(settings.output_dir)
    existing_user_ids = load_existing_user_ids(settings.output_dir)

    channel_id_by_name: dict[SlackChannelName, SlackChannelId] = {
        event.channel_name: event.channel_id for event in existing_channel_by_id.values()
    }

    # Fetch channels and split into created vs updated
    fresh_channels = fetch_channel_list(api_caller)
    new_channels: list[ChannelEvent] = []
    updated_channels: list[ChannelEvent] = []
    for channel in fresh_channels:
        existing = existing_channel_by_id.get(channel.channel_id)
        if existing is None:
            new_channels.append(channel)
        elif existing.raw != channel.raw:
            updated_channels.append(channel)
        else:
            pass

    save_channel_events(settings.output_dir, "created", new_channels)
    # Created events also go to updated (a create is logically an update from nothing)
    all_changed_channels = list(new_channels) + list(updated_channels)
    save_channel_events(settings.output_dir, "updated", all_changed_channels)
    if new_channels:
        logger.info("Saved %d new channels", len(new_channels))
    if updated_channels:
        logger.info("Saved %d updated channels", len(updated_channels))

    for event in fresh_channels:
        channel_id_by_name[event.channel_name] = event.channel_id

    # Fetch users and split into created vs updated
    fresh_users = fetch_user_list(api_caller)
    new_users = [u for u in fresh_users if u.user_id not in existing_user_ids]
    # Users that already exist but may have changed -- we currently only track new ones
    save_user_events(settings.output_dir, "created", new_users)
    save_user_events(settings.output_dir, "updated", new_users)
    if new_users:
        logger.info("Saved %d new users", len(new_users))

    # Export messages for each configured channel
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


def _export_single_channel(
    channel_config: ChannelConfig,
    fresh_channels: list[ChannelEvent],
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

    oldest_datetime = channel_config.oldest or settings.default_oldest
    oldest_ts = _datetime_to_slack_timestamp(oldest_datetime)

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
        is_inclusive=existing_state is None or existing_state.latest_message_timestamp is None,
        api_caller=api_caller,
    )

    # All fetched messages go to "created" stream (new messages only)
    new_messages = [m for m in all_fetched if (m.channel_id, m.message_ts) not in known_message_keys]

    if new_messages:
        save_message_events(settings.output_dir, "created", new_messages)
        save_message_events(settings.output_dir, "updated", new_messages)
        logger.info("  Saved %d new messages from channel %s", len(new_messages), channel_config.name)
    else:
        logger.info("  No new messages in channel %s", channel_config.name)


def _fetch_all_messages_for_channel(
    channel_id: SlackChannelId,
    channel_name: SlackChannelName,
    oldest_ts: SlackMessageTimestamp,
    is_inclusive: bool,
    api_caller: SlackApiCaller,
) -> list[MessageEvent]:
    """Fetch all messages from a channel newer than oldest_ts, handling pagination."""
    all_messages: list[MessageEvent] = []
    cursor: str | None = None

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
            event = MessageEvent(
                timestamp=make_iso_timestamp(),
                type=EventType("message_fetched"),
                event_id=make_event_id(),
                source=_MESSAGE_SOURCE,
                channel_id=channel_id,
                channel_name=channel_name,
                message_ts=SlackMessageTimestamp(ts),
                raw=message_raw,
            )
            all_messages.append(event)

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
