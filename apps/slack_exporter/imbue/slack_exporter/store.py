import json
import logging
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any

from imbue.imbue_common.event_envelope import EventEnvelope
from imbue.slack_exporter.data_types import ChannelEvent
from imbue.slack_exporter.data_types import ChannelExportState
from imbue.slack_exporter.data_types import MessageEvent
from imbue.slack_exporter.data_types import ReplyEvent
from imbue.slack_exporter.data_types import UserEvent
from imbue.slack_exporter.primitives import SlackChannelId
from imbue.slack_exporter.primitives import SlackMessageTimestamp
from imbue.slack_exporter.primitives import SlackUserId

logger = logging.getLogger(__name__)


class DataType(StrEnum):
    """The category of Slack data being stored. Values are lowercase directory names."""

    CHANNELS = "channels"
    MESSAGES = "messages"
    REPLIES = "replies"
    USERS = "users"


class StreamType(StrEnum):
    """The event stream within a data type. Values are lowercase directory names."""

    CREATED = "created"
    UPDATED = "updated"


def _events_path(output_dir: Path, data_type: DataType, stream: StreamType) -> Path:
    return output_dir / data_type / stream / "events.jsonl"


def _load_jsonl_records(file_path: Path) -> list[dict[str, Any]]:
    if not file_path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in file_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("Skipping malformed JSON in %s", file_path)
    return records


def _append_events(file_path: Path, events: Sequence[EventEnvelope]) -> None:
    if not events:
        return
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(file_path, "a") as f:
        for event in events:
            f.write(event.model_dump_json() + "\n")
    logger.info("Appended %d events to %s", len(events), file_path)


def load_existing_channels(
    output_dir: Path,
) -> dict[SlackChannelId, ChannelEvent]:
    """Load existing channel events from both created and updated streams.

    Returns the latest event per channel_id (updated events override created).
    """
    channel_by_id: dict[SlackChannelId, ChannelEvent] = {}

    # Load created first, then updated (updated overrides)
    for stream in StreamType:
        for record in _load_jsonl_records(_events_path(output_dir, DataType.CHANNELS, stream)):
            event = ChannelEvent.model_validate(record)
            channel_by_id[event.channel_id] = event

    logger.info("Loaded %d channels from store", len(channel_by_id))
    return channel_by_id


def load_existing_message_state(
    output_dir: Path,
) -> tuple[dict[SlackChannelId, ChannelExportState], set[tuple[SlackChannelId, SlackMessageTimestamp]]]:
    """Load existing message events to derive per-channel state and known message keys."""
    state_by_channel_id: dict[SlackChannelId, ChannelExportState] = {}
    known_message_keys: set[tuple[SlackChannelId, SlackMessageTimestamp]] = set()

    for stream in StreamType:
        for record in _load_jsonl_records(_events_path(output_dir, DataType.MESSAGES, stream)):
            event = MessageEvent.model_validate(record)
            known_message_keys.add((event.channel_id, event.message_ts))

            existing = state_by_channel_id.get(event.channel_id)
            is_newer = (
                existing is None
                or existing.latest_message_timestamp is None
                or event.message_ts > existing.latest_message_timestamp
            )
            if is_newer:
                state_by_channel_id[event.channel_id] = ChannelExportState(
                    channel_id=event.channel_id,
                    channel_name=event.channel_name,
                    latest_message_timestamp=event.message_ts,
                )

    logger.info("Loaded %d known messages from store", len(known_message_keys))
    return state_by_channel_id, known_message_keys


def load_existing_users(output_dir: Path) -> dict[SlackUserId, UserEvent]:
    """Load existing user events, keeping the latest per user_id (updated overrides created)."""
    user_by_id: dict[SlackUserId, UserEvent] = {}
    for stream in StreamType:
        for record in _load_jsonl_records(_events_path(output_dir, DataType.USERS, stream)):
            event = UserEvent.model_validate(record)
            user_by_id[event.user_id] = event
    logger.info("Loaded %d users from store", len(user_by_id))
    return user_by_id


def save_channel_events(output_dir: Path, stream: StreamType, events: Sequence[ChannelEvent]) -> None:
    _append_events(_events_path(output_dir, DataType.CHANNELS, stream), events)


def save_message_events(output_dir: Path, stream: StreamType, events: Sequence[MessageEvent]) -> None:
    _append_events(_events_path(output_dir, DataType.MESSAGES, stream), events)


def save_reply_events(output_dir: Path, stream: StreamType, events: Sequence[ReplyEvent]) -> None:
    _append_events(_events_path(output_dir, DataType.REPLIES, stream), events)


def save_user_events(output_dir: Path, stream: StreamType, events: Sequence[UserEvent]) -> None:
    _append_events(_events_path(output_dir, DataType.USERS, stream), events)


def load_existing_reply_keys(
    output_dir: Path,
) -> set[tuple[SlackChannelId, SlackMessageTimestamp, SlackMessageTimestamp]]:
    """Load the set of known reply keys (channel_id, thread_ts, reply_ts) from both streams."""
    known_keys: set[tuple[SlackChannelId, SlackMessageTimestamp, SlackMessageTimestamp]] = set()
    for stream in StreamType:
        for record in _load_jsonl_records(_events_path(output_dir, DataType.REPLIES, stream)):
            event = ReplyEvent.model_validate(record)
            known_keys.add((event.channel_id, event.thread_ts, event.reply_ts))
    logger.info("Loaded %d known replies from store", len(known_keys))
    return known_keys
