"""Synthesize claude-native partial-message ``StreamEvent``s from approximate stream_buffer deltas.

The mngr transport has no access to claude's real token-level stream; the on-host tmux watcher only
reconstructs approximate assistant text into a ``stream_buffer`` file. When the caller sets
``include_partial_messages``, this module wraps the successive text deltas (computed by
:func:`stream_buffer.compute_stream_delta`) in the SAME event sequence the real SDK emits --
``message_start`` -> ``content_block_start`` -> ``content_block_delta``(``text_delta``)* ->
``content_block_stop`` -> ``message_delta`` -> ``message_stop`` -- as ``StreamEvent`` objects. The
event *shapes* conform to claude's native stream; only the text content is approximate (the same
approximation the CLI streaming already ships). ``usage`` is zeroed -- the authoritative usage and
``total_cost_usd`` stay on the transcript-derived ``ResultMessage``.

The synthesizer is intentionally decoupled from the driver's ``LiveSession`` (it takes the live
``session_id`` / ``model`` per call) so this module has no import cycle with ``driver``.
"""

from pathlib import Path
from typing import Any
from typing import Final
from uuid import uuid4

from claude_agent_sdk import StreamEvent
from pydantic import ConfigDict
from pydantic import Field
from pydantic import SkipValidation

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr_robinhood.stream_buffer import buffer_body
from imbue.mngr_robinhood.stream_buffer import compute_stream_delta

# We reconstruct a single text content block per streamed message; tool-call and reasoning blocks
# never reach the stream_buffer, so index 0 always refers to the assistant-text block.
_BLOCK_INDEX: Final[int] = 0

# stop_reason stamped on the synthesized message_delta at clean turn completion. mngr cannot observe
# claude's real stop_reason at stream time; end_turn is the overwhelmingly common terminal value and
# the authoritative ResultMessage carries the real terminal signal.
_SYNTHETIC_STOP_REASON: Final[str] = "end_turn"


def _zeroed_usage() -> dict[str, Any]:
    """A zeroed usage stub: stream-time token counts are unknown on the mngr transport."""
    return {"input_tokens": 0, "output_tokens": 0}


class StreamEventSynthesizer(MutableModel):
    """Turns successive ``stream_buffer`` snapshots into an ordered claude-native StreamEvent sequence.

    Stateful within a turn: tracks how much body text has already been wrapped as deltas and whether
    the message framing (``message_start`` / ``content_block_start``) is open. The driver calls
    :meth:`poll` each drain tick and :meth:`finalize` once at a clean turn completion (close framing
    is emitted only on clean completion; an interrupt leaves the sequence unterminated, mirroring the
    transport).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    host: SkipValidation[OnlineHostInterface] = Field(description="Host to read the stream_buffer file from")
    buffer_path: Path = Field(description="Absolute path to the agent's stream_buffer file")
    emitted_body: str = Field(default="", description="Body text already wrapped as content_block_delta events")
    last_content: str = Field(default="", description="Most recent non-empty buffer snapshot (for the final flush)")
    is_message_open: bool = Field(default=False, description="Whether message_start framing has been emitted")
    message_id: str = Field(default="", description="Synthesized id of the in-progress message, when open")

    def poll(self, session_id: str, model: str) -> list[StreamEvent]:
        """Read the current buffer snapshot and wrap any new delta as ordered StreamEvents.

        Returns ``[]`` until ``session_id`` is known (a StreamEvent must carry a non-empty session
        id); the still-buffered text is not lost -- the next poll re-diffs the full snapshot.
        """
        if not session_id:
            return []
        try:
            content = self.host.read_text_file(self.buffer_path)
        except (FileNotFoundError, OSError, MngrError):
            # The buffer may not exist yet (watcher still starting up); benign.
            return []
        if buffer_body(content).strip():
            self.last_content = content
        return self._wrap_delta(content, session_id, model, is_flush=False)

    def finalize(self, session_id: str, model: str) -> list[StreamEvent]:
        """Emit the held-back final delta plus closing framing for a cleanly-completed turn."""
        if not session_id:
            return []
        events = self._wrap_delta(self.last_content, session_id, model, is_flush=True)
        events.extend(self._close_framing(session_id))
        return events

    def _wrap_delta(self, content: str, session_id: str, model: str, is_flush: bool) -> list[StreamEvent]:
        delta, self.emitted_body = compute_stream_delta(content, self.emitted_body, is_flush)
        if not delta:
            return []
        events: list[StreamEvent] = []
        if not self.is_message_open:
            events.extend(self._open_framing(session_id, model))
        events.append(self._event(session_id, _content_block_delta_payload(delta)))
        return events

    def _open_framing(self, session_id: str, model: str) -> list[StreamEvent]:
        self.is_message_open = True
        self.message_id = str(uuid4())
        return [
            self._event(session_id, _message_start_payload(self.message_id, model)),
            self._event(session_id, _content_block_start_payload()),
        ]

    def _close_framing(self, session_id: str) -> list[StreamEvent]:
        if not self.is_message_open:
            return []
        self.is_message_open = False
        self.message_id = ""
        return [
            self._event(session_id, _content_block_stop_payload()),
            self._event(session_id, _message_delta_payload()),
            self._event(session_id, _message_stop_payload()),
        ]

    def _event(self, session_id: str, payload: dict[str, Any]) -> StreamEvent:
        return StreamEvent(uuid=str(uuid4()), session_id=session_id, event=payload)


def _message_start_payload(message_id: str, model: str) -> dict[str, Any]:
    return {
        "type": "message_start",
        "message": {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": _zeroed_usage(),
        },
    }


def _content_block_start_payload() -> dict[str, Any]:
    return {"type": "content_block_start", "index": _BLOCK_INDEX, "content_block": {"type": "text", "text": ""}}


def _content_block_delta_payload(text: str) -> dict[str, Any]:
    return {"type": "content_block_delta", "index": _BLOCK_INDEX, "delta": {"type": "text_delta", "text": text}}


def _content_block_stop_payload() -> dict[str, Any]:
    return {"type": "content_block_stop", "index": _BLOCK_INDEX}


def _message_delta_payload() -> dict[str, Any]:
    return {
        "type": "message_delta",
        "delta": {"stop_reason": _SYNTHETIC_STOP_REASON, "stop_sequence": None},
        "usage": _zeroed_usage(),
    }


def _message_stop_payload() -> dict[str, Any]:
    return {"type": "message_stop"}
