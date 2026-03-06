import pytest

from imbue.slack_exporter.channels import fetch_channel_list
from imbue.slack_exporter.channels import fetch_user_list
from imbue.slack_exporter.channels import resolve_channel_id
from imbue.slack_exporter.errors import ChannelNotFoundError
from imbue.slack_exporter.primitives import SlackChannelId
from imbue.slack_exporter.primitives import SlackChannelName
from imbue.slack_exporter.primitives import SlackUserId
from imbue.slack_exporter.testing import make_channel_event
from imbue.slack_exporter.testing import make_fake_api_caller
from imbue.slack_exporter.testing import make_slack_response


def test_fetch_channel_list_single_page() -> None:
    api_caller = make_fake_api_caller(
        {
            "conversations.list": [
                make_slack_response(
                    "channels",
                    [
                        {"id": "C123", "name": "general"},
                        {"id": "C456", "name": "random"},
                    ],
                ),
            ],
        }
    )

    channels = fetch_channel_list(api_caller)

    assert len(channels) == 2
    assert channels[0].channel_id == SlackChannelId("C123")
    assert channels[1].channel_id == SlackChannelId("C456")
    assert channels[0].source == "channels"
    assert "event_id" in channels[0].model_dump()


def test_fetch_channel_list_multiple_pages() -> None:
    api_caller = make_fake_api_caller(
        {
            "conversations.list": [
                {
                    "ok": True,
                    "channels": [{"id": "C123", "name": "general"}],
                    "response_metadata": {"next_cursor": "cursor_page2"},
                },
                make_slack_response("channels", [{"id": "C456", "name": "random"}]),
            ],
        }
    )

    channels = fetch_channel_list(api_caller)
    assert len(channels) == 2


def test_fetch_channel_list_empty_response() -> None:
    api_caller = make_fake_api_caller({"conversations.list": [make_slack_response("channels", [])]})
    channels = fetch_channel_list(api_caller)
    assert channels == []


def test_fetch_user_list_single_page() -> None:
    api_caller = make_fake_api_caller(
        {
            "users.list": [
                make_slack_response(
                    "members",
                    [
                        {"id": "U001", "name": "alice"},
                        {"id": "U002", "name": "bob"},
                    ],
                ),
            ],
        }
    )

    users = fetch_user_list(api_caller)

    assert len(users) == 2
    assert users[0].user_id == SlackUserId("U001")
    assert users[0].source == "users"


def test_fetch_user_list_multiple_pages() -> None:
    api_caller = make_fake_api_caller(
        {
            "users.list": [
                {
                    "ok": True,
                    "members": [{"id": "U001", "name": "alice"}],
                    "response_metadata": {"next_cursor": "next"},
                },
                make_slack_response("members", [{"id": "U002", "name": "bob"}]),
            ],
        }
    )

    users = fetch_user_list(api_caller)
    assert len(users) == 2


def test_resolve_channel_id_finds_channel_in_fresh_events() -> None:
    events = [make_channel_event("C123", "general")]
    result = resolve_channel_id(SlackChannelName("general"), events, {})
    assert result == SlackChannelId("C123")


def test_resolve_channel_id_falls_back_to_cached_mapping() -> None:
    cached = {SlackChannelName("general"): SlackChannelId("C999")}
    result = resolve_channel_id(SlackChannelName("general"), [], cached)
    assert result == SlackChannelId("C999")


def test_resolve_channel_id_prefers_fresh_events_over_cache() -> None:
    events = [make_channel_event("C123", "general")]
    cached = {SlackChannelName("general"): SlackChannelId("C999")}
    result = resolve_channel_id(SlackChannelName("general"), events, cached)
    assert result == SlackChannelId("C123")


def test_resolve_channel_id_raises_when_channel_not_found() -> None:
    with pytest.raises(ChannelNotFoundError):
        resolve_channel_id(SlackChannelName("nonexistent"), [], {})
