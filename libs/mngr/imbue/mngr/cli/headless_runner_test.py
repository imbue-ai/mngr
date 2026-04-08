import json

import pytest

from imbue.mngr.agents.agent_registry import reset_agent_registry
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.cli.headless_runner import accumulate_chunks
from imbue.mngr.cli.headless_runner import check_streaming_headless_agent_type
from imbue.mngr.cli.headless_runner import stream_or_accumulate_response
from imbue.mngr.config.agent_class_registry import set_default_agent_class
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import OutputFormat

# =============================================================================
# Tests for check_streaming_headless_agent_type
# =============================================================================


def test_check_streaming_headless_agent_type_raises_for_non_streaming() -> None:
    """Non-streaming agent types should be rejected with a clear error."""
    reset_agent_registry()
    set_default_agent_class(BaseAgent)
    with pytest.raises(MngrError, match="does not support streaming headless output"):
        check_streaming_headless_agent_type("headless_claude")


# =============================================================================
# Tests for accumulate_chunks
# =============================================================================


def test_accumulate_chunks_joins_all() -> None:
    chunks = iter(["Hello ", "world", "!"])
    assert accumulate_chunks(chunks) == "Hello world!"


def test_accumulate_chunks_empty() -> None:
    assert accumulate_chunks(iter([])) == ""


def test_accumulate_chunks_single() -> None:
    assert accumulate_chunks(iter(["Hello"])) == "Hello"


# =============================================================================
# Tests for stream_or_accumulate_response
# =============================================================================


def test_stream_or_accumulate_response_human(capsys: pytest.CaptureFixture[str]) -> None:
    """HUMAN format should stream chunks directly to stdout."""
    chunks = iter(["Hello ", "world"])
    stream_or_accumulate_response(chunks, OutputFormat.HUMAN)
    captured = capsys.readouterr()
    assert "Hello world" in captured.out


def test_stream_or_accumulate_response_json(capsys: pytest.CaptureFixture[str]) -> None:
    """JSON format should accumulate and emit as JSON object."""
    chunks = iter(["Hello ", "world"])
    stream_or_accumulate_response(chunks, OutputFormat.JSON)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["response"] == "Hello world"


def test_stream_or_accumulate_response_jsonl(capsys: pytest.CaptureFixture[str]) -> None:
    """JSONL format should accumulate and emit with event field."""
    chunks = iter(["Hello ", "world"])
    stream_or_accumulate_response(chunks, OutputFormat.JSONL)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["event"] == "response"
    assert data["response"] == "Hello world"
