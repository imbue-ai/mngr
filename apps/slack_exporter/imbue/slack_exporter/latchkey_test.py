import json
from typing import Any

import pytest

from imbue.slack_exporter.errors import LatchkeyInvocationError
from imbue.slack_exporter.errors import SlackApiError
from imbue.slack_exporter.latchkey import extract_next_cursor
from imbue.slack_exporter.latchkey import fetch_paginated
from imbue.slack_exporter.latchkey import parse_latchkey_response
from imbue.slack_exporter.testing import make_fake_api_caller
from imbue.slack_exporter.testing import make_slack_response


def test_parse_latchkey_response_returns_data_on_success() -> None:
    response_data = {"ok": True, "channels": []}
    result = parse_latchkey_response(
        command_str="latchkey curl url",
        method="conversations.list",
        return_code=0,
        stdout=json.dumps(response_data),
        stderr="",
    )
    assert result == response_data


def test_parse_latchkey_response_raises_on_nonzero_exit() -> None:
    with pytest.raises(LatchkeyInvocationError, match="exit 1"):
        parse_latchkey_response(
            command_str="latchkey curl url",
            method="conversations.list",
            return_code=1,
            stdout="",
            stderr="auth failed",
        )


def test_parse_latchkey_response_raises_on_invalid_json() -> None:
    with pytest.raises(LatchkeyInvocationError, match="Invalid JSON"):
        parse_latchkey_response(
            command_str="latchkey curl url",
            method="conversations.list",
            return_code=0,
            stdout="not json",
            stderr="",
        )


def test_parse_latchkey_response_raises_on_slack_api_error() -> None:
    response_data = {"ok": False, "error": "channel_not_found"}
    with pytest.raises(SlackApiError, match="channel_not_found"):
        parse_latchkey_response(
            command_str="latchkey curl url",
            method="conversations.history",
            return_code=0,
            stdout=json.dumps(response_data),
            stderr="",
        )


def test_parse_latchkey_response_raises_on_missing_ok() -> None:
    with pytest.raises(SlackApiError, match="unknown"):
        parse_latchkey_response(
            command_str="latchkey curl url",
            method="conversations.list",
            return_code=0,
            stdout=json.dumps({"channels": []}),
            stderr="",
        )


def test_extract_next_cursor_returns_cursor_when_present() -> None:
    data = {"response_metadata": {"next_cursor": "abc123"}}
    assert extract_next_cursor(data) == "abc123"


def test_extract_next_cursor_returns_none_when_empty() -> None:
    data = {"response_metadata": {"next_cursor": ""}}
    assert extract_next_cursor(data) is None


def test_extract_next_cursor_returns_none_when_missing_metadata() -> None:
    data: dict[str, object] = {"ok": True}
    assert extract_next_cursor(data) is None


def test_extract_next_cursor_returns_none_when_metadata_is_not_dict() -> None:
    data: dict[str, object] = {"response_metadata": "not a dict"}
    assert extract_next_cursor(data) is None


def test_fetch_paginated_single_page_with_has_more_false() -> None:
    api_caller = make_fake_api_caller({"conversations.history": [make_slack_response("messages", [{"ts": "1"}])]})
    items = fetch_paginated(api_caller, "conversations.history", {"channel": "C1"}, "messages")
    assert items == [{"ts": "1"}]


def test_fetch_paginated_single_page_cursor_only() -> None:
    api_caller = make_fake_api_caller(
        {
            "conversations.list": [
                {"ok": True, "channels": [{"id": "C1"}], "response_metadata": {"next_cursor": ""}},
            ]
        }
    )
    items = fetch_paginated(api_caller, "conversations.list", {}, "channels")
    assert items == [{"id": "C1"}]


def test_fetch_paginated_multiple_pages_cursor_only() -> None:
    api_caller = make_fake_api_caller(
        {
            "conversations.list": [
                {"ok": True, "channels": [{"id": "C1"}], "response_metadata": {"next_cursor": "page2"}},
                {"ok": True, "channels": [{"id": "C2"}], "response_metadata": {"next_cursor": "page3"}},
                {"ok": True, "channels": [{"id": "C3"}], "response_metadata": {"next_cursor": ""}},
            ]
        }
    )
    items = fetch_paginated(api_caller, "conversations.list", {}, "channels")
    assert items == [{"id": "C1"}, {"id": "C2"}, {"id": "C3"}]


def test_fetch_paginated_multiple_pages_has_more_and_cursor() -> None:
    api_caller = make_fake_api_caller(
        {
            "conversations.history": [
                make_slack_response("messages", [{"ts": "1"}], has_more=True, next_cursor="c2"),
                make_slack_response("messages", [{"ts": "2"}], has_more=True, next_cursor="c3"),
                make_slack_response("messages", [{"ts": "3"}]),
            ]
        }
    )
    items = fetch_paginated(api_caller, "conversations.history", {}, "messages")
    assert items == [{"ts": "1"}, {"ts": "2"}, {"ts": "3"}]


def test_fetch_paginated_passes_cursor_in_params() -> None:
    captured_params: list[dict[str, str] | None] = []

    def tracking_caller(method: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        captured_params.append(params)
        if len(captured_params) == 1:
            return {"ok": True, "items": [{"id": "1"}], "response_metadata": {"next_cursor": "abc"}}
        return make_slack_response("items", [{"id": "2"}])

    items = fetch_paginated(tracking_caller, "test.method", {"limit": "100"}, "items")
    assert items == [{"id": "1"}, {"id": "2"}]
    assert len(captured_params) == 2
    assert captured_params[0] == {"limit": "100"}
    assert captured_params[1] == {"limit": "100", "cursor": "abc"}


def test_fetch_paginated_no_response_metadata_stops() -> None:
    api_caller = make_fake_api_caller({"test.method": [{"ok": True, "items": [{"id": "1"}]}]})
    items = fetch_paginated(api_caller, "test.method", {}, "items")
    assert items == [{"id": "1"}]
