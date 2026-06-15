import json
import time
from typing import Any
from typing import Final
from typing import assert_never

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mngr_claude.stream_json import build_assistant_message
from imbue.mngr_claude.stream_json import text_delta_event
from imbue.mngr_claude.stream_json import wrap_stream_event
from imbue.mngr_robinhood.data_types import OutputFormat
from imbue.mngr_robinhood.data_types import ResultMeta

# Hard-coded model identifier used in the synthesized stream-json system/init
# envelope. mngr does not know which model the spawned agent picked, so we
# emit ``unknown`` rather than guessing.
_PLACEHOLDER_MODEL: Final[str] = "unknown"


# Fallback string used in the `result` field when an error is reported
# without a specific message. claude -p's native envelope always carries
# a string here, so we never emit JSON null.
_UNKNOWN_ERROR_TEXT: Final[str] = "unknown error"


@pure
def build_result_envelope(
    text: str,
    meta: ResultMeta,
    turn_count: int,
) -> dict[str, Any]:
    """Synthesize a ``{"type": "result", ...}`` envelope matching claude -p's shape.

    Fields mngr cannot observe (cost, token usage, model breakdown, ...) are
    zeroed or set to None. Consumers that parse this JSON should treat those
    as best-effort.
    """
    subtype = "error" if meta.is_error else "success"
    if meta.is_error:
        result_text = meta.error_text if meta.error_text is not None else _UNKNOWN_ERROR_TEXT
    else:
        result_text = text
    return {
        "type": "result",
        "subtype": subtype,
        "is_error": meta.is_error,
        "api_error_status": None,
        "duration_ms": meta.duration_ms,
        "duration_api_ms": 0,
        "num_turns": turn_count,
        "result": result_text,
        "stop_reason": "end_turn",
        "session_id": meta.session_id,
        "total_cost_usd": 0.0,
        "usage": None,
        "modelUsage": {},
        "permission_denials": [],
        "terminal_reason": "error" if meta.is_error else "completed",
    }


@pure
def build_system_init_envelope(session_id: str) -> dict[str, Any]:
    """Synthesize a ``{"type": "system", "subtype": "init", ...}`` envelope.

    Used as the first line of ``--output-format=stream-json`` output. mngr
    does not have visibility into claude's per-agent tool list / MCP servers
    at wrapper level, so most fields are blank.
    """
    return {
        "type": "system",
        "subtype": "init",
        "cwd": "",
        "session_id": session_id,
        "tools": [],
        "mcp_servers": [],
        "model": _PLACEHOLDER_MODEL,
        "permissionMode": "bypassPermissions",
        "apiKeySource": "mngr",
    }


@pure
def transcript_event_to_stream_json(event: dict[str, Any], session_id: str) -> dict[str, Any] | None:
    """Convert one common-transcript event to a claude stream-json line.

    Returns the synthesized stream-json dict, or ``None`` for events we drop
    (anything that isn't a user/assistant/tool_result message).
    """
    event_type = event.get("type")
    if event_type == "assistant_message":
        return _assistant_event_to_stream_json(event, session_id)
    if event_type == "user_message":
        return _user_event_to_stream_json(event, session_id)
    if event_type == "tool_result":
        return _tool_result_event_to_stream_json(event, session_id)
    return None


def _assistant_event_to_stream_json(event: dict[str, Any], session_id: str) -> dict[str, Any]:
    text = _coerce_str(event.get("text"))
    tool_calls: list[dict[str, Any]] = _coerce_dict_list(event.get("tool_calls"))
    content_blocks: list[dict[str, Any]] = []
    if text:
        content_blocks.append({"type": "text", "text": text})
    for call in tool_calls:
        content_blocks.append(
            {
                "type": "tool_use",
                "id": _coerce_str(call.get("tool_call_id", "")),
                "name": _coerce_str(call.get("tool_name", "")),
                "input": _parse_input_preview(_coerce_str(call.get("input_preview", ""))),
            }
        )
    # The inner `message` is the Anthropic-API Message; build it through the shared typed boundary
    # so its shape is owned upstream. The outer `{"type":"assistant",...,"session_id":...}` is the
    # claude-CLI wrapper and stays a plain dict.
    usage = event.get("usage")
    return {
        "type": "assistant",
        "message": build_assistant_message(
            message_id=_coerce_str(event.get("message_uuid", "")),
            model=_coerce_str(event.get("model", _PLACEHOLDER_MODEL)) or _PLACEHOLDER_MODEL,
            content=content_blocks,
            stop_reason=event.get("stop_reason") if isinstance(event.get("stop_reason"), str) else None,
            usage=usage if isinstance(usage, dict) else None,
        ),
        "session_id": session_id,
    }


