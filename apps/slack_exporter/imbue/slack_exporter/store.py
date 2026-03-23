import json
import logging
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from imbue.imbue_common.event_envelope import EventEnvelope
from imbue.slack_exporter.data_types import ChannelEvent
from imbue.slack_exporter.data_types import ChannelExportState
from imbue.slack_exporter.data_types import MessageEvent
from imbue.slack_exporter.data_types import ReactionEvent
from imbue.slack_exporter.data_types import RelevantThreadEvent
from imbue.slack_exporter.data_types import ReplyEvent
from imbue.slack_exporter.data_types import SelfIdentityEvent
from imbue.slack_exporter.data_types import UnreadMarkerEvent
from imbue.slack_exporter.data_types import UserEvent
from imbue.slack_exporter.primitives import SlackChannelId
from imbue.slack_exporter.primitives import SlackMessageTimestamp
from imbue.slack_exporter.primitives import SlackUserId

logger = logging.getLogger(__name__)


class DataType(StrEnum):
    """The category of Slack data being stored. Values are lowercase directory names."""

    CHANNEL = "channel"
    MESSAGE = "message"
    REACTION = "reaction"
    RELEVANT_THREAD_REPLY = "relevant_thread_reply"
    RELEVANT_THREAD = "relevant_thread"
    REPLY = "reply"
    SELF_IDENTITY = "self_identity"
    UNREAD_MARKER = "unread_marker"
    USER = "user"


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
        for record in _load_jsonl_records(_events_path(output_dir, DataType.CHANNEL, stream)):
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
        for record in _load_jsonl_records(_events_path(output_dir, DataType.MESSAGE, stream)):
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
        for record in _load_jsonl_records(_events_path(output_dir, DataType.USER, stream)):
            event = UserEvent.model_validate(record)
            user_by_id[event.user_id] = event
    logger.info("Loaded %d users from store", len(user_by_id))
    return user_by_id


def save_channel_events(output_dir: Path, stream: StreamType, events: Sequence[ChannelEvent]) -> None:
    _append_events(_events_path(output_dir, DataType.CHANNEL, stream), events)


def save_message_events(output_dir: Path, stream: StreamType, events: Sequence[MessageEvent]) -> None:
    _append_events(_events_path(output_dir, DataType.MESSAGE, stream), events)


def save_reply_events(output_dir: Path, stream: StreamType, events: Sequence[ReplyEvent]) -> None:
    _append_events(_events_path(output_dir, DataType.REPLY, stream), events)


def save_user_events(output_dir: Path, stream: StreamType, events: Sequence[UserEvent]) -> None:
    _append_events(_events_path(output_dir, DataType.USER, stream), events)


def load_existing_reply_keys(
    output_dir: Path,
) -> set[tuple[SlackChannelId, SlackMessageTimestamp, SlackMessageTimestamp]]:
    """Load the set of known reply keys (channel_id, thread_ts, reply_ts) from both streams."""
    known_keys: set[tuple[SlackChannelId, SlackMessageTimestamp, SlackMessageTimestamp]] = set()
    for stream in StreamType:
        for record in _load_jsonl_records(_events_path(output_dir, DataType.REPLY, stream)):
            event = ReplyEvent.model_validate(record)
            known_keys.add((event.channel_id, event.thread_ts, event.reply_ts))
    logger.info("Loaded %d known replies from store", len(known_keys))
    return known_keys


def load_existing_relevant_thread_reply_keys(
    output_dir: Path,
) -> set[tuple[SlackChannelId, SlackMessageTimestamp, SlackMessageTimestamp]]:
    """Load known relevant thread reply keys (channel_id, thread_ts, reply_ts) from both streams."""
    known_keys: set[tuple[SlackChannelId, SlackMessageTimestamp, SlackMessageTimestamp]] = set()
    for stream in StreamType:
        for record in _load_jsonl_records(_events_path(output_dir, DataType.RELEVANT_THREAD_REPLY, stream)):
            event = ReplyEvent.model_validate(record)
            known_keys.add((event.channel_id, event.thread_ts, event.reply_ts))
    logger.info("Loaded %d known relevant thread replies from store", len(known_keys))
    return known_keys


def save_relevant_thread_reply_events(output_dir: Path, stream: StreamType, events: Sequence[ReplyEvent]) -> None:
    _append_events(_events_path(output_dir, DataType.RELEVANT_THREAD_REPLY, stream), events)


def load_existing_self_identity(output_dir: Path) -> dict[str, SelfIdentityEvent]:
    """Load existing self-identity events, keeping the latest per user_id."""
    identity_by_id: dict[str, SelfIdentityEvent] = {}
    for stream in StreamType:
        for record in _load_jsonl_records(_events_path(output_dir, DataType.SELF_IDENTITY, stream)):
            event = SelfIdentityEvent.model_validate(record)
            identity_by_id[event.user_id] = event
    logger.info("Loaded %d self-identity events from store", len(identity_by_id))
    return identity_by_id


