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
from imbue.slack_exporter.data_types import ReplyEvent
from imbue.slack_exporter.data_types import SlackApiCaller
from imbue.slack_exporter.data_types import make_event_id
from imbue.slack_exporter.data_types import make_iso_timestamp
from imbue.slack_exporter.latchkey import fetch_paginated
from imbue.slack_exporter.primitives import SlackChannelId
from imbue.slack_exporter.primitives import SlackChannelName
from imbue.slack_exporter.primitives import SlackMessageTimestamp
from imbue.slack_exporter.store import StreamType
from imbue.slack_exporter.store import load_existing_channels
from imbue.slack_exporter.store import load_existing_message_state
from imbue.slack_exporter.store import load_existing_reply_keys
from imbue.slack_exporter.store import load_existing_user_ids
from imbue.slack_exporter.store import save_channel_events
from imbue.slack_exporter.store import save_message_events
from imbue.slack_exporter.store import save_reply_events
from imbue.slack_exporter.store import save_user_events

logger = logging.getLogger(__name__)

_MESSAGE_SOURCE = EventSource("messages")
_REPLY_SOURCE = EventSource("replies")


def run_export(settings: ExporterSettings, api_caller: SlackApiCaller) -> None:
    """Run the full export process: load state, resolve channels, fetch new messages, save."""
    existing_channel_by_id = load_existing_channels(settings.output_dir)
    state_by_channel_id, known_message_keys = load_existing_message_state(settings.output_dir)
    existing_user_ids = load_existing_user_ids(settings.output_dir)
    known_reply_keys = load_existing_reply_keys(settings.output_dir)

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

    save_channel_events(settings.output_dir, StreamType.CREATED, new_channels)
    all_changed_channels = list(new_channels) + list(updated_channels)
    save_channel_events(settings.output_dir, StreamType.UPDATED, all_changed_channels)
    if new_channels:
        logger.info("Saved %d new channels", len(new_channels))
    if updated_channels:
        logger.info("Saved %d updated channels", len(updated_channels))

    for event in fresh_channels:
        channel_id_by_name[event.channel_name] = event.channel_id

    # Fetch users (only save newly seen users for now)
    fresh_users = fetch_user_list(api_caller)
    new_users = [u for u in fresh_users if u.user_id not in existing_user_ids]
    save_user_events(settings.output_dir, StreamType.CREATED, new_users)
    save_user_events(settings.output_dir, StreamType.UPDATED, new_users)
    if new_users:
        logger.info("Saved %d new users", len(new_users))

    # Fetch messages and replies for all configured channels
    for channel_config in settings.channels:
        channel_id = resolve_channel_id(channel_config.name, fresh_channels, channel_id_by_name)
        _export_single_channel(
            channel_config=channel_config,
            channel_id=channel_id,
            state_by_channel_id=state_by_channel_id,
            known_message_keys=known_message_keys,
            known_reply_keys=known_reply_keys,
            settings=settings,
            api_caller=api_caller,
        )


def _export_single_channel(
    channel_config: ChannelConfig,
    channel_id: SlackChannelId,
    state_by_channel_id: dict[SlackChannelId, ChannelExportState],
    known_message_keys: set[tuple[SlackChannelId, SlackMessageTimestamp]],
    known_reply_keys: set[tuple[SlackChannelId, SlackMessageTimestamp, SlackMessageTimestamp]],
    settings: ExporterSettings,
    api_caller: SlackApiCaller,
) -> None:
    """Export messages and replies from a single channel."""
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

    new_messages = [m for m in all_fetched if (m.channel_id, m.message_ts) not in known_message_keys]

    if new_messages:
        save_message_events(settings.output_dir, StreamType.CREATED, new_messages)
        save_message_events(settings.output_dir, StreamType.UPDATED, new_messages)
        logger.info("  Saved %d new messages from channel %s", len(new_messages), channel_config.name)
    else:
        logger.info("  No new messages in channel %s", channel_config.name)

    # Fetch replies for threaded messages
    # conversations.history includes reply_count and latest_reply on parent messages,
    # so we can efficiently skip threads that haven't changed
    _export_replies_for_channel(
        channel_id=channel_id,
        channel_name=channel_config.name,
        all_message_events=all_fetched,
        known_reply_keys=known_reply_keys,
        settings=settings,
        api_caller=api_caller,
    )


