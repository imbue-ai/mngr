import json

import pytest

from imbue.slack_exporter.errors import LatchkeyInvocationError
from imbue.slack_exporter.errors import SlackApiError
from imbue.slack_exporter.latchkey import extract_next_cursor
from imbue.slack_exporter.latchkey import parse_latchkey_response


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
