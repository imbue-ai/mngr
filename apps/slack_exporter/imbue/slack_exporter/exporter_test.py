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
    assert messages[0].source == "slack"


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
) -> Any:
    """Build a fake API caller with standard channel/user/message responses."""
    channels = channel_data or [{"id": "C123", "name": "general", "is_member": True}]
    msg_response = make_slack_response("messages", message_data or [])
    return make_fake_api_caller(
        {
            "auth.test": [_DEFAULT_AUTH_RESPONSE],
            "conversations.list": [make_slack_response("channels", channels)],
            "conversations.info": [
                {
                    "ok": True,
                    "channel": {**ch, "last_read": ch.get("last_read", "1700000000.000000")},
                }
                for ch in channels
            ],
            "users.list": [make_slack_response("members", user_data or [])],
            "conversations.history": [msg_response],
        }
    )


def test_run_export_writes_to_created_streams(default_settings: ExporterSettings) -> None:
    api_caller = _standard_api_caller(
        user_data=[{"id": "U001", "name": "alice"}],
        message_data=[{"ts": "1700000000.000001", "text": "hello"}],
    )

    run_export(default_settings, api_caller=api_caller)

    output_dir = default_settings.output_dir
    assert (output_dir / "channel" / "created" / "events.jsonl").exists()
    assert (output_dir / "message" / "created" / "events.jsonl").exists()
    assert (output_dir / "user" / "created" / "events.jsonl").exists()
    assert (output_dir / "channel" / "updated" / "events.jsonl").exists()
    assert (output_dir / "message" / "updated" / "events.jsonl").exists()
    assert (output_dir / "user" / "updated" / "events.jsonl").exists()

    msg_lines = (output_dir / "message" / "created" / "events.jsonl").read_text().strip().splitlines()
    assert len(msg_lines) == 1
    msg = json.loads(msg_lines[0])
    assert msg["channel_id"] == "C123"
    assert msg["source"] == "slack"


def test_run_export_unchanged_channels_not_written(default_settings: ExporterSettings) -> None:
    run_export(default_settings, api_caller=_standard_api_caller())
    run_export(default_settings, api_caller=_standard_api_caller())

    output_dir = default_settings.output_dir
    created_lines = (output_dir / "channel" / "created" / "events.jsonl").read_text().strip().splitlines()
    assert len(created_lines) == 1

    updated_lines = (output_dir / "channel" / "updated" / "events.jsonl").read_text().strip().splitlines()
    assert len(updated_lines) == 1


def test_run_export_changed_channels_go_to_updated_stream(default_settings: ExporterSettings) -> None:
    run_export(default_settings, api_caller=_standard_api_caller())

    # Second run: conversations.info returns updated channel data (topic changed).
    # Since the channel is already cached, conversations.list is skipped, but
    # conversations.info still updates channel data.
    run_export(
        default_settings,
        api_caller=make_fake_api_caller(
            {
                "auth.test": [_DEFAULT_AUTH_RESPONSE],
                "conversations.info": [
                    {
                        "ok": True,
                        "channel": {
                            "id": "C123",
                            "name": "general",
                            "is_member": True,
                            "topic": "new topic",
                            "last_read": "1700000000.000000",
                        },
                    },
                ],
                "users.list": [make_slack_response("members", [])],
                "conversations.history": [
                    make_slack_response("messages", []),
                ],
            }
        ),
    )

    output_dir = default_settings.output_dir
    updated_lines = (output_dir / "channel" / "updated" / "events.jsonl").read_text().strip().splitlines()
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
        else:
            return {"ok": True}

    run_export(default_settings, api_caller=tracking_api_caller)

    # One conversations.history call: forward fetch only
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
            "conversations.info": [
                {"ok": True, "channel": {"id": "C123", "last_read": "1700000000.000000"}},
            ],
            "users.list": [make_slack_response("members", [])],
            "conversations.history": [
                make_slack_response("messages", [threaded_message]),
            ],
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

    reply_path = default_settings.output_dir / "reply" / "created" / "events.jsonl"
    assert reply_path.exists()
    reply_lines = reply_path.read_text().strip().splitlines()
    assert len(reply_lines) == 2
    first_reply = json.loads(reply_lines[0])
    assert first_reply["thread_ts"] == "1700000000.000001"
    assert first_reply["source"] == "slack"


