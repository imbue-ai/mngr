import pytest

from imbue.slack_exporter.channels import fetch_channel_info
from imbue.slack_exporter.channels import fetch_channel_list
from imbue.slack_exporter.channels import fetch_self_identity
from imbue.slack_exporter.channels import fetch_user_list
from imbue.slack_exporter.channels import resolve_channel_id
from imbue.slack_exporter.errors import ChannelNotFoundError
from imbue.slack_exporter.primitives import SlackChannelId
from imbue.slack_exporter.primitives import SlackChannelName
from imbue.slack_exporter.primitives import SlackMessageTimestamp
from imbue.slack_exporter.primitives import SlackUserId
from imbue.slack_exporter.primitives import SlackUserName
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
                        {"id": "C123", "name": "general", "is_member": True},
                        {"id": "C456", "name": "random", "is_member": True},
                    ],
                ),
            ],
        }
    )

    channels = fetch_channel_list(api_caller)

    assert len(channels) == 2
    assert channels[0].channel_id == SlackChannelId("C123")
    assert channels[1].channel_id == SlackChannelId("C456")
    assert channels[0].source == "slack"
    assert "event_id" in channels[0].model_dump()


def test_fetch_channel_list_filters_non_member_channels_by_default() -> None:
    api_caller = make_fake_api_caller(
        {
            "conversations.list": [
                make_slack_response(
                    "channels",
                    [
                        {"id": "C123", "name": "general", "is_member": True},
                        {"id": "C456", "name": "random", "is_member": False},
                        {"id": "C789", "name": "private", "is_member": True},
                    ],
                ),
            ],
        }
    )

    channels = fetch_channel_list(api_caller)

    assert len(channels) == 2
    assert channels[0].channel_id == SlackChannelId("C123")
    assert channels[1].channel_id == SlackChannelId("C789")


def test_fetch_channel_list_includes_all_channels_when_members_only_false() -> None:
    api_caller = make_fake_api_caller(
        {
            "conversations.list": [
                make_slack_response(
                    "channels",
                    [
                        {"id": "C123", "name": "general", "is_member": True},
                        {"id": "C456", "name": "random", "is_member": False},
                    ],
                ),
            ],
        }
    )

    channels = fetch_channel_list(api_caller, members_only=False)

    assert len(channels) == 2


def test_fetch_channel_list_multiple_pages() -> None:
    api_caller = make_fake_api_caller(
        {
            "conversations.list": [
                {
                    "ok": True,
                    "channels": [{"id": "C123", "name": "general", "is_member": True}],
                    "response_metadata": {"next_cursor": "cursor_page2"},
                },
                make_slack_response("channels", [{"id": "C456", "name": "random", "is_member": True}]),
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
    assert users[0].source == "slack"


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


def test_fetch_self_identity_returns_event() -> None:
    api_caller = make_fake_api_caller(
        {
            "auth.test": [{"ok": True, "user_id": "U001", "user": "alice", "team": "test", "team_id": "T001"}],
        }
    )

    event = fetch_self_identity(api_caller)

    assert event.user_id == SlackUserId("U001")
    assert event.user_name == SlackUserName("alice")
    assert event.source == "slack"
    assert event.raw["team"] == "test"


def test_fetch_unread_markers_from_conversations_info() -> None:
    channels = [
        make_channel_event("C123", "general"),
        make_channel_event("C456", "random"),
    ]
    api_caller = make_fake_api_caller(
        {
            "conversations.info": [
                {"ok": True, "channel": {"id": "C123", "name": "general", "last_read": "1700000000.000001"}},
                {"ok": True, "channel": {"id": "C456", "name": "random", "last_read": "1700000000.000099"}},
            ],
        }
    )

    result = fetch_channel_info(api_caller, channels)

    assert len(result.unread_markers) == 2
    assert result.unread_markers[0].channel_id == SlackChannelId("C123")
    assert result.unread_markers[0].last_read_ts == SlackMessageTimestamp("1700000000.000001")
    assert result.unread_markers[0].source == "slack"
    assert result.unread_markers[1].last_read_ts == SlackMessageTimestamp("1700000000.000099")
    assert len(result.updated_channels) == 2


def test_fetch_channel_info_skips_channels_without_last_read() -> None:
    channels = [
        make_channel_event("C123", "general"),
        make_channel_event("C456", "random"),
    ]
    api_caller = make_fake_api_caller(
        {
            "conversations.info": [
                {"ok": True, "channel": {"id": "C123", "name": "general", "last_read": "1700000000.000001"}},
                {"ok": True, "channel": {"id": "C456", "name": "random"}},
            ],
        }
    )

    result = fetch_channel_info(api_caller, channels)

    assert len(result.unread_markers) == 1
    assert result.unread_markers[0].channel_id == SlackChannelId("C123")
