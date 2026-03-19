import json
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from imbue.slack_exporter.primitives import SlackChannelId
from imbue.slack_exporter.primitives import SlackChannelName
from imbue.slack_exporter.primitives import SlackMessageTimestamp
from imbue.slack_exporter.primitives import SlackUserId
from imbue.slack_exporter.store import StreamType
from imbue.slack_exporter.store import derive_reaction_item_key
from imbue.slack_exporter.store import load_channel_export_metadata
from imbue.slack_exporter.store import load_existing_channels
from imbue.slack_exporter.store import load_existing_message_state
from imbue.slack_exporter.store import load_existing_reactions
from imbue.slack_exporter.store import load_existing_self_identity
from imbue.slack_exporter.store import load_existing_unread_markers
from imbue.slack_exporter.store import load_existing_users
from imbue.slack_exporter.store import load_fetch_metadata
from imbue.slack_exporter.store import save_channel_events
from imbue.slack_exporter.store import save_channel_searched_oldest
from imbue.slack_exporter.store import save_fetch_timestamp
from imbue.slack_exporter.store import save_message_events
from imbue.slack_exporter.store import save_reaction_events
from imbue.slack_exporter.store import save_self_identity_events
from imbue.slack_exporter.store import save_unread_marker_events
from imbue.slack_exporter.store import save_user_events
from imbue.slack_exporter.testing import make_channel_event
from imbue.slack_exporter.testing import make_message_event
from imbue.slack_exporter.testing import make_reaction_item_event
from imbue.slack_exporter.testing import make_self_identity_event
from imbue.slack_exporter.testing import make_unread_marker_event
from imbue.slack_exporter.testing import make_user_event


def test_load_existing_channels_returns_empty_when_missing(temp_output_dir: Path) -> None:
    result = load_existing_channels(temp_output_dir)
    assert result == {}


def test_load_existing_channels_loads_from_created_stream(temp_output_dir: Path) -> None:
    event = make_channel_event("C123", "general")
    save_channel_events(temp_output_dir, StreamType.CREATED, [event])

    result = load_existing_channels(temp_output_dir)
    assert len(result) == 1
    assert result[SlackChannelId("C123")].channel_name == SlackChannelName("general")


def test_load_existing_channels_updated_overrides_created(temp_output_dir: Path) -> None:
    created = make_channel_event("C123", "general")
    updated = make_channel_event("C123", "general-renamed")
    save_channel_events(temp_output_dir, StreamType.CREATED, [created])
    save_channel_events(temp_output_dir, StreamType.UPDATED, [updated])

    result = load_existing_channels(temp_output_dir)
    assert result[SlackChannelId("C123")].channel_name == SlackChannelName("general-renamed")


def test_load_existing_message_state_returns_empty_when_missing(temp_output_dir: Path) -> None:
    state, keys = load_existing_message_state(temp_output_dir)
    assert state == {}
    assert keys == set()


def test_load_existing_message_state_tracks_latest_timestamp(temp_output_dir: Path) -> None:
    msg1 = make_message_event(ts="1700000000.000001")
    msg2 = make_message_event(ts="1700000000.000009")
    save_message_events(temp_output_dir, StreamType.CREATED, [msg1, msg2])

    state, keys = load_existing_message_state(temp_output_dir)

    assert SlackChannelId("C123") in state
    assert state[SlackChannelId("C123")].latest_message_timestamp == SlackMessageTimestamp("1700000000.000009")
    assert len(keys) == 2


def test_load_existing_users_returns_empty_when_missing(temp_output_dir: Path) -> None:
    result = load_existing_users(temp_output_dir)
    assert result == {}


def test_load_existing_users_returns_from_created_stream(temp_output_dir: Path) -> None:
    save_user_events(temp_output_dir, StreamType.CREATED, [make_user_event("U111"), make_user_event("U222")])
    result = load_existing_users(temp_output_dir)
    assert SlackUserId("U111") in result
    assert SlackUserId("U222") in result


def test_load_existing_users_updated_overrides_created(temp_output_dir: Path) -> None:
    created = make_user_event("U111")
    updated = make_user_event("U111")
    save_user_events(temp_output_dir, StreamType.CREATED, [created])
    save_user_events(temp_output_dir, StreamType.UPDATED, [updated])
    result = load_existing_users(temp_output_dir)
    assert len(result) == 1


