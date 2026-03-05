import json
from pathlib import Path

from imbue.slack_exporter.primitives import SlackChannelId
from imbue.slack_exporter.primitives import SlackChannelName
from imbue.slack_exporter.primitives import SlackMessageTimestamp
from imbue.slack_exporter.primitives import SlackUserId
from imbue.slack_exporter.store import load_existing_channels
from imbue.slack_exporter.store import load_existing_message_state
from imbue.slack_exporter.store import load_existing_user_ids
from imbue.slack_exporter.store import save_channels
from imbue.slack_exporter.store import save_messages
from imbue.slack_exporter.store import save_users
from imbue.slack_exporter.testing import make_stored_channel_info
from imbue.slack_exporter.testing import make_stored_message
from imbue.slack_exporter.testing import make_stored_user


def test_load_existing_channels_returns_empty_when_dir_missing(temp_output_dir: Path) -> None:
    result = load_existing_channels(temp_output_dir)
    assert result == {}


def test_load_existing_channels_returns_latest_per_id(temp_output_dir: Path) -> None:
    info1 = make_stored_channel_info("C123", "general")
    info2 = make_stored_channel_info("C123", "general-renamed")
    save_channels(temp_output_dir, [info1, info2])

    result = load_existing_channels(temp_output_dir)
    assert len(result) == 1
    assert result[SlackChannelId("C123")].channel_name == SlackChannelName("general-renamed")


def test_load_existing_message_state_returns_empty_when_missing(temp_output_dir: Path) -> None:
    state, keys = load_existing_message_state(temp_output_dir)
    assert state == {}
    assert keys == set()


def test_load_existing_message_state_tracks_latest_timestamp(temp_output_dir: Path) -> None:
    msg1 = make_stored_message(ts="1700000000.000001")
    msg2 = make_stored_message(ts="1700000000.000009")
    save_messages(temp_output_dir, [msg1, msg2])

    state, keys = load_existing_message_state(temp_output_dir)

    assert SlackChannelId("C123") in state
    assert state[SlackChannelId("C123")].latest_message_timestamp == SlackMessageTimestamp("1700000000.000009")
    assert len(keys) == 2


def test_load_existing_user_ids_returns_empty_when_missing(temp_output_dir: Path) -> None:
    result = load_existing_user_ids(temp_output_dir)
    assert result == set()


def test_load_existing_user_ids_returns_stored_ids(temp_output_dir: Path) -> None:
    user1 = make_stored_user("U111")
    user2 = make_stored_user("U222")
    save_users(temp_output_dir, [user1, user2])

    result = load_existing_user_ids(temp_output_dir)
    assert result == {SlackUserId("U111"), SlackUserId("U222")}


def test_save_channels_creates_directory_structure(temp_output_dir: Path) -> None:
    info = make_stored_channel_info()
    save_channels(temp_output_dir, [info])

    expected_path = temp_output_dir / "channels" / "events.jsonl"
    assert expected_path.exists()
    lines = expected_path.read_text().strip().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["channel_id"] == "C123"


def test_save_messages_creates_directory_structure(temp_output_dir: Path) -> None:
    msg = make_stored_message()
    save_messages(temp_output_dir, [msg])

    expected_path = temp_output_dir / "messages" / "events.jsonl"
    assert expected_path.exists()
    lines = expected_path.read_text().strip().splitlines()
    assert len(lines) == 1


def test_save_users_creates_directory_structure(temp_output_dir: Path) -> None:
    user = make_stored_user()
    save_users(temp_output_dir, [user])

    expected_path = temp_output_dir / "users" / "events.jsonl"
    assert expected_path.exists()
    lines = expected_path.read_text().strip().splitlines()
    assert len(lines) == 1


def test_save_appends_to_existing(temp_output_dir: Path) -> None:
    msg1 = make_stored_message(ts="1700000000.000001")
    save_messages(temp_output_dir, [msg1])

    msg2 = make_stored_message(ts="1700000000.000002")
    save_messages(temp_output_dir, [msg2])

    expected_path = temp_output_dir / "messages" / "events.jsonl"
    lines = expected_path.read_text().strip().splitlines()
    assert len(lines) == 2


def test_save_does_nothing_for_empty_list(temp_output_dir: Path) -> None:
    save_messages(temp_output_dir, [])
    assert not (temp_output_dir / "messages" / "events.jsonl").exists()
