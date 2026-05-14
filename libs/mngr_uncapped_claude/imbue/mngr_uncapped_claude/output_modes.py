import json
import time
from typing import Any
from typing import Final
from typing import IO
from typing import assert_never

from imbue.imbue_common.pure import pure
from imbue.mngr_uncapped_claude.data_types import OutputFormat
from imbue.mngr_uncapped_claude.data_types import ResultMeta

# Hard-coded model identifier used in the synthesized stream-json system/init
# envelope. mngr does not know which model the spawned agent picked, so we
# emit ``unknown`` rather than guessing.
_PLACEHOLDER_MODEL: Final[str] = "unknown"


@pure
def build_result_envelope(
    text: str,
    meta: ResultMeta,
    num_turns: int,
) -> dict[str, Any]:
    """Synthesize a ``{"type": "result", ...}`` envelope matching claude -p's shape.

    Fields mngr cannot observe (cost, token usage, model breakdown, ...) are
    zeroed or set to None. Consumers that parse this JSON should treat those
    as best-effort.
    """
    subtype = "error" if meta.is_error else "success"
    return {
        "type": "result",
        "subtype": subtype,
        "is_error": meta.is_error,
        "api_error_status": None,
        "duration_ms": meta.duration_ms,
        "duration_api_ms": 0,
        "num_turns": num_turns,
        "result": meta.error_text if meta.is_error else text,
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
    text = event.get("text", "")
    tool_calls = event.get("tool_calls", []) or []
    content_blocks: list[dict[str, Any]] = []
    if text:
        content_blocks.append({"type": "text", "text": text})
    for call in tool_calls:
        content_blocks.append(
            {
                "type": "tool_use",
                "id": call.get("tool_call_id", ""),
                "name": call.get("tool_name", ""),
                "input": _parse_input_preview(call.get("input_preview", "")),
            }
        )
    return {
        "type": "assistant",
        "message": {
            "id": event.get("message_uuid", ""),
            "type": "message",
            "role": "assistant",
            "model": event.get("model", _PLACEHOLDER_MODEL),
            "content": content_blocks,
            "stop_reason": event.get("stop_reason"),
            "usage": event.get("usage"),
        },
        "session_id": session_id,
    }


def _user_event_to_stream_json(event: dict[str, Any], session_id: str) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": event.get("content", ""),
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
    except json.JSONDecodeError:
        return preview


@pure
def collect_assistant_text(events: list[dict[str, Any]]) -> str:
    """Concatenate the text from every ``assistant_message`` event in order.

    Used by ``OutputFormat.TEXT``. Joins multiple assistant turns with
    newlines so the user sees the full assistant-side content of the run,
    matching how ``claude -p`` prints its response stream.
    """
    parts: list[str] = []
    for event in events:
        if event.get("type") != "assistant_message":
            continue
        text = event.get("text", "")
        if text:
            parts.append(text)
    return "\n".join(parts)


class StreamingOutputWriter:
    """Stateful helper that emits incremental output as transcript events arrive.

    One writer instance handles a single ``mngr uncapped-claude`` invocation.
    The caller feeds it events from each turn via :meth:`emit_events`, then
    calls :meth:`finalize` with the result metadata to write any trailing
    envelope (for ``json``/``stream-json``) or the accumulated assistant
    text (for ``text``).
    """

    output_format: OutputFormat
    session_id: str
    stdout: IO[str]
    _is_init_written: bool
    _seen_event_ids: set[str]
    _assistant_text_parts: list[str]
    _num_assistant_turns: int

    def __init__(self, output_format: OutputFormat, session_id: str, stdout: IO[str]) -> None:
        self.output_format = output_format
        self.session_id = session_id
        self.stdout = stdout
        self._is_init_written = False
        self._seen_event_ids = set()
        self._assistant_text_parts = []
        self._num_assistant_turns = 0

    def write_init_if_needed(self) -> None:
        """Write the synthesized ``system/init`` envelope on first stream-json call."""
        if self.output_format != OutputFormat.STREAM_JSON:
            return
        if self._is_init_written:
            return
        envelope = build_system_init_envelope(self.session_id)
        self.stdout.write(json.dumps(envelope, separators=(",", ":")) + "\n")
        self.stdout.flush()
        self._is_init_written = True

    def emit_events(self, events: list[dict[str, Any]]) -> None:
        """Process new transcript events, emitting per-format output as appropriate."""
        for event in events:
            event_id = event.get("event_id")
            if isinstance(event_id, str) and event_id in self._seen_event_ids:
                continue
            if isinstance(event_id, str):
                self._seen_event_ids.add(event_id)
            self._handle_event(event)

    def _handle_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "assistant_message":
            text = event.get("text", "")
            if text:
                self._assistant_text_parts.append(text)
            self._num_assistant_turns += 1
        match self.output_format:
            case OutputFormat.TEXT:
                # Text mode accumulates until finalize().
                pass
            case OutputFormat.JSON:
                # JSON mode emits only the trailing result envelope.
                pass
            case OutputFormat.STREAM_JSON:
                self.write_init_if_needed()
                self._write_stream_json_event(event)
            case _ as unreachable:
                assert_never(unreachable)

    def _write_stream_json_event(self, event: dict[str, Any]) -> None:
        line = transcript_event_to_stream_json(event, self.session_id)
        if line is None:
            return
        self.stdout.write(json.dumps(line, separators=(",", ":")) + "\n")
        self.stdout.flush()

    def finalize(self, meta: ResultMeta) -> None:
        """Write the trailing envelope (or text dump) for this invocation."""
        match self.output_format:
            case OutputFormat.TEXT:
                self._finalize_text()
            case OutputFormat.JSON:
                self._finalize_json(meta)
            case OutputFormat.STREAM_JSON:
                self._finalize_stream_json(meta)
            case _ as unreachable:
                assert_never(unreachable)

    def _finalize_text(self) -> None:
        body = "\n".join(self._assistant_text_parts)
        if body:
            self.stdout.write(body + "\n")
        self.stdout.flush()

    def _finalize_json(self, meta: ResultMeta) -> None:
        envelope = build_result_envelope(
            text="\n".join(self._assistant_text_parts),
            meta=meta,
            num_turns=max(self._num_assistant_turns, 1),
        )
        self.stdout.write(json.dumps(envelope, separators=(",", ":")) + "\n")
        self.stdout.flush()

    def _finalize_stream_json(self, meta: ResultMeta) -> None:
        self.write_init_if_needed()
        envelope = build_result_envelope(
            text="\n".join(self._assistant_text_parts),
            meta=meta,
            num_turns=max(self._num_assistant_turns, 1),
        )
        self.stdout.write(json.dumps(envelope, separators=(",", ":")) + "\n")
        self.stdout.flush()


def monotonic_ms_since(start_monotonic: float) -> int:
    """Return milliseconds elapsed since the given ``time.monotonic()`` value."""
    return int((time.monotonic() - start_monotonic) * 1000)
