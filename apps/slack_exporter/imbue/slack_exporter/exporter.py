import logging
from collections.abc import Callable
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import TypeVar

from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import EventType
from imbue.slack_exporter.channels import extract_unread_markers
from imbue.slack_exporter.channels import fetch_channel_list
from imbue.slack_exporter.channels import fetch_self_identity
from imbue.slack_exporter.channels import fetch_user_list
from imbue.slack_exporter.channels import fetch_user_reactions
from imbue.slack_exporter.channels import resolve_channel_id
from imbue.slack_exporter.data_types import ChannelConfig
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
from imbue.slack_exporter.store import DataType
from imbue.slack_exporter.store import StreamType
from imbue.slack_exporter.store import derive_reaction_item_key
from imbue.slack_exporter.store import load_channel_export_metadata
from imbue.slack_exporter.store import load_existing_channels
from imbue.slack_exporter.store import load_existing_message_state
from imbue.slack_exporter.store import load_existing_reactions
from imbue.slack_exporter.store import load_existing_reply_keys
from imbue.slack_exporter.store import load_existing_self_identity
from imbue.slack_exporter.store import load_existing_unread_markers
from imbue.slack_exporter.store import load_existing_users
from imbue.slack_exporter.store import load_fetch_metadata
from imbue.slack_exporter.store import save_channel_events
from imbue.slack_exporter.store import save_channel_searched_oldest
from imbue.slack_exporter.store import save_fetch_timestamp
from imbue.slack_exporter.store import save_message_events
from imbue.slack_exporter.store import save_reaction_events
from imbue.slack_exporter.store import save_reply_events
from imbue.slack_exporter.store import save_self_identity_events
from imbue.slack_exporter.store import save_unread_marker_events
from imbue.slack_exporter.store import save_user_events

logger = logging.getLogger(__name__)

_MESSAGE_SOURCE = EventSource("messages")
_REPLY_SOURCE = EventSource("replies")

_T = TypeVar("_T")


def _diff_and_save(
    fresh_items: Sequence[_T],
    existing_by_key: dict[str, _T],
    get_key: Callable[[_T], str],
    get_raw: Callable[[_T], dict[str, Any]],
    save_fn: Callable[[Path, StreamType, Sequence[_T]], None],
    output_dir: Path,
    entity_name: str,
) -> None:
    """Diff fresh items against existing, save new to CREATED+UPDATED and changed to UPDATED."""
    new_items: list[_T] = []
    updated_items: list[_T] = []
    for item in fresh_items:
        key = get_key(item)
        existing = existing_by_key.get(key)
        if existing is None:
            new_items.append(item)
        elif get_raw(existing) != get_raw(item):
            updated_items.append(item)
        else:
            pass

    save_fn(output_dir, StreamType.CREATED, new_items)
    all_changed = list(new_items) + list(updated_items)
    save_fn(output_dir, StreamType.UPDATED, all_changed)
    if new_items:
        logger.info("Saved %d new %s", len(new_items), entity_name)
    if updated_items:
        logger.info("Saved %d updated %s", len(updated_items), entity_name)


def _is_cache_fresh(
    fetch_metadata: dict[str, datetime],
    data_type: str,
    now: datetime,
    settings: ExporterSettings,
) -> bool:
    """Check if cached data for a data type is still within the TTL."""
    if settings.refresh:
        return False
    last_fetched = fetch_metadata.get(data_type)
    if last_fetched is None:
        return False
    return (now - last_fetched).total_seconds() < settings.cache_ttl_seconds


def _build_latest_reply_by_thread(
    known_reply_keys: set[tuple[SlackChannelId, SlackMessageTimestamp, SlackMessageTimestamp]],
) -> dict[tuple[SlackChannelId, SlackMessageTimestamp], SlackMessageTimestamp]:
    """Build a mapping from (channel_id, thread_ts) to the latest reply_ts we have stored."""
    latest: dict[tuple[SlackChannelId, SlackMessageTimestamp], SlackMessageTimestamp] = {}
    for channel_id, thread_ts, reply_ts in known_reply_keys:
        key = (channel_id, thread_ts)
        if key not in latest or reply_ts > latest[key]:
            latest[key] = reply_ts
    return latest