def save_self_identity_events(output_dir: Path, stream: StreamType, events: Sequence[SelfIdentityEvent]) -> None:
    _append_events(_events_path(output_dir, DataType.SELF_IDENTITY, stream), events)


def load_existing_unread_markers(output_dir: Path) -> dict[str, UnreadMarkerEvent]:
    """Load existing unread marker events, keeping the latest per channel_id."""
    marker_by_channel: dict[str, UnreadMarkerEvent] = {}
    for stream in StreamType:
        for record in _load_jsonl_records(_events_path(output_dir, DataType.UNREAD_MARKER, stream)):
            event = UnreadMarkerEvent.model_validate(record)
            marker_by_channel[event.channel_id] = event
    logger.info("Loaded %d unread marker events from store", len(marker_by_channel))
    return marker_by_channel


def save_unread_marker_events(output_dir: Path, stream: StreamType, events: Sequence[UnreadMarkerEvent]) -> None:
    _append_events(_events_path(output_dir, DataType.UNREAD_MARKER, stream), events)


def load_existing_reactions(output_dir: Path) -> dict[str, ReactionEvent]:
    """Load existing reaction events, keeping the latest per channel_id:message_ts key."""
    reaction_by_key: dict[str, ReactionEvent] = {}
    for stream in StreamType:
        for record in _load_jsonl_records(_events_path(output_dir, DataType.REACTION, stream)):
            event = ReactionEvent.model_validate(record)
            reaction_by_key[f"{event.channel_id}:{event.message_ts}"] = event
    logger.info("Loaded %d reaction events from store", len(reaction_by_key))
    return reaction_by_key


def save_reaction_events(output_dir: Path, stream: StreamType, events: Sequence[ReactionEvent]) -> None:
    _append_events(_events_path(output_dir, DataType.REACTION, stream), events)


def load_existing_relevant_threads(output_dir: Path) -> dict[str, RelevantThreadEvent]:
    """Load existing relevant thread events, keeping the latest per channel_id:thread_ts key."""
    by_key: dict[str, RelevantThreadEvent] = {}
    for stream in StreamType:
        for record in _load_jsonl_records(_events_path(output_dir, DataType.RELEVANT_THREAD, stream)):
            event = RelevantThreadEvent.model_validate(record)
            by_key[f"{event.channel_id}:{event.thread_ts}"] = event
    logger.info("Loaded %d relevant thread events from store", len(by_key))
    return by_key


def save_relevant_thread_events(output_dir: Path, stream: StreamType, events: Sequence[RelevantThreadEvent]) -> None:
    _append_events(_events_path(output_dir, DataType.RELEVANT_THREAD, stream), events)


def _channel_export_metadata_path(output_dir: Path) -> Path:
    return output_dir / ".channel_export_metadata.json"


def load_channel_export_metadata(output_dir: Path) -> dict[SlackChannelId, SlackMessageTimestamp]:
    """Load the per-channel searched-oldest timestamps.

    Returns a mapping from channel_id to the oldest Slack timestamp we have
    already searched from for that channel.
    """
    path = _channel_export_metadata_path(output_dir)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError:
        logger.warning("Malformed channel export metadata at %s, treating as empty", path)
        return {}
    return {SlackChannelId(k): SlackMessageTimestamp(v) for k, v in raw.items()}


def save_channel_searched_oldest(
    output_dir: Path,
    channel_id: SlackChannelId,
    searched_oldest_ts: SlackMessageTimestamp,
) -> None:
    """Persist the oldest timestamp we have searched from for a channel.

    Only updates the value if the new timestamp is older (earlier) than the
    existing one, so repeated runs with the same --since are no-ops.
    """
    metadata = load_channel_export_metadata(output_dir)
    existing = metadata.get(channel_id)
    if existing is not None and existing <= searched_oldest_ts:
        return
    metadata[channel_id] = searched_oldest_ts
    path = _channel_export_metadata_path(output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({str(k): str(v) for k, v in metadata.items()}))


def _fetch_metadata_path(output_dir: Path) -> Path:
    return output_dir / ".fetch_metadata.json"


def load_fetch_metadata(output_dir: Path) -> dict[str, datetime]:
    """Load the per-data-type last-fetch timestamps."""
    path = _fetch_metadata_path(output_dir)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError:
        logger.warning("Malformed fetch metadata at %s, treating as empty", path)
        return {}
    result: dict[str, datetime] = {}
    for key, value in raw.items():
        try:
            result[key] = datetime.fromisoformat(value)
        except (ValueError, TypeError):
            logger.warning("Skipping invalid timestamp for %s in fetch metadata", key)
    return result


def save_fetch_timestamp(output_dir: Path, data_type: str, timestamp: datetime) -> None:
    """Update the last-fetch timestamp for a specific data type."""
    metadata = load_fetch_metadata(output_dir)
    metadata[data_type] = timestamp
    path = _fetch_metadata_path(output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = {k: v.isoformat() for k, v in metadata.items()}
    path.write_text(json.dumps(raw))
