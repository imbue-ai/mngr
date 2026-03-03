import json

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mng.errors import MngError
from imbue.mng.utils.claude import _extract_text_delta
from imbue.mng.utils.claude import query_claude
from imbue.mng.utils.claude import query_claude_streaming

# -- extract_text_delta tests --


def test_extract_text_delta_valid_event() -> None:
    """A valid content_block_delta event should return the text."""
    event = json.dumps(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "hello"},
            },
        }
    )
    assert _extract_text_delta(event) == "hello"


def test_extract_text_delta_non_delta_event() -> None:
    """Non-delta events should return None."""
    event = json.dumps(
        {
            "type": "stream_event",
            "event": {"type": "content_block_start", "index": 0},
        }
    )
    assert _extract_text_delta(event) is None


def test_extract_text_delta_malformed_json() -> None:
    """Malformed JSON should return None, not raise."""
    assert _extract_text_delta("not valid json {{{") is None


def test_extract_text_delta_non_stream_event() -> None:
    """Events that are not stream_event type should return None."""
    event = json.dumps({"type": "result", "subtype": "success"})
    assert _extract_text_delta(event) is None


def test_extract_text_delta_missing_delta() -> None:
    """content_block_delta without a delta field should return None."""
    event = json.dumps(
        {
            "type": "stream_event",
            "event": {"type": "content_block_delta", "index": 0},
        }
    )
    assert _extract_text_delta(event) is None


def test_extract_text_delta_non_text_delta_type() -> None:
    """A delta with a type other than text_delta should return None."""
    event = json.dumps(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": "{}"},
            },
        }
    )
    assert _extract_text_delta(event) is None


def test_extract_text_delta_empty_string() -> None:
    assert _extract_text_delta("") is None


def test_extract_text_delta_event_field_not_dict() -> None:
    event = json.dumps({"type": "stream_event", "event": "not a dict"})
    assert _extract_text_delta(event) is None


def test_extract_text_delta_delta_field_not_dict() -> None:
    event = json.dumps(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": "not a dict",
            },
        }
    )
    assert _extract_text_delta(event) is None


# -- query_claude tests --


def test_query_claude_returns_none_on_missing_command() -> None:
    """query_claude should return None when the command is not found or fails."""
    with ConcurrencyGroup(name="test-query-claude") as cg:
        result = query_claude(
            prompt="test",
            system_prompt="test",
            cg=cg,
        )
    assert result is None


# -- query_claude_streaming tests --


def test_query_claude_streaming_raises_mng_error_on_failure() -> None:
    """query_claude_streaming should raise MngError when claude fails."""
    with ConcurrencyGroup(name="test-stream-failure") as cg:
        with pytest.raises(MngError):
            list(query_claude_streaming(prompt="test", system_prompt="test", cg=cg))