def run_export(settings: ExporterSettings, api_caller: SlackApiCaller) -> None:
    """Run the full export process: load state, resolve channels, fetch new messages, save."""
    existing_channel_by_id = load_existing_channels(settings.output_dir)
    state_by_channel_id, known_message_keys = load_existing_message_state(settings.output_dir)
    existing_user_by_id = load_existing_users(settings.output_dir)
    known_reply_keys = load_existing_reply_keys(settings.output_dir)

    fetch_metadata = load_fetch_metadata(settings.output_dir)
    now = datetime.now(timezone.utc)

    # Export self identity
    existing_self_identity = load_existing_self_identity(settings.output_dir)
    if _is_cache_fresh(fetch_metadata, DataType.SELF_IDENTITY, now, settings) and existing_self_identity:
        self_identity = next(iter(existing_self_identity.values()))
        logger.info("Using cached self identity (user_id=%s)", self_identity.user_id)
    else:
        self_identity = fetch_self_identity(api_caller)
        _diff_and_save(
            fresh_items=[self_identity],
            existing_by_key=dict(existing_self_identity),
            get_key=lambda e: e.user_id,
            get_raw=lambda e: e.raw,
            save_fn=save_self_identity_events,
            output_dir=settings.output_dir,
            entity_name="self identity",
        )
        save_fetch_timestamp(settings.output_dir, DataType.SELF_IDENTITY, now)

    # Export channels (and unread markers, which come from the same API call)
    if _is_cache_fresh(fetch_metadata, DataType.CHANNELS, now, settings) and existing_channel_by_id:
        fresh_channels = list(existing_channel_by_id.values())
        logger.info("Using cached channel data (%d channels)", len(fresh_channels))
    else:
        fresh_channels = fetch_channel_list(api_caller, members_only=settings.members_only)
        _diff_and_save(
            fresh_items=fresh_channels,
            existing_by_key=dict(existing_channel_by_id),
            get_key=lambda ch: ch.channel_id,
            get_raw=lambda ch: ch.raw,
            save_fn=save_channel_events,
            output_dir=settings.output_dir,
            entity_name="channels",
        )

        # Unread markers are extracted from channel data (no extra API call)
        fresh_markers = extract_unread_markers(fresh_channels)
        existing_markers = load_existing_unread_markers(settings.output_dir)
        _diff_and_save(
            fresh_items=fresh_markers,
            existing_by_key=dict(existing_markers),
            get_key=lambda m: m.channel_id,
            get_raw=lambda m: m.raw,
            save_fn=save_unread_marker_events,
            output_dir=settings.output_dir,
            entity_name="unread markers",
        )
        save_fetch_timestamp(settings.output_dir, DataType.CHANNELS, now)

    channel_id_by_name: dict[SlackChannelName, SlackChannelId] = {
        event.channel_name: event.channel_id for event in existing_channel_by_id.values()
    }
    for event in fresh_channels:
        channel_id_by_name[event.channel_name] = event.channel_id

    # Export users
    if _is_cache_fresh(fetch_metadata, DataType.USERS, now, settings) and existing_user_by_id:
        logger.info("Using cached user data (%d users)", len(existing_user_by_id))
    else:
        fresh_users = fetch_user_list(api_caller)
        _diff_and_save(
            fresh_items=fresh_users,
            existing_by_key=dict(existing_user_by_id),
            get_key=lambda u: u.user_id,
            get_raw=lambda u: u.raw,
            save_fn=save_user_events,
            output_dir=settings.output_dir,
            entity_name="users",
        )
        save_fetch_timestamp(settings.output_dir, DataType.USERS, now)

    # Export messages and replies per channel
    latest_reply_by_thread = _build_latest_reply_by_thread(known_reply_keys)
    channel_export_metadata = load_channel_export_metadata(settings.output_dir)
    for channel_config in settings.channels:
        channel_id = resolve_channel_id(channel_config.name, fresh_channels, channel_id_by_name)
        _export_single_channel(
            channel_config=channel_config,
            channel_id=channel_id,
            state_by_channel_id=state_by_channel_id,
            known_message_keys=known_message_keys,
            known_reply_keys=known_reply_keys,
            latest_reply_by_thread=latest_reply_by_thread,
            channel_export_metadata=channel_export_metadata,
            settings=settings,
            api_caller=api_caller,
        )

    # Export reactions for the authenticated user
    if _is_cache_fresh(fetch_metadata, DataType.REACTIONS, now, settings):
        logger.info("Using cached reaction data")
    else:
        fresh_reactions = fetch_user_reactions(api_caller, self_identity.user_id)
        existing_reactions = load_existing_reactions(settings.output_dir)
        _diff_and_save(
            fresh_items=fresh_reactions,
            existing_by_key=dict(existing_reactions),
            get_key=lambda r: derive_reaction_item_key(r.raw),
            get_raw=lambda r: r.raw,
            save_fn=save_reaction_events,
            output_dir=settings.output_dir,
            entity_name="reaction items",
        )
        save_fetch_timestamp(settings.output_dir, DataType.REACTIONS, now)


