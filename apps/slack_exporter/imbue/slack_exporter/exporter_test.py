import json
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from imbue.slack_exporter.data_types import ChannelConfig
from imbue.slack_exporter.data_types import ExporterSettings
from imbue.slack_exporter.data_types import StoredChannelInfo
from imbue.slack_exporter.exporter import _datetime_to_slack_timestamp
from imbue.slack_exporter.exporter import _fetch_all_messages_for_channel
from imbue.slack_exporter.exporter import _filter_changed_channels
from imbue.slack_exporter.exporter import run_export
from imbue.slack_exporter.primitives import SlackChannelId
from imbue.slack_exporter.primitives import SlackChannelName
from imbue.slack_exporter.primitives import SlackMessageTimestamp
from imbue.slack_exporter.store import save_messages
from imbue.slack_exporter.testing import make_fake_api_caller
from imbue.slack_exporter.testing import make_stored_channel_info
from imbue.slack_exporter.testing import make_stored_message


def _history_response(
    messages: list[dict[str, str]],
    has_more: bool = False,
    next_cursor: str = "",
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "ok": True,
        "messages": messages,
        "has_more": has_more,
    }
    if next_cursor:
        response["response_metadata"] = {"next_cursor": next_cursor}
    return response


def _channel_list_response(channels: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "ok": True,
        "channels": channels,
        "response_metadata": {"next_cursor": ""},
    }


def _user_list_response(members: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "ok": True,
        "members": members,
        "response_metadata": {"next_cursor": ""},
    }


def test_datetime_to_slack_timestamp_converts_correctly() -> None:
    dt = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    result = _datetime_to_slack_timestamp(dt)
    assert result == SlackMessageTimestamp("1704067200.000000")


def test_filter_changed_channels_returns_new_channels() -> None:
    fresh = [make_stored_channel_info("C123", "general")]
    result = _filter_changed_channels(fresh, {})
    assert len(result) == 1


def test_filter_changed_channels_skips_unchanged() -> None:
    existing_info = make_stored_channel_info("C123", "general")
    fresh = [make_stored_channel_info("C123", "general")]
    result = _filter_changed_channels(fresh, {SlackChannelId("C123"): existing_info})
    assert len(result) == 0


def test_filter_changed_channels_includes_changed() -> None:
    existing_info = make_stored_channel_info("C123", "general")
    # Create a fresh channel with different raw data to simulate a change
    changed_fresh = StoredChannelInfo(
        channel_id=SlackChannelId("C123"),
        channel_name=SlackChannelName("general"),
        fetched_at=existing_info.fetched_at,
        raw={"id": "C123", "name": "general", "topic": "new"},
    )
    result = _filter_changed_channels([changed_fresh], {SlackChannelId("C123"): existing_info})
    assert len(result) == 1


def test_fetch_all_messages_fetches_single_page() -> None:
    api_caller = make_fake_api_caller(
        {
            "conversations.history": [
                _history_response(messages=[{"ts": "1700000000.000001", "text": "hello"}]),
            ],
        }
    )

    messages = _fetch_all_messages_for_channel(
        channel_id=SlackChannelId("C123"),
        channel_name=SlackChannelName("general"),
        oldest_ts=SlackMessageTimestamp("1699999999.000000"),
        is_inclusive=True,
        api_caller=api_caller,
    )

    assert len(messages) == 1
    assert messages[0].timestamp == SlackMessageTimestamp("1700000000.000001")


def test_fetch_all_messages_handles_pagination() -> None:
    api_caller = make_fake_api_caller(
        {
            "conversations.history": [
                _history_response(
                    messages=[{"ts": "1700000000.000001", "text": "first"}],
                    has_more=True,
                    next_cursor="cursor_abc",
                ),
                _history_response(messages=[{"ts": "1700000000.000002", "text": "second"}]),
            ],
        }
    )

    messages = _fetch_all_messages_for_channel(
        channel_id=SlackChannelId("C123"),
        channel_name=SlackChannelName("general"),
        oldest_ts=SlackMessageTimestamp("1699999999.000000"),
        is_inclusive=True,
        api_caller=api_caller,
    )

    assert len(messages) == 2