def _user_event_to_stream_json(event: dict[str, Any], session_id: str) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": _coerce_str(event.get("content", "")),
        },
        "session_id": session_id,
    }


def _tool_result_event_to_stream_json(event: dict[str, Any], session_id: str) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": event.get("tool_call_id", ""),
                    "content": event.get("output", ""),
                    "is_error": bool(event.get("is_error", False)),
                }
            ],
        },
        "session_id": session_id,
    }


@pure
def _coerce_dict_list(value: object) -> list[dict[str, Any]]:
    """Return ``value`` if it is a list of dicts; otherwise an empty list."""
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            out.append({str(key): val for key, val in item.items()})
    return out


@pure
def _coerce_str(value: object) -> str:
    """Return ``value`` if it is a string; otherwise the empty string."""
    if isinstance(value, str):
        return value
    return ""


def _parse_input_preview(preview: str) -> object:
    """Best-effort parse of the tool input preview into structured JSON.

    The common transcript stores a JSON-encoded preview that may have been
    truncated; if it isn't parseable, we surface it as a string so the
    consumer still sees something.
    """
    if preview == "":
        return {}
    try:
        return json.loads(preview)
    except json.JSONDecodeError as exc:
        logger.warning("Failed to parse tool input preview as JSON ({}): {!r}", exc.msg, preview)
        return preview