def _export_replies_for_channel(
    channel_id: SlackChannelId,
    channel_name: SlackChannelName,
    all_message_events: list[MessageEvent],
    known_reply_keys: set[tuple[SlackChannelId, SlackMessageTimestamp, SlackMessageTimestamp]],
    settings: ExporterSettings,
    api_caller: SlackApiCaller,
) -> None:
    """Fetch replies for all threaded messages in a channel."""
    # Identify parent messages that have threads (reply_count > 0)
    thread_parents = [m for m in all_message_events if m.raw.get("reply_count", 0) > 0]

    if not thread_parents:
        return

    logger.info("  Found %d threads to check for replies", len(thread_parents))
    total_new_replies = 0

    for parent in thread_parents:
        thread_ts = parent.message_ts

        replies = _fetch_all_replies_for_thread(
            channel_id=channel_id,
            channel_name=channel_name,
            thread_ts=thread_ts,
            api_caller=api_caller,
        )

        # Filter to only replies we haven't seen (exclude the parent message itself)
        new_replies = [
            r
            for r in replies
            if r.reply_ts != thread_ts and (channel_id, thread_ts, r.reply_ts) not in known_reply_keys
        ]

        if new_replies:
            save_reply_events(settings.output_dir, StreamType.CREATED, new_replies)
            save_reply_events(settings.output_dir, StreamType.UPDATED, new_replies)
            total_new_replies += len(new_replies)
            # Track the newly saved keys to avoid duplicates within this run
            for reply in new_replies:
                known_reply_keys.add((channel_id, thread_ts, reply.reply_ts))

    if total_new_replies > 0:
        logger.info("  Saved %d new replies from channel %s", total_new_replies, channel_name)


def _fetch_all_replies_for_thread(
    channel_id: SlackChannelId,
    channel_name: SlackChannelName,
    thread_ts: SlackMessageTimestamp,
    api_caller: SlackApiCaller,
) -> list[ReplyEvent]:
    """Fetch all replies for a specific thread, handling pagination."""
    raw_replies = fetch_paginated(
        api_caller=api_caller,
        method="conversations.replies",
        base_params={"channel": channel_id, "ts": thread_ts, "limit": "200"},
        response_key="messages",
    )
    replies: list[ReplyEvent] = []
    for reply_raw in raw_replies:
        ts = reply_raw.get("ts", "")
        if not ts:
            continue
        replies.append(
            ReplyEvent(
                timestamp=make_iso_timestamp(),
                type=EventType("reply_fetched"),
                event_id=make_event_id(),
                source=_REPLY_SOURCE,
                channel_id=channel_id,
                channel_name=channel_name,
                thread_ts=thread_ts,
                reply_ts=SlackMessageTimestamp(ts),
                raw=reply_raw,
            )
        )
    return replies


def _fetch_all_messages_for_channel(
    channel_id: SlackChannelId,
    channel_name: SlackChannelName,
    oldest_ts: SlackMessageTimestamp,
    is_inclusive: bool,
    api_caller: SlackApiCaller,
) -> list[MessageEvent]:
    """Fetch all messages from a channel newer than oldest_ts, handling pagination."""
    raw_messages = fetch_paginated(
        api_caller=api_caller,
        method="conversations.history",
        base_params={
            "channel": channel_id,
            "oldest": oldest_ts,
            "inclusive": "true" if is_inclusive else "false",
            "include_all_metadata": "true",
            "limit": "200",
        },
        response_key="messages",
    )
    messages: list[MessageEvent] = []
    for message_raw in raw_messages:
        ts = message_raw.get("ts", "")
        if not ts:
            continue
        messages.append(
            MessageEvent(
                timestamp=make_iso_timestamp(),
                type=EventType("message_fetched"),
                event_id=make_event_id(),
                source=_MESSAGE_SOURCE,
                channel_id=channel_id,
                channel_name=channel_name,
                message_ts=SlackMessageTimestamp(ts),
                raw=message_raw,
            )
        )
    return messages


def _datetime_to_slack_timestamp(dt: datetime) -> SlackMessageTimestamp:
    """Convert a datetime to a Slack-style timestamp string."""
    return SlackMessageTimestamp(f"{dt.timestamp():.6f}")