def test_run_export_writes_to_directory_structure(temp_output_dir: Path) -> None:
    api_caller = make_fake_api_caller(
        {
            "conversations.list": [
                _channel_list_response(channels=[{"id": "C123", "name": "general"}]),
            ],
            "users.list": [
                _user_list_response(members=[{"id": "U001", "name": "alice"}]),
            ],
            "conversations.history": [
                _history_response(messages=[{"ts": "1700000000.000001", "text": "hello"}]),
            ],
        }
    )

    settings = ExporterSettings(
        channels=(ChannelConfig(name=SlackChannelName("general")),),
        default_oldest=datetime(2024, 1, 1, tzinfo=timezone.utc),
        output_dir=temp_output_dir,
    )

    run_export(settings, api_caller=api_caller)

    # Verify directory structure
    assert (temp_output_dir / "channels" / "events.jsonl").exists()
    assert (temp_output_dir / "messages" / "events.jsonl").exists()
    assert (temp_output_dir / "users" / "events.jsonl").exists()

    # Verify message content
    message_lines = (temp_output_dir / "messages" / "events.jsonl").read_text().strip().splitlines()
    assert len(message_lines) == 1
    assert json.loads(message_lines[0])["channel_id"] == "C123"

    # Verify user content
    user_lines = (temp_output_dir / "users" / "events.jsonl").read_text().strip().splitlines()
    assert len(user_lines) == 1
    assert json.loads(user_lines[0])["user_id"] == "U001"


def test_run_export_skips_unchanged_channels(temp_output_dir: Path) -> None:
    """When a channel hasn't changed, it should not be re-appended."""
    settings = ExporterSettings(
        channels=(ChannelConfig(name=SlackChannelName("general")),),
        default_oldest=datetime(2024, 1, 1, tzinfo=timezone.utc),
        output_dir=temp_output_dir,
    )

    # Run twice with same data
    run_export(
        settings,
        api_caller=make_fake_api_caller(
            {
                "conversations.list": [
                    _channel_list_response(channels=[{"id": "C123", "name": "general"}]),
                ],
                "users.list": [_user_list_response(members=[])],
                "conversations.history": [_history_response(messages=[])],
            }
        ),
    )
    run_export(
        settings,
        api_caller=make_fake_api_caller(
            {
                "conversations.list": [
                    _channel_list_response(channels=[{"id": "C123", "name": "general"}]),
                ],
                "users.list": [_user_list_response(members=[])],
                "conversations.history": [_history_response(messages=[])],
            }
        ),
    )

    channel_lines = (temp_output_dir / "channels" / "events.jsonl").read_text().strip().splitlines()
    # Only written once since the data didn't change
    assert len(channel_lines) == 1


def test_run_export_incremental_resumes_from_latest(temp_output_dir: Path) -> None:
    existing_msg = make_stored_message(ts="1700000000.000001")
    save_messages(temp_output_dir, [existing_msg])

    captured_params: list[dict[str, str] | None] = []

    def tracking_api_caller(method: str, query_params: dict[str, str] | None = None) -> dict[str, Any]:
        if method == "conversations.list":
            return _channel_list_response(channels=[{"id": "C123", "name": "general"}])
        elif method == "users.list":
            return _user_list_response(members=[])
        elif method == "conversations.history":
            captured_params.append(query_params)
            return _history_response(messages=[{"ts": "1700000000.000009", "text": "new"}])
        else:
            return {"ok": True}

    settings = ExporterSettings(
        channels=(ChannelConfig(name=SlackChannelName("general")),),
        default_oldest=datetime(2024, 1, 1, tzinfo=timezone.utc),
        output_dir=temp_output_dir,
    )

    run_export(settings, api_caller=tracking_api_caller)

    assert len(captured_params) == 1
    assert captured_params[0] is not None
    assert captured_params[0].get("oldest") == "1700000000.000001"
    assert captured_params[0].get("inclusive") == "false"
