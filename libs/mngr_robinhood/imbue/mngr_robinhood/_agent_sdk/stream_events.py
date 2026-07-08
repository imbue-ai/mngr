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

The text-delta extraction (diffing successive snapshots, holding back the volatile final line) is
delegated to the agent's :class:`LiveOutputReader`; this module only wraps the resulting deltas in
the event framing. The synthesizer is intentionally decoupled from the driver's ``LiveSession`` (it
takes the live ``session_id`` / ``model`` per call) so this module has no import cycle with
``driver``.
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
from imbue.mngr.interfaces.live_output import LiveOutputReader
from imbue.mngr_claude.stream_json import content_block_start_event
from imbue.mngr_claude.stream_json import content_block_stop_event
from imbue.mngr_claude.stream_json import message_delta_event
from imbue.mngr_claude.stream_json import message_start_event
from imbue.mngr_claude.stream_json import message_stop_event
from imbue.mngr_claude.stream_json import text_delta_event

# stop_reason stamped on the synthesized message_delta at clean turn completion. mngr cannot observe
# claude's real stop_reason at stream time; end_turn is the overwhelmingly common terminal value and
# the authoritative ResultMessage carries the real terminal signal.
_SYNTHETIC_STOP_REASON: Final[str] = "end_turn"


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
    reader: LiveOutputReader = Field(description="Extracts text deltas from successive buffer snapshots")
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
        return self._wrap_deltas(self.reader.feed(content), session_id, model)

    def finalize(self, session_id: str, model: str) -> list[StreamEvent]:
        """Emit the held-back final delta plus closing framing for a cleanly-completed turn."""
        if not session_id:
            return []
        events = self._wrap_deltas(self.reader.finalize(), session_id, model)
        events.extend(self._close_framing(session_id))
        return events

    def _wrap_deltas(self, deltas: list[str], session_id: str, model: str) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        for delta in deltas:
            if not self.is_message_open:
                events.extend(self._open_framing(session_id, model))
            events.append(self._event(session_id, text_delta_event(delta)))
        return events

    def _open_framing(self, session_id: str, model: str) -> list[StreamEvent]:
        self.is_message_open = True
        self.message_id = str(uuid4())
        return [
            self._event(session_id, message_start_event(self.message_id, model)),
            self._event(session_id, content_block_start_event()),
        ]

    def _close_framing(self, session_id: str) -> list[StreamEvent]:
        if not self.is_message_open:
            return []
        self.is_message_open = False
        self.message_id = ""
        return [
            self._event(session_id, content_block_stop_event()),
            self._event(session_id, message_delta_event(_SYNTHETIC_STOP_REASON)),
            self._event(session_id, message_stop_event()),
        ]

    def _event(self, session_id: str, payload: dict[str, Any]) -> StreamEvent:
        return StreamEvent(uuid=str(uuid4()), session_id=session_id, event=payload)
