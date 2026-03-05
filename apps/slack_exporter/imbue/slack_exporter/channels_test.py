from typing import Any

import pytest

from imbue.slack_exporter.channels import fetch_channel_list
from imbue.slack_exporter.channels import fetch_user_list
from imbue.slack_exporter.channels import resolve_channel_id
from imbue.slack_exporter.data_types import SlackApiCaller
from imbue.slack_exporter.errors import ChannelNotFoundError
from imbue.slack_exporter.primitives import SlackChannelId
from imbue.slack_exporter.primitives import SlackChannelName
from imbue.slack_exporter.primitives import SlackUserId
from imbue.slack_exporter.testing import make_stored_channel_info


def _make_paginated_api_caller(pages: list[dict[str, Any]]) -> SlackApiCaller:
    """Create a fake api caller that returns pages in order."""
    call_idx = 0

    def fake_caller(method: str, query_params: dict[str, str] | None = None) -> dict[str, Any]:
        nonlocal call_idx
        result = pages[call_idx]
        call_idx += 1
        return result

    return fake_caller


def test_fetch_channel_list_single_page() -> None:
    api_caller = _make_paginated_api_caller(
        [
            {
                "ok": True,
                "channels": [
                    {"id": "C123", "name": "general"},
                    {"id": "C456", "name": "random"},
                ],
                "response_metadata": {"next_cursor": ""},
            },
        ]
    )

    channels = fetch_channel_list(api_caller)

    assert len(channels) == 2
    assert channels[0].channel_id == SlackChannelId("C123")
    assert channels[1].channel_id == SlackChannelId("C456")


def test_fetch_channel_list_multiple_pages() -> None:
    api_caller = _make_paginated_api_caller(
        [
            {
                "ok": True,
                "channels": [{"id": "C123", "name": "general"}],
                "response_metadata": {"next_cursor": "cursor_page2"},
            },
            {
                "ok": True,
                "channels": [{"id": "C456", "name": "random"}],
                "response_metadata": {"next_cursor": ""},
            },
        ]
    )

    channels = fetch_channel_list(api_caller)
    assert len(channels) == 2


def test_fetch_channel_list_empty_response() -> None:
    api_caller = _make_paginated_api_caller([{"ok": True, "channels": [], "response_metadata": {"next_cursor": ""}}])
    channels = fetch_channel_list(api_caller)
    assert channels == []


def test_fetch_user_list_single_page() -> None:
    api_caller = _make_paginated_api_caller(
        [
            {
                "ok": True,
                "members": [
                    {"id": "U001", "name": "alice"},
                    {"id": "U002", "name": "bob"},
                ],
                "response_metadata": {"next_cursor": ""},
            },
        ]
    )

    users = fetch_user_list(api_caller)

    assert len(users) == 2
    assert users[0].user_id == SlackUserId("U001")
    assert users[1].user_id == SlackUserId("U002")


def test_fetch_user_list_multiple_pages() -> None:
    api_caller = _make_paginated_api_caller(
        [
            {
                "ok": True,
                "members": [{"id": "U001", "name": "alice"}],
                "response_metadata": {"next_cursor": "next"},
            },
            {
                "ok": True,
                "members": [{"id": "U002", "name": "bob"}],
                "response_metadata": {"next_cursor": ""},
            },
        ]
    )

    users = fetch_user_list(api_caller)
    assert len(users) == 2


def test_resolve_channel_id_finds_channel_in_fresh_info() -> None:
    info = [make_stored_channel_info("C123", "general")]
    result = resolve_channel_id(SlackChannelName("general"), info, {})
    assert result == SlackChannelId("C123")


def test_resolve_channel_id_falls_back_to_cached_mapping() -> None:
    cached = {SlackChannelName("general"): SlackChannelId("C999")}
    result = resolve_channel_id(SlackChannelName("general"), [], cached)
    assert result == SlackChannelId("C999")


def test_resolve_channel_id_prefers_fresh_info_over_cache() -> None:
    info = [make_stored_channel_info("C123", "general")]
    cached = {SlackChannelName("general"): SlackChannelId("C999")}
    result = resolve_channel_id(SlackChannelName("general"), info, cached)
    assert result == SlackChannelId("C123")


def test_resolve_channel_id_raises_when_channel_not_found() -> None:
    with pytest.raises(ChannelNotFoundError):
        resolve_channel_id(SlackChannelName("nonexistent"), [], {})
