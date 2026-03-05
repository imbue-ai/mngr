import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from imbue.slack_exporter.data_types import ChannelExportState
from imbue.slack_exporter.data_types import StoredChannelInfo
from imbue.slack_exporter.data_types import StoredMessage
from imbue.slack_exporter.data_types import StoredUser
from imbue.slack_exporter.primitives import SlackChannelId
from imbue.slack_exporter.primitives import SlackMessageTimestamp
from imbue.slack_exporter.primitives import SlackUserId

logger = logging.getLogger(__name__)


def _events_path(output_dir: Path, data_type: str) -> Path:
    return output_dir / data_type / "events.jsonl"


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


def _append_records(file_path: Path, records: Sequence[BaseModel]) -> None:
    if not records:
        return
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(file_path, "a") as f:
        for record in records:
            f.write(record.model_dump_json() + "\n")
    logger.info("Appended %d records to %s", len(records), file_path)


def load_existing_channels(output_dir: Path) -> dict[SlackChannelId, StoredChannelInfo]:
    """Load existing channel records, keeping only the latest per channel_id."""
    channel_by_id: dict[SlackChannelId, StoredChannelInfo] = {}
    for record in _load_jsonl_records(_events_path(output_dir, "channels")):
        info = StoredChannelInfo.model_validate(record)
        channel_by_id[info.channel_id] = info
    logger.info("Loaded %d channels from store", len(channel_by_id))
    return channel_by_id


def load_existing_message_state(
    output_dir: Path,
) -> tuple[dict[SlackChannelId, ChannelExportState], set[tuple[SlackChannelId, SlackMessageTimestamp]]]:
    """Load existing messages to derive per-channel export state and the set of known message keys."""
    state_by_channel_id: dict[SlackChannelId, ChannelExportState] = {}
    known_message_keys: set[tuple[SlackChannelId, SlackMessageTimestamp]] = set()

    for record in _load_jsonl_records(_events_path(output_dir, "messages")):
        msg = StoredMessage.model_validate(record)
        known_message_keys.add((msg.channel_id, msg.timestamp))

        existing = state_by_channel_id.get(msg.channel_id)
        is_newer = (
            existing is None
            or existing.latest_message_timestamp is None
            or msg.timestamp > existing.latest_message_timestamp
        )
        if is_newer:
            state_by_channel_id[msg.channel_id] = ChannelExportState(
                channel_id=msg.channel_id,
                channel_name=msg.channel_name,
                latest_message_timestamp=msg.timestamp,
            )

    logger.info("Loaded %d known messages from store", len(known_message_keys))
    return state_by_channel_id, known_message_keys


def load_existing_user_ids(output_dir: Path) -> set[SlackUserId]:
    """Load the set of user IDs already stored."""
    user_ids: set[SlackUserId] = set()
    for record in _load_jsonl_records(_events_path(output_dir, "users")):
        user = StoredUser.model_validate(record)
        user_ids.add(user.user_id)
    logger.info("Loaded %d known users from store", len(user_ids))
    return user_ids


def save_channels(output_dir: Path, records: Sequence[StoredChannelInfo]) -> None:
    """Append channel info records to channels/events.jsonl."""
    _append_records(_events_path(output_dir, "channels"), records)


def save_messages(output_dir: Path, records: Sequence[StoredMessage]) -> None:
    """Append message records to messages/events.jsonl."""
    _append_records(_events_path(output_dir, "messages"), records)


def save_users(output_dir: Path, records: Sequence[StoredUser]) -> None:
    """Append user records to users/events.jsonl."""
    _append_records(_events_path(output_dir, "users"), records)
