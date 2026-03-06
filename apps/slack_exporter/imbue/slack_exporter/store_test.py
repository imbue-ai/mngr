import json
from pathlib import Path

from imbue.slack_exporter.primitives import SlackChannelId
from imbue.slack_exporter.primitives import SlackChannelName
from imbue.slack_exporter.primitives import SlackMessageTimestamp
from imbue.slack_exporter.primitives import SlackUserId
from imbue.slack_exporter.store import StreamType
from imbue.slack_exporter.store import load_existing_channels
from imbue.slack_exporter.store import load_existing_message_state
from imbue.slack_exporter.store import load_existing_users
from imbue.slack_exporter.store import save_channel_events
from imbue.slack_exporter.store import save_message_events
from imbue.slack_exporter.store import save_user_events
from imbue.slack_exporter.testing import make_channel_event
from imbue.slack_exporter.testing import make_message_event
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
