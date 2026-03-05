import json
from datetime import datetime
from datetime import timezone
from pathlib import Path
from unittest.mock import patch

from imbue.slack_exporter.data_types import ChannelConfig
from imbue.slack_exporter.data_types import ExporterSettings
from imbue.slack_exporter.data_types import StoredMessage
from imbue.slack_exporter.exporter import _datetime_to_slack_timestamp
from imbue.slack_exporter.exporter import _fetch_all_messages_for_channel
from imbue.slack_exporter.exporter import run_export
from imbue.slack_exporter.primitives import SlackChannelId
from imbue.slack_exporter.primitives import SlackChannelName
from imbue.slack_exporter.primitives import SlackMessageTimestamp

_NOW = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def _make_slack_history_response(
    messages: list[dict[str, str]],
    has_more: bool = False,
    next_cursor: str = "",
) -> dict[str, object]:
    response: dict[str, object] = {
        "ok": True,
        "messages": messages,
        "has_more": has_more,
    }
    if next_cursor:
        response["response_metadata"] = {"next_cursor": next_cursor}
    return response


def _make_slack_channel_list_response(
    channels: list[dict[str, str]],
) -> dict[str, object]:
    return {
        "ok": True,
        "channels": channels,
        "response_metadata": {"next_cursor": ""},
    }


def _make_mock_call_slack_api(
    channel_list_response: dict[str, object],
    history_response: dict[str, object],
) -> object:
    def mock_call_slack_api(method: str, query_params: dict[str, str] | None = None) -> dict[str, object]:
        if method == "conversations.list":
            return channel_list_response
        elif method == "conversations.history":
            return history_response
        else:
            raise AssertionError(f"Unexpected method: {method}")

    return mock_call_slack_api


def test_datetime_to_slack_timestamp_converts_correctly() -> None:
    dt = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    result = _datetime_to_slack_timestamp(dt)
    assert result == SlackMessageTimestamp("1704067200.000000")


def test_datetime_to_slack_timestamp_preserves_fractional_seconds() -> None:
    dt = datetime(2024, 6, 15, 10, 30, 45, 123456, tzinfo=timezone.utc)
    result = _datetime_to_slack_timestamp(dt)
    assert "." in result


def test_fetch_all_messages_fetches_single_page() -> None:
    response = _make_slack_history_response(
        messages=[{"ts": "1700000000.000001", "text": "hello"}],
    )

    with patch("imbue.slack_exporter.exporter.call_slack_api", return_value=response):
        messages = _fetch_all_messages_for_channel(
            channel_id=SlackChannelId("C123"),
            channel_name=SlackChannelName("general"),
            oldest_ts=SlackMessageTimestamp("1699999999.000000"),
            is_inclusive=True,
        )

    assert len(messages) == 1
    assert messages[0].timestamp == SlackMessageTimestamp("1700000000.000001")
    assert messages[0].channel_id == SlackChannelId("C123")


def test_fetch_all_messages_handles_pagination() -> None:
    page1 = _make_slack_history_response(
        messages=[{"ts": "1700000000.000001", "text": "first"}],
        has_more=True,
        next_cursor="cursor_abc",
    )
    page2 = _make_slack_history_response(
        messages=[{"ts": "1700000000.000002", "text": "second"}],
    )

    with patch("imbue.slack_exporter.exporter.call_slack_api", side_effect=[page1, page2]):
        messages = _fetch_all_messages_for_channel(
            channel_id=SlackChannelId("C123"),
            channel_name=SlackChannelName("general"),
            oldest_ts=SlackMessageTimestamp("1699999999.000000"),
            is_inclusive=True,
        )

    assert len(messages) == 2


def test_fetch_all_messages_skips_messages_without_ts() -> None:
    response = _make_slack_history_response(
        messages=[{"text": "no timestamp"}, {"ts": "1700000000.000001", "text": "has ts"}],
    )

    with patch("imbue.slack_exporter.exporter.call_slack_api", return_value=response):
        messages = _fetch_all_messages_for_channel(
            channel_id=SlackChannelId("C123"),
            channel_name=SlackChannelName("general"),
            oldest_ts=SlackMessageTimestamp("1699999999.000000"),
            is_inclusive=True,
        )

    assert len(messages) == 1


def test_fetch_all_messages_returns_empty_when_no_messages() -> None:
    response = _make_slack_history_response(messages=[])

    with patch("imbue.slack_exporter.exporter.call_slack_api", return_value=response):
        messages = _fetch_all_messages_for_channel(
            channel_id=SlackChannelId("C123"),
            channel_name=SlackChannelName("general"),
            oldest_ts=SlackMessageTimestamp("1699999999.000000"),
            is_inclusive=True,
        )

    assert messages == []


def test_run_export_writes_messages_to_file(temp_output_path: Path) -> None:
    channel_list_response = _make_slack_channel_list_response(
        channels=[{"id": "C123", "name": "general"}],
    )
    history_response = _make_slack_history_response(
        messages=[{"ts": "1700000000.000001", "text": "hello"}],
    )
    mock_api = _make_mock_call_slack_api(channel_list_response, history_response)

    settings = ExporterSettings(
        channels=(ChannelConfig(name=SlackChannelName("general")),),
        default_oldest=datetime(2024, 1, 1, tzinfo=timezone.utc),
        output_path=temp_output_path,
    )

    with patch("imbue.slack_exporter.exporter.call_slack_api", side_effect=mock_api):
        with patch("imbue.slack_exporter.channels.call_slack_api", side_effect=mock_api):
            run_export(settings)

    lines = temp_output_path.read_text().strip().splitlines()
    assert len(lines) >= 2

    message_lines = [json.loads(line) for line in lines if json.loads(line).get("kind") == "MESSAGE"]
    assert len(message_lines) == 1
    assert message_lines[0]["channel_id"] == "C123"


def test_run_export_incremental_resumes_from_latest(temp_output_path: Path) -> None:
    existing_msg = StoredMessage(
        channel_id=SlackChannelId("C123"),
        channel_name=SlackChannelName("general"),
        timestamp=SlackMessageTimestamp("1700000000.000001"),
        fetched_at=_NOW,
        raw={"ts": "1700000000.000001", "text": "old"},
    )
    temp_output_path.write_text(existing_msg.model_dump_json() + "\n")

    channel_list_response = _make_slack_channel_list_response(
        channels=[{"id": "C123", "name": "general"}],
    )
    history_response = _make_slack_history_response(
        messages=[{"ts": "1700000000.000009", "text": "new"}],
    )

    def mock_call_slack_api(method: str, query_params: dict[str, str] | None = None) -> dict[str, object]:
        if method == "conversations.list":
            return channel_list_response
        elif method == "conversations.history":
            assert query_params is not None
            assert query_params.get("oldest") == "1700000000.000001"
            assert query_params.get("inclusive") == "false"
            return history_response
        else:
            raise AssertionError(f"Unexpected method: {method}")

    settings = ExporterSettings(
        channels=(ChannelConfig(name=SlackChannelName("general")),),
        default_oldest=datetime(2024, 1, 1, tzinfo=timezone.utc),
        output_path=temp_output_path,
    )

    with patch("imbue.slack_exporter.exporter.call_slack_api", side_effect=mock_call_slack_api):
        with patch("imbue.slack_exporter.channels.call_slack_api", side_effect=mock_call_slack_api):
            run_export(settings)

    lines = temp_output_path.read_text().strip().splitlines()
    assert len(lines) >= 3