def _export_single_channel(
    channel_config: ChannelConfig,
    channel_id: SlackChannelId,
    state_by_channel_id: dict[SlackChannelId, ChannelExportState],
    known_message_keys: set[tuple[SlackChannelId, SlackMessageTimestamp]],
    known_reply_keys: set[tuple[SlackChannelId, SlackMessageTimestamp, SlackMessageTimestamp]],
    latest_reply_by_thread: dict[tuple[SlackChannelId, SlackMessageTimestamp], SlackMessageTimestamp],
    channel_export_metadata: dict[SlackChannelId, SlackMessageTimestamp],
    settings: ExporterSettings,
    api_caller: SlackApiCaller,
) -> None:
    """Export messages and replies from a single channel."""
    logger.info("Exporting channel %s (ID: %s)", channel_config.name, channel_id)

    existing_state = state_by_channel_id.get(channel_id)

    oldest_datetime = channel_config.oldest or settings.default_oldest
    requested_oldest_ts = _datetime_to_slack_timestamp(oldest_datetime)

    # Forward fetch: get new messages since last export (or from requested oldest on first run)
    if existing_state and existing_state.latest_message_timestamp:
        forward_oldest = existing_state.latest_message_timestamp
        forward_inclusive = False
        logger.info("  Resuming from timestamp %s for channel %s", forward_oldest, channel_config.name)
    else:
        forward_oldest = requested_oldest_ts
        forward_inclusive = True

    all_fetched = _fetch_all_messages_for_channel(
        channel_id=channel_id,
        channel_name=channel_config.name,
        oldest_ts=forward_oldest,
        is_inclusive=forward_inclusive,
        api_caller=api_caller,
    )

    # Backfill: fetch older messages if --since goes further back than what we've already searched
    searched_oldest = channel_export_metadata.get(channel_id)
    if searched_oldest is not None and requested_oldest_ts < searched_oldest:
        logger.info(
            "  Backfilling from %s to %s for channel %s",
            requested_oldest_ts,
            searched_oldest,
            channel_config.name,
        )
        backfill_messages = _fetch_all_messages_for_channel(
            channel_id=channel_id,
            channel_name=channel_config.name,
            oldest_ts=requested_oldest_ts,
            is_inclusive=True,
            api_caller=api_caller,
            latest_ts=searched_oldest,
        )
        all_fetched = all_fetched + backfill_messages

    save_channel_searched_oldest(settings.output_dir, channel_id, requested_oldest_ts)

    new_messages = [m for m in all_fetched if (m.channel_id, m.message_ts) not in known_message_keys]
    if new_messages:
        save_message_events(settings.output_dir, StreamType.CREATED, new_messages)
        save_message_events(settings.output_dir, StreamType.UPDATED, new_messages)
        logger.info("  Saved %d new messages from channel %s", len(new_messages), channel_config.name)
    else:
        logger.info("  No new messages in channel %s", channel_config.name)

    _export_replies_for_channel(
        channel_id=channel_id,
        channel_name=channel_config.name,
        all_message_events=all_fetched,
        known_reply_keys=known_reply_keys,
        latest_reply_by_thread=latest_reply_by_thread,
        settings=settings,
        api_caller=api_caller,
    )


