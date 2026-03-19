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


_DEFAULT_AUTH_RESPONSE: dict[str, Any] = {
    "ok": True,
    "user_id": "U001",
    "user": "testuser",
    "team": "test",
    "team_id": "T001",
}


def _standard_api_caller(
    channel_data: list[dict[str, Any]] | None = None,
    user_data: list[dict[str, Any]] | None = None,
    message_data: list[dict[str, Any]] | None = None,
    reaction_data: list[dict[str, Any]] | None = None,
) -> Any:
    """Build a fake API caller with standard channel/user/message responses."""
    return make_fake_api_caller(
        {
            "auth.test": [_DEFAULT_AUTH_RESPONSE],
            "conversations.list": [
                make_slack_response("channels", channel_data or [{"id": "C123", "name": "general", "is_member": True}])
            ],
            "users.list": [make_slack_response("members", user_data or [])],
            "conversations.history": [make_slack_response("messages", message_data or [])],
            "reactions.list": [make_slack_response("items", reaction_data or [])],
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
                "auth.test": [_DEFAULT_AUTH_RESPONSE],
                "conversations.list": [
                    make_slack_response(
                        "channels", [{"id": "C123", "name": "general", "is_member": True, "topic": "new topic"}]
                    ),
                ],
                "users.list": [make_slack_response("members", [])],
                "conversations.history": [make_slack_response("messages", [])],
                "reactions.list": [make_slack_response("items", [])],
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
        if method == "auth.test":
            return _DEFAULT_AUTH_RESPONSE
        elif method == "conversations.list":
            return make_slack_response("channels", [{"id": "C123", "name": "general", "is_member": True}])
        elif method == "users.list":
            return make_slack_response("members", [])
        elif method == "conversations.history":
            captured_params.append(query_params)
            return make_slack_response("messages", [{"ts": "1700000000.000009", "text": "new"}])
        elif method == "reactions.list":
            return make_slack_response("items", [])
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
            "auth.test": [_DEFAULT_AUTH_RESPONSE],
            "conversations.list": [
                make_slack_response("channels", [{"id": "C123", "name": "general", "is_member": True}])
            ],
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
            "reactions.list": [make_slack_response("items", [])],
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
            "auth.test": [_DEFAULT_AUTH_RESPONSE],
            "conversations.list": [
                make_slack_response("channels", [{"id": "C123", "name": "general", "is_member": True}])
            ],
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
            "reactions.list": [make_slack_response("items", [])],
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


def test_run_export_writes_self_identity(default_settings: ExporterSettings) -> None:
    api_caller = _standard_api_caller()
    run_export(default_settings, api_caller=api_caller)

    output_dir = default_settings.output_dir
    identity_path = output_dir / "self_identity" / "created" / "events.jsonl"
    assert identity_path.exists()
    lines = identity_path.read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["user_id"] == "U001"
    assert record["user_name"] == "testuser"


def test_run_export_self_identity_unchanged_not_duplicated(default_settings: ExporterSettings) -> None:
    run_export(default_settings, api_caller=_standard_api_caller())
    run_export(default_settings, api_caller=_standard_api_caller())

    output_dir = default_settings.output_dir
    created_lines = (output_dir / "self_identity" / "created" / "events.jsonl").read_text().strip().splitlines()
    assert len(created_lines) == 1


def test_run_export_writes_unread_markers(default_settings: ExporterSettings) -> None:
    api_caller = _standard_api_caller(
        channel_data=[{"id": "C123", "name": "general", "is_member": True, "last_read": "1700000000.000001"}],
    )
    run_export(default_settings, api_caller=api_caller)

    output_dir = default_settings.output_dir
    marker_path = output_dir / "unread_markers" / "created" / "events.jsonl"
    assert marker_path.exists()
    lines = marker_path.read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["channel_id"] == "C123"
    assert record["last_read_ts"] == "1700000000.000001"


def test_run_export_unread_markers_updated_when_changed(default_settings: ExporterSettings) -> None:
    api_caller1 = _standard_api_caller(
        channel_data=[{"id": "C123", "name": "general", "is_member": True, "last_read": "1700000000.000001"}],
    )
    run_export(default_settings, api_caller=api_caller1)

    api_caller2 = _standard_api_caller(
        channel_data=[{"id": "C123", "name": "general", "is_member": True, "last_read": "1700000000.000099"}],
    )
    run_export(default_settings, api_caller=api_caller2)

    output_dir = default_settings.output_dir
    created_lines = (output_dir / "unread_markers" / "created" / "events.jsonl").read_text().strip().splitlines()
    assert len(created_lines) == 1

    updated_lines = (output_dir / "unread_markers" / "updated" / "events.jsonl").read_text().strip().splitlines()
    assert len(updated_lines) == 2


def test_run_export_writes_reaction_items(default_settings: ExporterSettings) -> None:
    reaction_item = {
        "type": "message",
        "channel": "C123",
        "message": {
            "ts": "1700000000.000001",
            "text": "hello",
            "reactions": [{"name": "thumbsup", "users": ["U001"], "count": 1}],
        },
    }
    api_caller = _standard_api_caller(reaction_data=[reaction_item])
    run_export(default_settings, api_caller=api_caller)

    output_dir = default_settings.output_dir
    reaction_path = output_dir / "reactions" / "created" / "events.jsonl"
    assert reaction_path.exists()
    lines = reaction_path.read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["user_id"] == "U001"
    assert record["source"] == "reactions"


def test_run_export_skips_replies_for_non_threaded_messages(default_settings: ExporterSettings) -> None:
    reply_call_count = 0

    def tracking_caller(method: str, query_params: dict[str, str] | None = None) -> dict[str, Any]:
        nonlocal reply_call_count
        if method == "auth.test":
            return _DEFAULT_AUTH_RESPONSE
        elif method == "conversations.list":
            return make_slack_response("channels", [{"id": "C123", "name": "general", "is_member": True}])
        elif method == "users.list":
            return make_slack_response("members", [])
        elif method == "conversations.history":
            return make_slack_response("messages", [{"ts": "1700000000.000001", "text": "no thread"}])
        elif method == "conversations.replies":
            reply_call_count += 1
            return make_slack_response("messages", [])
        elif method == "reactions.list":
            return make_slack_response("items", [])
        else:
            return {"ok": True}

    run_export(default_settings, api_caller=tracking_caller)

    assert reply_call_count == 0


def _make_cached_settings(temp_output_dir: Path, cache_ttl_seconds: int = 3600) -> ExporterSettings:
    """Create settings with caching enabled (large TTL so cache is fresh within a test)."""
    return ExporterSettings(
        channels=(ChannelConfig(name=SlackChannelName("general")),),
        default_oldest=datetime(2024, 1, 1, tzinfo=timezone.utc),
        output_dir=temp_output_dir,
        cache_ttl_seconds=cache_ttl_seconds,
    )


def _tracking_api_caller(
    channel_data: list[dict[str, Any]] | None = None,
    user_data: list[dict[str, Any]] | None = None,
    message_data: list[dict[str, Any]] | None = None,
    reply_data: list[dict[str, Any]] | None = None,
    reaction_data: list[dict[str, Any]] | None = None,
) -> tuple[Any, dict[str, int]]:
    """Build a fake API caller that also tracks call counts per method.

    Unlike _standard_api_caller (which uses indexed responses), this returns the
    same response every time a method is called, making it safe for multi-run tests.
    """
    call_counts: dict[str, int] = {}

    def caller(method: str, query_params: dict[str, str] | None = None) -> dict[str, Any]:
        call_counts[method] = call_counts.get(method, 0) + 1
        if method == "auth.test":
            return _DEFAULT_AUTH_RESPONSE
        elif method == "conversations.list":
            return make_slack_response(
                "channels", channel_data or [{"id": "C123", "name": "general", "is_member": True}]
            )
        elif method == "users.list":
            return make_slack_response("members", user_data or [])
        elif method == "conversations.history":
            return make_slack_response("messages", message_data or [])
        elif method == "conversations.replies":
            return make_slack_response("messages", reply_data or [])
        elif method == "reactions.list":
            return make_slack_response("items", reaction_data or [])
        else:
            return {"ok": True}

    return caller, call_counts


def test_run_export_caches_channels_and_users_within_ttl(temp_output_dir: Path) -> None:
    settings = _make_cached_settings(temp_output_dir)
    caller, call_counts = _tracking_api_caller(
        user_data=[{"id": "U001", "name": "alice"}],
    )

    run_export(settings, api_caller=caller)
    assert call_counts["conversations.list"] == 1
    assert call_counts["users.list"] == 1
    assert call_counts["auth.test"] == 1
    assert call_counts["reactions.list"] == 1

    # Second run: should use cached data for channels, users, self identity, reactions
    call_counts.clear()
    run_export(settings, api_caller=caller)
    assert "conversations.list" not in call_counts
    assert "users.list" not in call_counts
    assert "auth.test" not in call_counts
    assert "reactions.list" not in call_counts
    # Messages are always fetched (not cached)
    assert call_counts.get("conversations.history", 0) == 1


def test_run_export_refresh_forces_refetch(temp_output_dir: Path) -> None:
    settings = _make_cached_settings(temp_output_dir)
    caller, call_counts = _tracking_api_caller()

    # First run populates cache
    run_export(settings, api_caller=caller)

    # Second run with refresh=True should re-fetch everything
    call_counts.clear()
    refresh_settings = ExporterSettings(
        channels=settings.channels,
        default_oldest=settings.default_oldest,
        output_dir=settings.output_dir,
        cache_ttl_seconds=settings.cache_ttl_seconds,
        refresh=True,
    )
    run_export(refresh_settings, api_caller=caller)
    assert call_counts["conversations.list"] == 1
    assert call_counts["users.list"] == 1
    assert call_counts["auth.test"] == 1
    assert call_counts["reactions.list"] == 1


def test_run_export_skips_replies_when_latest_reply_unchanged(default_settings: ExporterSettings) -> None:
    """When a thread's latest_reply matches what we already have, skip fetching replies."""
    caller, call_counts = _tracking_api_caller(
        message_data=[
            {
                "ts": "1700000000.000001",
                "text": "parent",
                "reply_count": 1,
                "latest_reply": "1700000000.000002",
            }
        ],
        reply_data=[
            {"ts": "1700000000.000001", "thread_ts": "1700000000.000001", "text": "parent"},
            {"ts": "1700000000.000002", "thread_ts": "1700000000.000001", "text": "reply"},
        ],
    )

    run_export(default_settings, api_caller=caller)
    assert call_counts.get("conversations.replies", 0) == 1

    # Second run with same latest_reply: should skip the thread
    call_counts.clear()
    run_export(default_settings, api_caller=caller)
    assert call_counts.get("conversations.replies", 0) == 0


def test_run_export_fetches_replies_when_latest_reply_changed(default_settings: ExporterSettings) -> None:
    """When a thread's latest_reply is newer than what we have, fetch replies."""
    # Run 1: thread with one reply
    caller1, counts1 = _tracking_api_caller(
        message_data=[
            {
                "ts": "1700000000.000001",
                "text": "parent",
                "reply_count": 1,
                "latest_reply": "1700000000.000002",
            }
        ],
        reply_data=[
            {"ts": "1700000000.000001", "thread_ts": "1700000000.000001", "text": "parent"},
            {"ts": "1700000000.000002", "thread_ts": "1700000000.000001", "text": "reply 1"},
        ],
    )
    run_export(default_settings, api_caller=caller1)
    assert counts1.get("conversations.replies", 0) == 1

    # Run 2: thread has a new reply (latest_reply changed)
    caller2, counts2 = _tracking_api_caller(
        message_data=[
            {
                "ts": "1700000000.000001",
                "text": "parent",
                "reply_count": 2,
                "latest_reply": "1700000000.000003",
            }
        ],
        reply_data=[
            {"ts": "1700000000.000001", "thread_ts": "1700000000.000001", "text": "parent"},
            {"ts": "1700000000.000002", "thread_ts": "1700000000.000001", "text": "reply 1"},
            {"ts": "1700000000.000003", "thread_ts": "1700000000.000001", "text": "reply 2"},
        ],
    )
    run_export(default_settings, api_caller=caller2)
    assert counts2.get("conversations.replies", 0) == 1

    reply_path = default_settings.output_dir / "replies" / "created" / "events.jsonl"
    reply_lines = reply_path.read_text().strip().splitlines()
    reply_timestamps = sorted(json.loads(line)["reply_ts"] for line in reply_lines)
    assert "1700000000.000003" in reply_timestamps


def test_run_export_backfills_older_messages_when_since_is_earlier(temp_output_dir: Path) -> None:
    """When --since is earlier than the oldest exported message, backfill older messages."""
    # First run: export with default_oldest=2024-06-01, producing messages from that range
    initial_settings = ExporterSettings(
        channels=(ChannelConfig(name=SlackChannelName("general")),),
        default_oldest=datetime(2024, 6, 1, tzinfo=timezone.utc),
        output_dir=temp_output_dir,
        cache_ttl_seconds=0,
    )
    caller1, _ = _tracking_api_caller(
        message_data=[{"ts": "1717200000.000001", "text": "june msg"}],
    )
    run_export(initial_settings, api_caller=caller1)

    # Verify first run saved one message
    msg_path = temp_output_dir / "messages" / "created" / "events.jsonl"
    assert len(msg_path.read_text().strip().splitlines()) == 1

    # Second run: earlier --since date; track the conversations.history calls
    backfill_settings = ExporterSettings(
        channels=(ChannelConfig(name=SlackChannelName("general")),),
        default_oldest=datetime(2024, 1, 1, tzinfo=timezone.utc),
        output_dir=temp_output_dir,
        cache_ttl_seconds=0,
    )
    history_params: list[dict[str, str] | None] = []
    base_caller, _ = _tracking_api_caller()

    def tracking_caller(method: str, query_params: dict[str, str] | None = None) -> dict[str, Any]:
        if method == "conversations.history":
            history_params.append(query_params)
            # Return a backfill message for calls with a "latest" param, empty for forward
            if query_params and "latest" in query_params:
                return make_slack_response("messages", [{"ts": "1704200000.000001", "text": "jan msg"}])
            return make_slack_response("messages", [])
        return base_caller(method, query_params)

    run_export(backfill_settings, api_caller=tracking_caller)

    # Should have made two conversations.history calls: forward + backfill
    assert len(history_params) == 2

    # Forward call: resumes from latest known message
    forward_params = history_params[0]
    assert forward_params is not None
    assert forward_params["oldest"] == "1717200000.000001"
    assert forward_params["inclusive"] == "false"
    assert "latest" not in forward_params

    # Backfill call: from the new --since date up to the first run's searched-from point
    backfill_params = history_params[1]
    assert backfill_params is not None
    assert backfill_params["oldest"] == _datetime_to_slack_timestamp(datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert backfill_params["inclusive"] == "true"
    assert backfill_params["latest"] == _datetime_to_slack_timestamp(datetime(2024, 6, 1, tzinfo=timezone.utc))

    # Backfilled message should be saved
    msg_lines = msg_path.read_text().strip().splitlines()
    assert len(msg_lines) == 2


def test_run_export_no_backfill_when_since_is_same_or_later(temp_output_dir: Path) -> None:
    """When --since is the same as a previous run, no backfill call is made, even if the oldest
    message is later than --since (i.e. there were no messages in the early part of the range)."""
    settings = ExporterSettings(
        channels=(ChannelConfig(name=SlackChannelName("general")),),
        default_oldest=datetime(2024, 1, 1, tzinfo=timezone.utc),
        output_dir=temp_output_dir,
        cache_ttl_seconds=0,
    )
    # Message is much later than --since -- there's a gap with no messages
    caller1, _ = _tracking_api_caller(
        message_data=[{"ts": "1717200000.000001", "text": "june msg"}],
    )
    run_export(settings, api_caller=caller1)

    # Second run with the same --since: should only do forward fetch (no backfill into the gap)
    caller2, counts2 = _tracking_api_caller()
    run_export(settings, api_caller=caller2)
    assert counts2.get("conversations.history", 0) == 1
