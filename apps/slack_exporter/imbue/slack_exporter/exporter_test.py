import json
from datetime import datetime
from datetime import timezone
from typing import Any

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
from imbue.slack_exporter.testing import make_slack_response


def test_datetime_to_slack_timestamp_converts_correctly() -> None:
    dt = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    result = _datetime_to_slack_timestamp(dt)
    assert result == SlackMessageTimestamp("1704067200.000000")


def test_fetch_all_messages_returns_event_envelope() -> None:
    api_caller = make_fake_api_caller(
        {"conversations.history": [make_slack_response("messages", [{"ts": "1700000000.000001", "text": "hello"}])]}
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
    assert messages[0].source == "messages"


def test_fetch_all_messages_handles_pagination() -> None:
    api_caller = make_fake_api_caller(
        {
            "conversations.history": [
                make_slack_response(
                    "messages", [{"ts": "1700000000.000001", "text": "first"}], has_more=True, next_cursor="c1"
                ),
                make_slack_response("messages", [{"ts": "1700000000.000002", "text": "second"}]),
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


def _standard_api_caller(
    channel_data: list[dict[str, Any]] | None = None,
    user_data: list[dict[str, Any]] | None = None,
    message_data: list[dict[str, Any]] | None = None,
) -> Any:
    """Build a fake API caller with standard channel/user/message responses."""
    return make_fake_api_caller(
        {
            "conversations.list": [
                make_slack_response("channels", channel_data or [{"id": "C123", "name": "general"}])
            ],
            "users.list": [make_slack_response("members", user_data or [])],
            "conversations.history": [make_slack_response("messages", message_data or [])],
        }
    )


def test_run_export_writes_to_created_streams(default_settings: ExporterSettings) -> None:
    api_caller = _standard_api_caller(
        user_data=[{"id": "U001", "name": "alice"}],
        message_data=[{"ts": "1700000000.000001", "text": "hello"}],
    )

    run_export(default_settings, api_caller=api_caller)

    output_dir = default_settings.output_dir
    assert (output_dir / "channels" / "created" / "events.jsonl").exists()
    assert (output_dir / "messages" / "created" / "events.jsonl").exists()
    assert (output_dir / "users" / "created" / "events.jsonl").exists()
    assert (output_dir / "channels" / "updated" / "events.jsonl").exists()
    assert (output_dir / "messages" / "updated" / "events.jsonl").exists()
    assert (output_dir / "users" / "updated" / "events.jsonl").exists()

    msg_lines = (output_dir / "messages" / "created" / "events.jsonl").read_text().strip().splitlines()
    assert len(msg_lines) == 1
    msg = json.loads(msg_lines[0])
    assert msg["channel_id"] == "C123"
    assert msg["source"] == "messages"


def test_run_export_unchanged_channels_not_written(default_settings: ExporterSettings) -> None:
    run_export(default_settings, api_caller=_standard_api_caller())
    run_export(default_settings, api_caller=_standard_api_caller())

    output_dir = default_settings.output_dir
    created_lines = (output_dir / "channels" / "created" / "events.jsonl").read_text().strip().splitlines()
    assert len(created_lines) == 1

    updated_lines = (output_dir / "channels" / "updated" / "events.jsonl").read_text().strip().splitlines()
    assert len(updated_lines) == 1


def test_run_export_changed_channels_go_to_updated_stream(default_settings: ExporterSettings) -> None:
    run_export(default_settings, api_caller=_standard_api_caller())

    # Second run with changed channel data
    run_export(
        default_settings,
        api_caller=make_fake_api_caller(
            {
                "conversations.list": [
                    make_slack_response("channels", [{"id": "C123", "name": "general", "topic": "new topic"}]),
                ],
                "users.list": [make_slack_response("members", [])],
                "conversations.history": [make_slack_response("messages", [])],
            }
        ),
    )

    output_dir = default_settings.output_dir
    updated_lines = (output_dir / "channels" / "updated" / "events.jsonl").read_text().strip().splitlines()
    assert len(updated_lines) == 2


def test_run_export_incremental_resumes_from_latest(default_settings: ExporterSettings) -> None:
    save_message_events(default_settings.output_dir, StreamType.CREATED, [make_message_event(ts="1700000000.000001")])

    captured_params: list[dict[str, str] | None] = []

    def tracking_api_caller(method: str, query_params: dict[str, str] | None = None) -> dict[str, Any]:
        if method == "conversations.list":
            return make_slack_response("channels", [{"id": "C123", "name": "general"}])
        elif method == "users.list":
            return make_slack_response("members", [])
        elif method == "conversations.history":
            captured_params.append(query_params)
            return make_slack_response("messages", [{"ts": "1700000000.000009", "text": "new"}])
        else:
            return {"ok": True}

    run_export(default_settings, api_caller=tracking_api_caller)

    assert len(captured_params) == 1
    assert captured_params[0] is not None
    assert captured_params[0].get("oldest") == "1700000000.000001"
    assert captured_params[0].get("inclusive") == "false"


def test_run_export_fetches_replies_for_threaded_messages(default_settings: ExporterSettings) -> None:
    threaded_message = {"ts": "1700000000.000001", "text": "parent", "reply_count": 2}

    api_caller = make_fake_api_caller(
        {
            "conversations.list": [make_slack_response("channels", [{"id": "C123", "name": "general"}])],
            "users.list": [make_slack_response("members", [])],
            "conversations.history": [make_slack_response("messages", [threaded_message])],
            "conversations.replies": [
                make_slack_response(
                    "messages",
                    [
                        {"ts": "1700000000.000001", "thread_ts": "1700000000.000001", "text": "parent"},
                        {"ts": "1700000000.000002", "thread_ts": "1700000000.000001", "text": "reply 1"},
                        {"ts": "1700000000.000003", "thread_ts": "1700000000.000001", "text": "reply 2"},
                    ],
                ),
            ],
        }
    )

    run_export(default_settings, api_caller=api_caller)

    reply_path = default_settings.output_dir / "replies" / "created" / "events.jsonl"
    assert reply_path.exists()
    reply_lines = reply_path.read_text().strip().splitlines()
    assert len(reply_lines) == 2
    first_reply = json.loads(reply_lines[0])
    assert first_reply["thread_ts"] == "1700000000.000001"
    assert first_reply["source"] == "replies"


def test_run_export_fetches_paginated_replies(default_settings: ExporterSettings) -> None:
    threaded_message = {"ts": "1700000000.000001", "text": "parent", "reply_count": 3}

    api_caller = make_fake_api_caller(
        {
            "conversations.list": [make_slack_response("channels", [{"id": "C123", "name": "general"}])],
            "users.list": [make_slack_response("members", [])],
            "conversations.history": [make_slack_response("messages", [threaded_message])],
            "conversations.replies": [
                {
                    "ok": True,
                    "messages": [
                        {"ts": "1700000000.000001", "thread_ts": "1700000000.000001", "text": "parent"},
                        {"ts": "1700000000.000002", "thread_ts": "1700000000.000001", "text": "reply 1"},
                    ],
                    "has_more": True,
                    "response_metadata": {"next_cursor": "reply_page2"},
                },
                make_slack_response(
                    "messages",
                    [{"ts": "1700000000.000003", "thread_ts": "1700000000.000001", "text": "reply 2"}],
                ),
            ],
        }
    )

    run_export(default_settings, api_caller=api_caller)

    reply_path = default_settings.output_dir / "replies" / "created" / "events.jsonl"
    assert reply_path.exists()
    reply_lines = reply_path.read_text().strip().splitlines()
    # 2 replies (parent message is excluded from replies)
    assert len(reply_lines) == 2
    reply_timestamps = sorted(json.loads(line)["reply_ts"] for line in reply_lines)
    assert reply_timestamps == ["1700000000.000002", "1700000000.000003"]


def test_run_export_skips_replies_for_non_threaded_messages(default_settings: ExporterSettings) -> None:
    reply_call_count = 0

    def tracking_caller(method: str, query_params: dict[str, str] | None = None) -> dict[str, Any]:
        nonlocal reply_call_count
        if method == "conversations.list":
            return make_slack_response("channels", [{"id": "C123", "name": "general"}])
        elif method == "users.list":
            return make_slack_response("members", [])
        elif method == "conversations.history":
            return make_slack_response("messages", [{"ts": "1700000000.000001", "text": "no thread"}])
        elif method == "conversations.replies":
            reply_call_count += 1
            return make_slack_response("messages", [])
        else:
            return {"ok": True}

    run_export(default_settings, api_caller=tracking_caller)

    assert reply_call_count == 0