def test_run_export_fetches_paginated_replies(default_settings: ExporterSettings) -> None:
    threaded_message = {"ts": "1700000000.000001", "text": "parent", "reply_count": 3}

    api_caller = make_fake_api_caller(
        {
            "auth.test": [_DEFAULT_AUTH_RESPONSE],
            "conversations.list": [
                make_slack_response("channels", [{"id": "C123", "name": "general", "is_member": True}])
            ],
            "conversations.info": [
                {"ok": True, "channel": {"id": "C123", "last_read": "1700000000.000000"}},
            ],
            "users.list": [make_slack_response("members", [])],
            "conversations.history": [
                make_slack_response("messages", [threaded_message]),
            ],
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

    reply_path = default_settings.output_dir / "reply" / "created" / "events.jsonl"
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
    marker_path = output_dir / "unread_marker" / "created" / "events.jsonl"
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
    created_lines = (output_dir / "unread_marker" / "created" / "events.jsonl").read_text().strip().splitlines()
    assert len(created_lines) == 1

    updated_lines = (output_dir / "unread_marker" / "updated" / "events.jsonl").read_text().strip().splitlines()
    assert len(updated_lines) == 2


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
        max_recent_threads_for_reactions=0,
        cache_ttl_seconds=cache_ttl_seconds,
    )


def _tracking_api_caller(
    channel_data: list[dict[str, Any]] | None = None,
    user_data: list[dict[str, Any]] | None = None,
    message_data: list[dict[str, Any]] | None = None,
    reply_data: list[dict[str, Any]] | None = None,
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
        elif method == "conversations.info":
            channel_id = (query_params or {}).get("channel", "C123")
            all_channels = channel_data or [{"id": "C123", "name": "general", "is_member": True}]
            matched_channel = next(
                (ch for ch in all_channels if ch["id"] == channel_id), {"id": channel_id, "name": "unknown"}
            )
            return {"ok": True, "channel": {**matched_channel, "last_read": "1700000000.000000"}}
        elif method == "conversations.history":
            return make_slack_response("messages", message_data or [])
        elif method == "conversations.replies":
            return make_slack_response("messages", reply_data or [])
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
    # Second run: should use cached data for channels, users, self identity
    call_counts.clear()
    run_export(settings, api_caller=caller)
    assert "conversations.list" not in call_counts
    assert "users.list" not in call_counts
    assert "auth.test" not in call_counts
    # Messages are always fetched (forward fetch only, no reaction scan)
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
        max_recent_threads_for_reactions=settings.max_recent_threads_for_reactions,
        cache_ttl_seconds=settings.cache_ttl_seconds,
        refresh=True,
    )
    run_export(refresh_settings, api_caller=caller)
    assert call_counts["conversations.list"] == 1
    assert call_counts["users.list"] == 1
    assert call_counts["auth.test"] == 1


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

    reply_path = default_settings.output_dir / "reply" / "created" / "events.jsonl"
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
    msg_path = temp_output_dir / "message" / "created" / "events.jsonl"
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

    # Second run with the same --since: forward fetch only, no backfill
    caller2, counts2 = _tracking_api_caller()
    run_export(settings, api_caller=caller2)
    assert counts2.get("conversations.history", 0) == 1


def test_run_export_extracts_reactions_from_fetched_messages(default_settings: ExporterSettings) -> None:
    """Reactions on messages from the forward fetch are extracted and saved."""
    msg_with_reactions = {
        "ts": "1700000000.000001",
        "text": "hello",
        "reactions": [{"name": "thumbsup", "users": ["U001"], "count": 1}],
    }
    api_caller = _standard_api_caller(message_data=[msg_with_reactions])
    run_export(default_settings, api_caller=api_caller)

    reaction_path = default_settings.output_dir / "reaction" / "created" / "events.jsonl"
    assert reaction_path.exists()
    lines = reaction_path.read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["message_ts"] == "1700000000.000001"
    assert record["raw"]["reactions"][0]["name"] == "thumbsup"


def test_run_export_detects_relevant_threads(default_settings: ExporterSettings) -> None:
    """Threads where the authenticated user replied are detected as relevant."""
    threaded_message = {"ts": "1700000000.000001", "text": "parent", "reply_count": 1}
    caller, _ = _tracking_api_caller(
        message_data=[threaded_message],
        reply_data=[
            {"ts": "1700000000.000001", "thread_ts": "1700000000.000001", "text": "parent", "user": "U999"},
            {"ts": "1700000000.000002", "thread_ts": "1700000000.000001", "text": "my reply", "user": "U001"},
        ],
    )
    run_export(default_settings, api_caller=caller)

    rt_path = default_settings.output_dir / "relevant_thread" / "created" / "events.jsonl"
    assert rt_path.exists()
    lines = rt_path.read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["thread_ts"] == "1700000000.000001"
    assert "participated" in record["relevance_reasons"]


def test_run_export_saves_relevant_thread_replies_for_newly_relevant_thread(
    default_settings: ExporterSettings,
) -> None:
    """When a thread becomes relevant, all its replies are saved to relevant_thread_replies."""
    threaded_message = {"ts": "1700000000.000001", "text": "parent", "reply_count": 2}
    caller, _ = _tracking_api_caller(
        message_data=[threaded_message],
        reply_data=[
            {"ts": "1700000000.000001", "thread_ts": "1700000000.000001", "text": "parent", "user": "U999"},
            {"ts": "1700000000.000002", "thread_ts": "1700000000.000001", "text": "reply 1", "user": "U999"},
            {"ts": "1700000000.000003", "thread_ts": "1700000000.000001", "text": "my reply", "user": "U001"},
        ],
    )
    run_export(default_settings, api_caller=caller)

    rt_replies_path = default_settings.output_dir / "relevant_thread_reply" / "created" / "events.jsonl"
    assert rt_replies_path.exists()
    lines = rt_replies_path.read_text().strip().splitlines()
    # Both replies (excluding parent) should be in relevant_thread_replies
    assert len(lines) == 2
    reply_timestamps = sorted(json.loads(line)["reply_ts"] for line in lines)
    assert reply_timestamps == ["1700000000.000002", "1700000000.000003"]


def test_run_export_saves_new_replies_in_already_relevant_thread(temp_output_dir: Path) -> None:
    """New replies in an already-relevant thread are saved to relevant_thread_replies."""
    settings = ExporterSettings(
        channels=(ChannelConfig(name=SlackChannelName("general")),),
        default_oldest=datetime(2024, 1, 1, tzinfo=timezone.utc),
        output_dir=temp_output_dir,
        max_recent_threads_for_reactions=0,
        cache_ttl_seconds=0,
    )

    # Run 1: discover a relevant thread
    threaded_msg_v1 = {
        "ts": "1700000000.000001",
        "text": "parent",
        "reply_count": 1,
        "latest_reply": "1700000000.000002",
    }
    caller1, _ = _tracking_api_caller(
        message_data=[threaded_msg_v1],
        reply_data=[
            {"ts": "1700000000.000001", "thread_ts": "1700000000.000001", "text": "parent", "user": "U999"},
            {"ts": "1700000000.000002", "thread_ts": "1700000000.000001", "text": "my reply", "user": "U001"},
        ],
    )
    run_export(settings, api_caller=caller1)

    rt_replies_path = temp_output_dir / "relevant_thread_reply" / "created" / "events.jsonl"
    initial_count = len(rt_replies_path.read_text().strip().splitlines())
    assert initial_count == 1

    # Run 2: new reply in the same thread (already relevant)
    threaded_msg_v2 = {
        "ts": "1700000000.000001",
        "text": "parent",
        "reply_count": 2,
        "latest_reply": "1700000000.000003",
    }
    caller2, _ = _tracking_api_caller(
        message_data=[threaded_msg_v2],
        reply_data=[
            {"ts": "1700000000.000001", "thread_ts": "1700000000.000001", "text": "parent", "user": "U999"},
            {"ts": "1700000000.000002", "thread_ts": "1700000000.000001", "text": "my reply", "user": "U001"},
            {"ts": "1700000000.000003", "thread_ts": "1700000000.000001", "text": "new reply", "user": "U999"},
        ],
    )
    run_export(settings, api_caller=caller2)

    # The new reply should be added to relevant_thread_replies
    updated_count = len(rt_replies_path.read_text().strip().splitlines())
    assert updated_count == 2


def test_run_export_deferred_reaction_pass_checks_relevant_threads(temp_output_dir: Path) -> None:
    """The deferred reaction pass fetches replies for the most recent relevant threads."""
    settings = ExporterSettings(
        channels=(ChannelConfig(name=SlackChannelName("general")),),
        default_oldest=datetime(2024, 1, 1, tzinfo=timezone.utc),
        output_dir=temp_output_dir,
        max_recent_threads_for_reactions=5,
        cache_ttl_seconds=0,
    )

    # Run 1: fetch a thread where the user replied (makes it relevant)
    threaded_message = {
        "ts": "1700000000.000001",
        "text": "parent",
        "reply_count": 1,
        "latest_reply": "1700000000.000002",
    }
    reply_with_reaction = [
        {"ts": "1700000000.000001", "thread_ts": "1700000000.000001", "text": "parent", "user": "U999"},
        {
            "ts": "1700000000.000002",
            "thread_ts": "1700000000.000001",
            "text": "reply",
            "user": "U001",
            "reactions": [{"name": "heart", "users": ["U999"], "count": 1}],
        },
    ]
    caller, counts = _tracking_api_caller(
        message_data=[threaded_message],
        reply_data=reply_with_reaction,
    )
    run_export(settings, api_caller=caller)

    # Channel loop fetches replies once, deferred pass fetches them again for reactions
    assert counts.get("conversations.replies", 0) == 2

    # The reaction should be saved by the deferred pass
    reaction_path = temp_output_dir / "reaction" / "created" / "events.jsonl"
    assert reaction_path.exists()
    reaction_lines = reaction_path.read_text().strip().splitlines()
    reaction_records = [json.loads(line) for line in reaction_lines]
    reply_reactions = [r for r in reaction_records if r.get("thread_ts") is not None]
    assert len(reply_reactions) == 1
    assert reply_reactions[0]["raw"]["reactions"][0]["name"] == "heart"


def test_run_export_deferred_reaction_pass_uses_threads_from_previous_runs(temp_output_dir: Path) -> None:
    """The deferred pass checks reactions on relevant threads detected in previous runs."""
    settings = ExporterSettings(
        channels=(ChannelConfig(name=SlackChannelName("general")),),
        default_oldest=datetime(2024, 1, 1, tzinfo=timezone.utc),
        output_dir=temp_output_dir,
        max_recent_threads_for_reactions=5,
        cache_ttl_seconds=0,
    )

    # Run 1: discover a relevant thread (user participated)
    threaded_message = {
        "ts": "1700000000.000001",
        "text": "parent",
        "reply_count": 1,
        "latest_reply": "1700000000.000002",
    }
    caller1, counts1 = _tracking_api_caller(
        message_data=[threaded_message],
        reply_data=[
            {"ts": "1700000000.000001", "thread_ts": "1700000000.000001", "text": "parent", "user": "U999"},
            {"ts": "1700000000.000002", "thread_ts": "1700000000.000001", "text": "reply", "user": "U001"},
        ],
    )
    run_export(settings, api_caller=caller1)
    assert counts1.get("conversations.replies", 0) == 2

    # Run 2: same latest_reply (thread skipped by channel loop), but deferred pass
    # should still re-fetch it from existing_relevant_threads and find the new reaction
    reply_with_reaction = [
        {"ts": "1700000000.000001", "thread_ts": "1700000000.000001", "text": "parent", "user": "U999"},
        {
            "ts": "1700000000.000002",
            "thread_ts": "1700000000.000001",
            "text": "reply",
            "user": "U001",
            "reactions": [{"name": "tada", "users": ["U999"], "count": 1}],
        },
    ]
    caller2, counts2 = _tracking_api_caller(
        message_data=[threaded_message],
        reply_data=reply_with_reaction,
    )
    run_export(settings, api_caller=caller2)

    # Channel loop skips the thread (unchanged latest_reply), deferred pass fetches it
    assert counts2.get("conversations.replies", 0) == 1

    # The new reaction should be saved
    reaction_path = temp_output_dir / "reaction" / "created" / "events.jsonl"
    assert reaction_path.exists()
    reaction_lines = reaction_path.read_text().strip().splitlines()
    reaction_records = [json.loads(line) for line in reaction_lines]
    reply_reactions = [r for r in reaction_records if r.get("thread_ts") is not None]
    assert len(reply_reactions) == 1
    assert reply_reactions[0]["raw"]["reactions"][0]["name"] == "tada"


def test_run_export_recently_active_channels_selects_top_n(temp_output_dir: Path) -> None:
    """--recently-active-channels selects the N channels with the most recent messages."""
    # Save messages with different timestamps to establish activity order
    save_message_events(
        temp_output_dir,
        StreamType.CREATED,
        [
            make_message_event(channel_id="C1", channel_name="old-channel", ts="1600000000.000001"),
            make_message_event(channel_id="C2", channel_name="new-channel", ts="1700000000.000001"),
            make_message_event(channel_id="C3", channel_name="mid-channel", ts="1650000000.000001"),
        ],
    )

    # Second run with --recently-active-channels 2: should pick new-channel and mid-channel
    settings_active = ExporterSettings(
        recently_active_channels=2,
        default_oldest=datetime(2024, 1, 1, tzinfo=timezone.utc),
        output_dir=temp_output_dir,
        max_recent_threads_for_reactions=0,
        cache_ttl_seconds=0,
    )
    caller2, counts2 = _tracking_api_caller(
        channel_data=[
            {"id": "C1", "name": "old-channel", "is_member": True},
            {"id": "C2", "name": "new-channel", "is_member": True},
            {"id": "C3", "name": "mid-channel", "is_member": True},
        ],
    )
    run_export(settings_active, api_caller=caller2)

    # Should have fetched messages for only 2 channels (the most recently active)
    assert counts2.get("conversations.history", 0) == 2


def test_run_export_cached_channels_filtered_by_membership(temp_output_dir: Path) -> None:
    """When members_only=True and cache contains non-member channels, only member channels are used."""
    settings = _make_cached_settings(temp_output_dir)

    # First run fetches channels including a non-member channel
    caller1, counts1 = _tracking_api_caller(
        channel_data=[
            {"id": "C123", "name": "general", "is_member": True},
            {"id": "C456", "name": "external", "is_member": False},
        ],
    )
    # Use channels=None and members_only=False to simulate a previous --all run
    settings_all = ExporterSettings(
        channels=None,
        default_oldest=settings.default_oldest,
        output_dir=settings.output_dir,
        max_recent_threads_for_reactions=settings.max_recent_threads_for_reactions,
        cache_ttl_seconds=settings.cache_ttl_seconds,
        members_only=False,
    )
    run_export(settings_all, api_caller=caller1)
    # Both channels stored, conversations.info called for both
    assert counts1.get("conversations.info", 0) == 2

    # Second run with members_only=True and channels=None so only the membership
    # filter is active (not the channel-name filter)
    counts1.clear()
    settings_members = ExporterSettings(
        channels=None,
        default_oldest=settings.default_oldest,
        output_dir=settings.output_dir,
        max_recent_threads_for_reactions=settings.max_recent_threads_for_reactions,
        cache_ttl_seconds=settings.cache_ttl_seconds,
        members_only=True,
    )
    run_export(settings_members, api_caller=caller1)
    # conversations.info should only be called for the 1 member channel
    assert counts1.get("conversations.info", 0) == 1


def test_run_export_channel_info_only_for_specified_channels(temp_output_dir: Path) -> None:
    """When --channels is specified, conversations.info is only called for those channels."""
    settings = ExporterSettings(
        channels=(ChannelConfig(name=SlackChannelName("general")),),
        default_oldest=datetime(2024, 1, 1, tzinfo=timezone.utc),
        output_dir=temp_output_dir,
        max_recent_threads_for_reactions=0,
        cache_ttl_seconds=0,
    )
    caller, counts = _tracking_api_caller(
        channel_data=[
            {"id": "C123", "name": "general", "is_member": True},
            {"id": "C456", "name": "random", "is_member": True},
            {"id": "C789", "name": "engineering", "is_member": True},
        ],
    )
    run_export(settings, api_caller=caller)

    # conversations.info should only be called for 'general', not all 3 member channels
    assert counts.get("conversations.info", 0) == 1


def test_run_export_all_channels_when_channels_is_none(temp_output_dir: Path) -> None:
    """When channels is None, all channels from the fetched channel list are exported."""
    settings = ExporterSettings(
        channels=None,
        default_oldest=datetime(2024, 1, 1, tzinfo=timezone.utc),
        output_dir=temp_output_dir,
        max_recent_threads_for_reactions=0,
        cache_ttl_seconds=0,
    )
    caller, counts = _tracking_api_caller(
        channel_data=[
            {"id": "C123", "name": "general", "is_member": True},
            {"id": "C456", "name": "random", "is_member": True},
        ],
        message_data=[{"ts": "1700000000.000001", "text": "hello"}],
    )
    run_export(settings, api_caller=caller)

    # Should have fetched messages for both channels (forward fetch only per channel)
    assert counts.get("conversations.history", 0) == 2
    # conversations.info called for both channels (no --channels filter)
    assert counts.get("conversations.info", 0) == 2
