import json
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from imbue.slack_exporter.data_types import ChannelConfig
from imbue.slack_exporter.data_types import ExporterSettings
from imbue.slack_exporter.exporter import _datetime_to_slack_timestamp
from imbue.slack_exporter.exporter import _fetch_all_messages_for_channel
from imbue.slack_exporter.exporter import run_export
from imbue.slack_exporter.primitives import SlackChannelId
from imbue.slack_exporter.primitives import SlackChannelName
from imbue.slack_exporter.primitives import SlackMessageTimestamp
from imbue.slack_exporter.store import StreamType
from imbue.slack_exporter.store import save_message_events
from imbue.slack_exporter.testing import make_fake_api_caller
from imbue.slack_exporter.testing import make_message_event


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


def test_fetch_all_messages_returns_event_envelope() -> None:
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
    assert messages[0].message_ts == SlackMessageTimestamp("1700000000.000001")
    assert messages[0].channel_id == SlackChannelId("C123")
    assert messages[0].source == "messages"
    assert messages[0].event_id.startswith("evt-")


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


def test_run_export_writes_to_created_streams(temp_output_dir: Path) -> None:
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

    # Verify created streams
    assert (temp_output_dir / "channels" / "created" / "events.jsonl").exists()
    assert (temp_output_dir / "messages" / "created" / "events.jsonl").exists()
    assert (temp_output_dir / "users" / "created" / "events.jsonl").exists()

    # Created events also appear in updated streams
    assert (temp_output_dir / "channels" / "updated" / "events.jsonl").exists()
    assert (temp_output_dir / "messages" / "updated" / "events.jsonl").exists()
    assert (temp_output_dir / "users" / "updated" / "events.jsonl").exists()

    msg_lines = (temp_output_dir / "messages" / "created" / "events.jsonl").read_text().strip().splitlines()
    assert len(msg_lines) == 1
    msg = json.loads(msg_lines[0])
    assert msg["channel_id"] == "C123"
    assert msg["source"] == "messages"
    assert "event_id" in msg


def test_run_export_unchanged_channels_not_written(temp_output_dir: Path) -> None:
    """Unchanged channels should not appear in either created or updated."""
    settings = ExporterSettings(
        channels=(ChannelConfig(name=SlackChannelName("general")),),
        default_oldest=datetime(2024, 1, 1, tzinfo=timezone.utc),
        output_dir=temp_output_dir,
    )

    # First run creates the channel
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

    # Second run with same data
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

    created_lines = (temp_output_dir / "channels" / "created" / "events.jsonl").read_text().strip().splitlines()
    assert len(created_lines) == 1

    # Updated stream has 1 entry from the initial create (creates also go to updated)
    # but no additional entry from the second run since data was unchanged
    updated_lines = (temp_output_dir / "channels" / "updated" / "events.jsonl").read_text().strip().splitlines()
    assert len(updated_lines) == 1


def test_run_export_changed_channels_go_to_updated_stream(temp_output_dir: Path) -> None:
    settings = ExporterSettings(
        channels=(ChannelConfig(name=SlackChannelName("general")),),
        default_oldest=datetime(2024, 1, 1, tzinfo=timezone.utc),
        output_dir=temp_output_dir,
    )

    # First run
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

    # Second run with changed channel data (added topic)
    run_export(
        settings,
        api_caller=make_fake_api_caller(
            {
                "conversations.list": [
                    {
                        "ok": True,
                        "channels": [{"id": "C123", "name": "general", "topic": "new topic"}],
                        "response_metadata": {"next_cursor": ""},
                    },
                ],
                "users.list": [_user_list_response(members=[])],
                "conversations.history": [_history_response(messages=[])],
            }
        ),
    )

    updated_path = temp_output_dir / "channels" / "updated" / "events.jsonl"
    assert updated_path.exists()
    # 1 from initial create + 1 from actual data change = 2
    updated_lines = updated_path.read_text().strip().splitlines()
    assert len(updated_lines) == 2


def test_run_export_skips_message_fetch_for_unchanged_channels(temp_output_dir: Path) -> None:
    """When a channel hasn't changed, conversations.history should not be called."""
    settings = ExporterSettings(
        channels=(ChannelConfig(name=SlackChannelName("general")),),
        default_oldest=datetime(2024, 1, 1, tzinfo=timezone.utc),
        output_dir=temp_output_dir,
    )

    # First run creates channel and fetches messages
    run_export(
        settings,
        api_caller=make_fake_api_caller(
            {
                "conversations.list": [
                    _channel_list_response(channels=[{"id": "C123", "name": "general"}]),
                ],
                "users.list": [_user_list_response(members=[])],
                "conversations.history": [_history_response(messages=[{"ts": "1700000000.000001", "text": "hi"}])],
            }
        ),
    )

    # Second run: channel unchanged, so conversations.history should NOT be called
    history_call_count = 0

    def tracking_caller(method: str, query_params: dict[str, str] | None = None) -> dict[str, Any]:
        nonlocal history_call_count
        if method == "conversations.list":
            return _channel_list_response(channels=[{"id": "C123", "name": "general"}])
        elif method == "users.list":
            return _user_list_response(members=[])
        elif method == "conversations.history":
            history_call_count += 1
            return _history_response(messages=[])
        else:
            return {"ok": True}

    run_export(settings, api_caller=tracking_caller)

    assert history_call_count == 0


def test_run_export_incremental_resumes_from_latest(temp_output_dir: Path) -> None:
    existing_msg = make_message_event(ts="1700000000.000001")
    save_message_events(temp_output_dir, StreamType.CREATED, [existing_msg])

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
