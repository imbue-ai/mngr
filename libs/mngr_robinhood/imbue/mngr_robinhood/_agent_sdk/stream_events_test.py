from pathlib import Path

from claude_agent_sdk import StreamEvent

from imbue.mngr_claude.stream_buffer import SnapshotDeltaReader
from imbue.mngr_robinhood._agent_sdk.stream_events import StreamEventSynthesizer


class _FakeBufferHost:
    """Minimal host stand-in whose read_text_file returns a settable buffer."""

    def __init__(self) -> None:
        self.content = ""

    def read_text_file(self, _path: Path) -> str:
        return self.content


class _MissingBufferHost:
    """Host stand-in that raises as if the stream_buffer file does not exist yet."""

    def read_text_file(self, path: Path) -> str:
        raise FileNotFoundError(path)


def _make_synth(host: object) -> StreamEventSynthesizer:
    # model_construct bypasses validation so the lightweight fake host can stand in for the real
    # OnlineHostInterface (the field is SkipValidation; only read_text_file is exercised).
    return StreamEventSynthesizer.model_construct(host=host, buffer_path=Path("/buffer"), reader=SnapshotDeltaReader())


def _event_types(events: list[StreamEvent]) -> list[str]:
    return [event.event["type"] for event in events]


def test_synthesizer_opens_framing_then_emits_text_delta() -> None:
    host = _FakeBufferHost()
    # The complete line is emitted; the still-streaming trailing line is held back.
    host.content = "id\nHello world\nstreaming-tail"
    synth = _make_synth(host)

    events = synth.poll("sess-1", "haiku")

    assert _event_types(events) == ["message_start", "content_block_start", "content_block_delta"]
    assert all(isinstance(event, StreamEvent) for event in events)
    assert all(event.session_id == "sess-1" for event in events)
    assert all(event.uuid != "" for event in events)
    # message_start envelope conforms to claude's shape, with a zeroed usage stub and the caller model.
    # The payload is dumped from anthropic's models, so usage also carries the API's optional
    # cache/detail fields (all null); we assert only the token counts we populate.
    message = events[0].event["message"]
    assert message["role"] == "assistant"
    assert message["model"] == "haiku"
    assert message["content"] == []
    assert message["stop_reason"] is None
    assert message["usage"]["input_tokens"] == 0
    assert message["usage"]["output_tokens"] == 0
    # The delta carries the complete line as a text_delta.
    assert events[2].event == {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": "Hello world\n"},
    }


def test_synthesizer_finalize_flushes_tail_then_close_framing() -> None:
    host = _FakeBufferHost()
    host.content = "id\nHello world\nstreaming-tail"
    synth = _make_synth(host)
    synth.poll("sess-1", "haiku")

    final = synth.finalize("sess-1", "haiku")

    assert _event_types(final) == ["content_block_delta", "content_block_stop", "message_delta", "message_stop"]
    # The held-back tail is delivered exactly once at finalize.
    assert final[0].event["delta"]["text"] == "streaming-tail"
    assert final[2].event["delta"]["stop_reason"] == "end_turn"


def test_synthesizer_subsequent_delta_does_not_reopen_framing() -> None:
    host = _FakeBufferHost()
    synth = _make_synth(host)
    host.content = "id\nline one\nx"
    first = synth.poll("sess-1", "haiku")
    host.content = "id\nline one\nline two\nx"
    second = synth.poll("sess-1", "haiku")

    assert _event_types(first) == ["message_start", "content_block_start", "content_block_delta"]
    # The message is already open, so only a delta follows -- no second message_start.
    assert _event_types(second) == ["content_block_delta"]
    assert second[0].event["delta"]["text"] == "line two\n"


def test_synthesizer_waits_for_session_id_without_losing_text() -> None:
    host = _FakeBufferHost()
    host.content = "id\nHello world\nx"
    synth = _make_synth(host)

    # No session id yet: emit nothing, and do not advance past the buffered text.
    assert synth.poll("", "haiku") == []
    # Once the session id is known, the full delta is emitted.
    events = synth.poll("sess-1", "haiku")
    assert _event_types(events) == ["message_start", "content_block_start", "content_block_delta"]
    assert events[2].event["delta"]["text"] == "Hello world\n"


def test_synthesizer_idle_buffer_emits_nothing() -> None:
    host = _FakeBufferHost()
    # id line only: empty body.
    host.content = "id\n"
    synth = _make_synth(host)

    assert synth.poll("sess-1", "haiku") == []
    # Nothing was opened, so finalize emits no close framing either.
    assert synth.finalize("sess-1", "haiku") == []


def test_synthesizer_tolerates_missing_buffer_file() -> None:
    synth = _make_synth(_MissingBufferHost())
    assert synth.poll("sess-1", "haiku") == []


def test_synthesizer_partial_first_line_is_held_until_complete() -> None:
    host = _FakeBufferHost()
    # A single in-progress line with no newline yet: the volatile line is withheld, so no
    # framing/delta is emitted until a newline completes it.
    host.content = "id\nStill typing this line"
    synth = _make_synth(host)
    assert synth.poll("sess-1", "haiku") == []