def _export_replies_for_channel(
    channel_id: SlackChannelId,
    channel_name: SlackChannelName,
    all_message_events: list[MessageEvent],
    known_reply_keys: set[tuple[SlackChannelId, SlackMessageTimestamp, SlackMessageTimestamp]],
    latest_reply_by_thread: dict[tuple[SlackChannelId, SlackMessageTimestamp], SlackMessageTimestamp],
    settings: ExporterSettings,
    api_caller: SlackApiCaller,
) -> None:
    """Fetch replies for all threaded messages in a channel.

    Uses the latest_reply field from the Slack API to skip threads whose replies
    have not changed since the last export.
    """
    thread_parents = [m for m in all_message_events if m.raw.get("reply_count", 0) > 0]
    if not thread_parents:
        return

    logger.info("  Found %d threads to check for replies", len(thread_parents))
    total_new_replies = 0
    skipped_threads = 0

    for parent in thread_parents:
        thread_ts = parent.message_ts
        thread_key = (channel_id, thread_ts)

        # Skip threads whose latest_reply hasn't changed since last export
        api_latest_reply = parent.raw.get("latest_reply")
        if api_latest_reply:
            stored_latest = latest_reply_by_thread.get(thread_key)
            if stored_latest is not None and stored_latest >= SlackMessageTimestamp(api_latest_reply):
                skipped_threads += 1
                continue

        replies = _fetch_all_replies_for_thread(
            channel_id=channel_id,
            channel_name=channel_name,
            thread_ts=thread_ts,
            api_caller=api_caller,
        )

        new_replies = [
            r
            for r in replies
            if r.reply_ts != thread_ts and (channel_id, thread_ts, r.reply_ts) not in known_reply_keys
        ]

        if new_replies:
            save_reply_events(settings.output_dir, StreamType.CREATED, new_replies)
            save_reply_events(settings.output_dir, StreamType.UPDATED, new_replies)
            total_new_replies += len(new_replies)
            for reply in new_replies:
                known_reply_keys.add((channel_id, thread_ts, reply.reply_ts))
                if thread_key not in latest_reply_by_thread or reply.reply_ts > latest_reply_by_thread[thread_key]:
                    latest_reply_by_thread[thread_key] = reply.reply_ts

    if skipped_threads > 0:
        logger.info("  Skipped %d threads with unchanged replies", skipped_threads)
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
    return [
        ReplyEvent(
            timestamp=make_iso_timestamp(),
            type=EventType("reply_fetched"),
            event_id=make_event_id(),
            source=_REPLY_SOURCE,
            channel_id=channel_id,
            channel_name=channel_name,
            thread_ts=thread_ts,
            reply_ts=SlackMessageTimestamp(raw["ts"]),
            raw=raw,
        )
        for raw in raw_replies
        if raw.get("ts")
    ]


def _fetch_all_messages_for_channel(
    channel_id: SlackChannelId,
    channel_name: SlackChannelName,
    oldest_ts: SlackMessageTimestamp,
    is_inclusive: bool,
    api_caller: SlackApiCaller,
    latest_ts: SlackMessageTimestamp | None = None,
) -> list[MessageEvent]:
    """Fetch all messages from a channel newer than oldest_ts, handling pagination.

    If latest_ts is provided, only messages older than latest_ts are returned (for backfill).
    """
    base_params: dict[str, str] = {
        "channel": channel_id,
        "oldest": oldest_ts,
        "inclusive": "true" if is_inclusive else "false",
        "include_all_metadata": "true",
        "limit": "200",
    }
    if latest_ts is not None:
        base_params["latest"] = latest_ts
    raw_messages = fetch_paginated(
        api_caller=api_caller,
        method="conversations.history",
        base_params=base_params,
        response_key="messages",
    )
    return [
        MessageEvent(
            timestamp=make_iso_timestamp(),
            type=EventType("message_fetched"),
            event_id=make_event_id(),
            source=_MESSAGE_SOURCE,
            channel_id=channel_id,
            channel_name=channel_name,
            message_ts=SlackMessageTimestamp(raw["ts"]),
            raw=raw,
        )
        for raw in raw_messages
        if raw.get("ts")
    ]


def _datetime_to_slack_timestamp(dt: datetime) -> SlackMessageTimestamp:
    """Convert a datetime to a Slack-style timestamp string."""
    return SlackMessageTimestamp(f"{dt.timestamp():.6f}")