def test_save_channel_events_creates_directory_structure(temp_output_dir: Path) -> None:
    save_channel_events(temp_output_dir, StreamType.CREATED, [make_channel_event()])
    expected_path = temp_output_dir / "channels" / "created" / "events.jsonl"
    assert expected_path.exists()
    parsed = json.loads(expected_path.read_text().strip())
    assert parsed["channel_id"] == "C123"
    assert "event_id" in parsed
    assert "timestamp" in parsed
    assert parsed["source"] == "channels"


def test_save_message_events_creates_directory_structure(temp_output_dir: Path) -> None:
    save_message_events(temp_output_dir, StreamType.CREATED, [make_message_event()])
    expected_path = temp_output_dir / "messages" / "created" / "events.jsonl"
    assert expected_path.exists()


def test_save_user_events_creates_directory_structure(temp_output_dir: Path) -> None:
    save_user_events(temp_output_dir, StreamType.CREATED, [make_user_event()])
    expected_path = temp_output_dir / "users" / "created" / "events.jsonl"
    assert expected_path.exists()


def test_save_appends_to_existing(temp_output_dir: Path) -> None:
    save_message_events(temp_output_dir, StreamType.CREATED, [make_message_event(ts="1700000000.000001")])
    save_message_events(temp_output_dir, StreamType.CREATED, [make_message_event(ts="1700000000.000002")])

    lines = (temp_output_dir / "messages" / "created" / "events.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2


def test_save_does_nothing_for_empty_list(temp_output_dir: Path) -> None:
    save_message_events(temp_output_dir, StreamType.CREATED, [])
    assert not (temp_output_dir / "messages" / "created" / "events.jsonl").exists()


def test_load_existing_self_identity_returns_empty_when_missing(temp_output_dir: Path) -> None:
    result = load_existing_self_identity(temp_output_dir)
    assert result == {}


def test_save_and_load_self_identity(temp_output_dir: Path) -> None:
    event = make_self_identity_event("U001", "alice")
    save_self_identity_events(temp_output_dir, StreamType.CREATED, [event])

    result = load_existing_self_identity(temp_output_dir)
    assert len(result) == 1
    assert "U001" in result
    assert result["U001"].user_name == "alice"


def test_self_identity_updated_overrides_created(temp_output_dir: Path) -> None:
    created = make_self_identity_event("U001", "alice")
    updated = make_self_identity_event("U001", "alice-renamed")
    save_self_identity_events(temp_output_dir, StreamType.CREATED, [created])
    save_self_identity_events(temp_output_dir, StreamType.UPDATED, [updated])

    result = load_existing_self_identity(temp_output_dir)
    assert result["U001"].user_name == "alice-renamed"


def test_save_self_identity_creates_directory_structure(temp_output_dir: Path) -> None:
    save_self_identity_events(temp_output_dir, StreamType.CREATED, [make_self_identity_event()])
    expected_path = temp_output_dir / "self_identity" / "created" / "events.jsonl"
    assert expected_path.exists()
    parsed = json.loads(expected_path.read_text().strip())
    assert parsed["user_id"] == "U123"
    assert parsed["source"] == "self_identity"


def test_load_existing_unread_markers_returns_empty_when_missing(temp_output_dir: Path) -> None:
    result = load_existing_unread_markers(temp_output_dir)
    assert result == {}


def test_save_and_load_unread_markers(temp_output_dir: Path) -> None:
    event = make_unread_marker_event("C123", "general", "1700000000.000001")
    save_unread_marker_events(temp_output_dir, StreamType.CREATED, [event])

    result = load_existing_unread_markers(temp_output_dir)
    assert len(result) == 1
    assert "C123" in result
    assert result["C123"].last_read_ts == "1700000000.000001"


def test_unread_markers_updated_overrides_created(temp_output_dir: Path) -> None:
    created = make_unread_marker_event("C123", "general", "1700000000.000001")
    updated = make_unread_marker_event("C123", "general", "1700000000.000099")
    save_unread_marker_events(temp_output_dir, StreamType.CREATED, [created])
    save_unread_marker_events(temp_output_dir, StreamType.UPDATED, [updated])

    result = load_existing_unread_markers(temp_output_dir)
    assert result["C123"].last_read_ts == "1700000000.000099"


def test_save_unread_marker_events_creates_directory_structure(temp_output_dir: Path) -> None:
    save_unread_marker_events(temp_output_dir, StreamType.CREATED, [make_unread_marker_event()])
    expected_path = temp_output_dir / "unread_markers" / "created" / "events.jsonl"
    assert expected_path.exists()
    parsed = json.loads(expected_path.read_text().strip())
    assert parsed["source"] == "unread_markers"


def test_derive_reaction_item_key_message() -> None:
    raw: dict[str, Any] = {"type": "message", "channel": "C123", "message": {"ts": "1.0"}}
    assert derive_reaction_item_key(raw) == "message:C123:1.0"


def test_derive_reaction_item_key_file() -> None:
    raw: dict[str, Any] = {"type": "file", "file": {"id": "F123"}}
    assert derive_reaction_item_key(raw) == "file:F123"


def test_derive_reaction_item_key_file_comment() -> None:
    raw: dict[str, Any] = {"type": "file_comment", "comment": {"id": "Fc123"}}
    assert derive_reaction_item_key(raw) == "file_comment:Fc123"


def test_derive_reaction_item_key_unknown_type() -> None:
    raw: dict[str, Any] = {"type": "unknown_type", "data": "something"}
    key = derive_reaction_item_key(raw)
    assert key.startswith("other:")


def test_load_existing_reactions_returns_empty_when_missing(temp_output_dir: Path) -> None:
    result = load_existing_reactions(temp_output_dir)
    assert result == {}


def test_save_and_load_reactions(temp_output_dir: Path) -> None:
    event = make_reaction_item_event(channel="C123", message_ts="1.0")
    save_reaction_events(temp_output_dir, StreamType.CREATED, [event])

    result = load_existing_reactions(temp_output_dir)
    assert len(result) == 1
    assert "message:C123:1.0" in result


def test_save_reaction_events_creates_directory_structure(temp_output_dir: Path) -> None:
    save_reaction_events(temp_output_dir, StreamType.CREATED, [make_reaction_item_event()])
    expected_path = temp_output_dir / "reactions" / "created" / "events.jsonl"
    assert expected_path.exists()
    parsed = json.loads(expected_path.read_text().strip())
    assert parsed["source"] == "reactions"


def test_load_channel_export_metadata_returns_empty_when_missing(temp_output_dir: Path) -> None:
    result = load_channel_export_metadata(temp_output_dir)
    assert result == {}


def test_save_and_load_channel_searched_oldest(temp_output_dir: Path) -> None:
    save_channel_searched_oldest(temp_output_dir, SlackChannelId("C123"), SlackMessageTimestamp("1700000000.000000"))
    result = load_channel_export_metadata(temp_output_dir)
    assert result[SlackChannelId("C123")] == SlackMessageTimestamp("1700000000.000000")


def test_save_channel_searched_oldest_only_moves_earlier(temp_output_dir: Path) -> None:
    save_channel_searched_oldest(temp_output_dir, SlackChannelId("C123"), SlackMessageTimestamp("1700000000.000000"))
    # Attempting to save a later timestamp should be a no-op
    save_channel_searched_oldest(temp_output_dir, SlackChannelId("C123"), SlackMessageTimestamp("1800000000.000000"))
    result = load_channel_export_metadata(temp_output_dir)
    assert result[SlackChannelId("C123")] == SlackMessageTimestamp("1700000000.000000")

    # Saving an earlier timestamp should update
    save_channel_searched_oldest(temp_output_dir, SlackChannelId("C123"), SlackMessageTimestamp("1600000000.000000"))
    result = load_channel_export_metadata(temp_output_dir)
    assert result[SlackChannelId("C123")] == SlackMessageTimestamp("1600000000.000000")


def test_load_fetch_metadata_returns_empty_when_missing(temp_output_dir: Path) -> None:
    result = load_fetch_metadata(temp_output_dir)
    assert result == {}


def test_save_and_load_fetch_metadata(temp_output_dir: Path) -> None:
    ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    save_fetch_timestamp(temp_output_dir, "channels", ts)

    result = load_fetch_metadata(temp_output_dir)
    assert "channels" in result
    assert result["channels"] == ts


def test_save_fetch_timestamp_preserves_existing_entries(temp_output_dir: Path) -> None:
    ts1 = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    ts2 = datetime(2025, 1, 15, 12, 5, 0, tzinfo=timezone.utc)
    save_fetch_timestamp(temp_output_dir, "channels", ts1)
    save_fetch_timestamp(temp_output_dir, "users", ts2)

    result = load_fetch_metadata(temp_output_dir)
    assert len(result) == 2
    assert result["channels"] == ts1
    assert result["users"] == ts2


def test_load_fetch_metadata_handles_malformed_json(temp_output_dir: Path) -> None:
    temp_output_dir.mkdir(parents=True, exist_ok=True)
    (temp_output_dir / ".fetch_metadata.json").write_text("not valid json")
    result = load_fetch_metadata(temp_output_dir)
    assert result == {}