class StreamingOutputWriter(MutableModel):
    """Stateful helper that emits incremental output as transcript events arrive.

    One writer instance handles a single ``mngr robinhood`` invocation.
    The caller feeds it events from each turn via :meth:`emit_events`, then
    calls :meth:`finalize` with the result metadata to write any trailing
    envelope (for ``json``/``stream-json``) or the accumulated assistant
    text (for ``text``).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    output_format: OutputFormat = Field(description="The chosen output format")
    session_id: str = Field(description="Session identifier used in envelopes")
    stdout: Any = Field(description="Where output is written (file-like object with write()/flush())")
    replay_user_messages: bool = Field(
        default=False,
        description=(
            "If False (claude -p default), suppress stream-json output for user_message transcript "
            "events so the user's own prompts are not echoed back. Tool-result events are still "
            "emitted because they carry assistant feedback, not the user's input."
        ),
    )
    stream_plain_text: bool = Field(
        default=False,
        description=(
            "If True (text output mode + --stream-plain-text), assistant text is streamed to stdout "
            "incrementally via emit_partial_text, and the trailing full-text dump in finalize is "
            "suppressed to avoid duplicating the streamed content."
        ),
    )
    has_streamed_partials: bool = Field(
        default=False,
        description="Whether any partial text has been streamed (used to gate the final text dump)",
    )
    is_init_written: bool = Field(default=False, description="Whether the system/init envelope was emitted")
    seen_event_ids: set[str] = Field(default_factory=set, description="Event IDs already processed")
    assistant_text_parts: list[str] = Field(
        default_factory=list, description="Buffered assistant text used for text/json finalize"
    )
    assistant_message_count: int = Field(
        default=0,
        description=(
            "Number of distinct ``assistant_message`` transcript events the writer has seen "
            "(deduped by ``event_id``). The orchestrator snapshots this at turn start and waits "
            "after WAITING for it to grow past the snapshot, so we don't emit a partial result "
            "while ``stream_transcript.sh`` is still mirroring claude's per-session JSONL into "
            "events.jsonl."
        ),
    )
    last_assistant_stop_reason: str | None = Field(
        default=None,
        description=(
            "``stop_reason`` of the most recent ``assistant_message`` event observed. The "
            "orchestrator uses this together with ``assistant_message_count`` to decide whether "
            "to finalize on WAITING: a terminal stop_reason (``end_turn``, ``max_tokens``, "
            "``stop_sequence``) means the LAST assistant message of the turn has arrived; "
            "anything else (notably ``tool_use``) means more events are still coming."
        ),
    )

    def emit_partial_text(self, delta: str) -> None:
        """Emit a chunk of in-progress assistant text sourced from the stream buffer.

        For stream-json output, emits a claude-native ``stream_event`` carrying a
        ``content_block_delta`` / ``text_delta``. For text output (with
        --stream-plain-text), writes the delta straight to stdout. Other modes
        ignore partial text.
        """
        if delta == "":
            return
        match self.output_format:
            case OutputFormat.STREAM_JSON:
                self.write_init_if_needed()
                event = wrap_stream_event(text_delta_event(delta), self.session_id)
                self.stdout.write(json.dumps(event, separators=(",", ":")) + "\n")
                self.stdout.flush()
                self.has_streamed_partials = True
            case OutputFormat.TEXT:
                if self.stream_plain_text:
                    self.stdout.write(delta)
                    self.stdout.flush()
                    self.has_streamed_partials = True
            case OutputFormat.JSON:
                pass
            case _ as unreachable:
                assert_never(unreachable)

    def write_init_if_needed(self) -> None:
        """Write the synthesized ``system/init`` envelope on first stream-json call."""
        if self.output_format != OutputFormat.STREAM_JSON:
            return
        if self.is_init_written:
            return
        envelope = build_system_init_envelope(self.session_id)
        self.stdout.write(json.dumps(envelope, separators=(",", ":")) + "\n")
        self.stdout.flush()
        self.is_init_written = True

    def emit_events(self, events: list[dict[str, Any]]) -> None:
        """Process new transcript events, emitting per-format output as appropriate."""
        for event in events:
            event_id = event.get("event_id")
            if isinstance(event_id, str) and event_id in self.seen_event_ids:
                continue
            if isinstance(event_id, str):
                self.seen_event_ids.add(event_id)
            self._handle_event(event)

    def _handle_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "assistant_message":
            text = _coerce_str(event.get("text", ""))
            if text:
                self.assistant_text_parts.append(text)
            self.assistant_message_count += 1
            stop_reason = event.get("stop_reason")
            if isinstance(stop_reason, str):
                self.last_assistant_stop_reason = stop_reason
        match self.output_format:
            case OutputFormat.TEXT:
                pass
            case OutputFormat.JSON:
                pass
            case OutputFormat.STREAM_JSON:
                self.write_init_if_needed()
                self._write_stream_json_event(event)
            case _ as unreachable:
                assert_never(unreachable)

    def _write_stream_json_event(self, event: dict[str, Any]) -> None:
        if not self.replay_user_messages and event.get("type") == "user_message":
            # Match claude -p's default behavior: do not echo the user's own
            # prompts back into the stream-json output unless explicitly opted
            # in via --replay-user-messages. Tool-result events (which also
            # synthesize claude `user` envelopes) are not gated here because
            # they convey assistant tool feedback, not user input.
            return
        line = transcript_event_to_stream_json(event, self.session_id)
        if line is None:
            return
        self.stdout.write(json.dumps(line, separators=(",", ":")) + "\n")
        self.stdout.flush()

    def finalize(self, meta: ResultMeta, turn_count: int) -> None:
        """Write the trailing envelope (or text dump) for this invocation.

        ``turn_count`` is the count of conversational turns the
        orchestrator drove (one per user prompt delivered). It populates
        the turn-count field in the result envelope.
        """
        match self.output_format:
            case OutputFormat.TEXT:
                self._finalize_text()
            case OutputFormat.JSON:
                self._finalize_json(meta, turn_count)
            case OutputFormat.STREAM_JSON:
                self._finalize_stream_json(meta, turn_count)
            case _ as unreachable:
                assert_never(unreachable)

    def _collected_assistant_text(self) -> str:
        """Return the concatenation of every assistant text block seen so far."""
        return "".join(self.assistant_text_parts)

    def _finalize_text(self) -> None:
        # When text was streamed incrementally, the body has already been written
        # to stdout; re-dumping the authoritative text would duplicate it. Emit a
        # trailing newline so the output is newline-terminated, then stop.
        if self.stream_plain_text and self.has_streamed_partials:
            self.stdout.write("\n")
            self.stdout.flush()
            return
        body = self._collected_assistant_text()
        if body:
            self.stdout.write(body + "\n")
        self.stdout.flush()

    def _finalize_json(self, meta: ResultMeta, turn_count: int) -> None:
        self._write_result_envelope(meta, turn_count)

    def _finalize_stream_json(self, meta: ResultMeta, turn_count: int) -> None:
        self.write_init_if_needed()
        self._write_result_envelope(meta, turn_count)

    def _write_result_envelope(self, meta: ResultMeta, turn_count: int) -> None:
        """Build and emit the trailing ``result`` envelope on a single line."""
        envelope = build_result_envelope(
            text=self._collected_assistant_text(),
            meta=meta,
            turn_count=max(turn_count, 1),
        )
        self.stdout.write(json.dumps(envelope, separators=(",", ":")) + "\n")
        self.stdout.flush()


def monotonic_ms_since(start_monotonic: float) -> int:
    """Return milliseconds elapsed since the given ``time.monotonic()`` value."""
    return int((time.monotonic() - start_monotonic) * 1000)
