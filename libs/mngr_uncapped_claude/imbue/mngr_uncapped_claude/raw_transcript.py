"""Read raw claude transcript events and convert them into the common
transcript shape consumed by :class:`StreamingOutputWriter`.

The raw transcript at ``$MNGR_AGENT_STATE_DIR/logs/claude_transcript/events.jsonl``
is populated by ``stream_transcript.sh`` on a ~1-second cadence directly
from claude's per-session JSONL files. The common transcript at
``events/claude/common_transcript/events.jsonl`` is a downstream derivation
produced by ``common_transcript.sh`` on a ~5-second cadence -- so reading
the raw one lets us surface the assistant's reply ~4-5 seconds sooner,
which matters for the ``claude -p``-replacement semantics this plugin
promises.

The conversion logic here mirrors ``common_transcript.sh``'s python script
so that the writer keeps consuming the same common-transcript shape (no
downstream changes needed): the only thing that moves is the *source* of
events. The converter is stateful because raw assistant events declare
``tool_use`` blocks that later raw user events refer to by ``tool_use_id``;
we have to remember the tool name per call-id so the synthesized
``tool_result`` common events can carry it.
"""

import json
from collections.abc import Sequence
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.utils.jsonl_warn import MalformedJsonLineWarner

# Filename relative to the agent's events directory holding the raw
# stream-json transcript that ``stream_transcript.sh`` writes. The output
# helper expects an events-target-relative path, and since the agent's
# events root is ``$MNGR_AGENT_STATE_DIR/events`` but the raw transcript
# lives at ``$MNGR_AGENT_STATE_DIR/logs/claude_transcript/events.jsonl``,
# we navigate up one level. Keep in sync with ``stream_transcript.sh``.
RAW_TRANSCRIPT_PATH: Final[str] = "../logs/claude_transcript/events.jsonl"

# Truncation lengths used by the common transcript so the synthesized
# stream-json output stays within typical line-length limits.
_MAX_INPUT_PREVIEW_LENGTH: Final[int] = 200
_MAX_OUTPUT_LENGTH: Final[int] = 2000


class RawTranscriptParser(MutableModel):
    """Stateful per-run parser: raw claude transcript -> common transcript events.

    Holds two pieces of state across calls:

    * ``warner`` -- the same :class:`MalformedJsonLineWarner` the orchestrator
      already uses; isolated here so the orchestrator's polling loop only
      has to thread one object through instead of two.
    * ``tool_name_by_call_id`` -- populated when an assistant event declares
      ``tool_use`` blocks; consumed later when the corresponding user event
      contains a ``tool_result`` block referencing the same ``tool_use_id``,
      so the synthesized common ``tool_result`` event can carry the
      originating tool's name (the raw user event doesn't carry it).
    """

    warner: MalformedJsonLineWarner = Field(
        description="Wraps malformed-line warnings so the parser is the only owner"
    )
    tool_name_by_call_id: dict[str, str] = Field(
        default_factory=dict,
        description="tool_use_id -> tool_name, learned from raw assistant events for tool_result labeling",
    )

    def parse_lines(self, lines: Sequence[str]) -> list[dict[str, Any]]:
        """Parse a batch of raw transcript lines, return the resulting common events."""
        events: list[dict[str, Any]] = []
        for line in lines:
            stripped = line.strip()
            if stripped == "":
                continue
            parsed = self.warner.parse(stripped)
            if parsed is None:
                continue
            raw, _ = parsed
            events.extend(self._raw_to_common(raw))
        return events

    def _raw_to_common(self, raw: dict[str, Any]) -> list[dict[str, Any]]:
        """Dispatch one raw event to its common-shape converter; returns 0+ events."""
        event_type = raw.get("type")
        uuid = raw.get("uuid")
        timestamp = raw.get("timestamp")
        if not isinstance(uuid, str) or not isinstance(timestamp, str):
            return []
        if event_type == "assistant":
            return self._convert_assistant(raw, uuid, timestamp)
        if event_type == "user":
            is_meta = bool(raw.get("isMeta", False))
            return self._convert_user(raw, uuid, timestamp, is_meta)
        return []

    def _convert_assistant(self, raw: dict[str, Any], uuid: str, timestamp: str) -> list[dict[str, Any]]:
        message = raw.get("message", {})
        if not isinstance(message, dict):
            return []
        content_blocks = message.get("content", [])
        model = message.get("model", "unknown")
        stop_reason = message.get("stop_reason")
        usage_raw = message.get("usage", {})

        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        if isinstance(content_blocks, list):
            for block in content_blocks:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    text = block.get("text", "")
                    if isinstance(text, str) and text:
                        text_parts.append(text)
                elif block_type == "tool_use":
                    self._collect_tool_use(block, tool_calls)
                else:
                    # Other block types (thinking, image, ...) carry no text
                    # or tool-call info we surface, so they are skipped.
                    pass

        usage = self._convert_usage(usage_raw) if isinstance(usage_raw, dict) else None

        return [
            {
                "timestamp": timestamp,
                "type": "assistant_message",
                "event_id": f"{uuid}-assistant",
                "source": "claude/raw_transcript",
                "role": "assistant",
                "model": model,
                "text": "\n".join(text_parts),
                "tool_calls": tool_calls,
                "stop_reason": stop_reason,
                "usage": usage,
                "message_uuid": uuid,
            }
        ]

    def _collect_tool_use(self, block: dict[str, Any], tool_calls: list[dict[str, Any]]) -> None:
        call_id = block.get("id", "")
        tool_name = block.get("name", "")
        tool_input = block.get("input", {})
        try:
            input_preview = json.dumps(tool_input, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            logger.warning("Tool input not JSON-serializable for {!r}: {}", tool_name, exc)
            input_preview = str(tool_input)
        if len(input_preview) > _MAX_INPUT_PREVIEW_LENGTH:
            input_preview = input_preview[:_MAX_INPUT_PREVIEW_LENGTH] + "..."
        if isinstance(call_id, str) and isinstance(tool_name, str) and call_id and tool_name:
            self.tool_name_by_call_id[call_id] = tool_name
        tool_calls.append(
            {
                "tool_call_id": call_id if isinstance(call_id, str) else "",
                "tool_name": tool_name if isinstance(tool_name, str) else "",
                "input_preview": input_preview,
            }
        )

    def _convert_usage(self, usage_raw: dict[str, Any]) -> dict[str, Any] | None:
        if not usage_raw:
            return None
        return {
            "input_tokens": usage_raw.get("input_tokens", 0),
            "output_tokens": usage_raw.get("output_tokens", 0),
            "cache_read_tokens": usage_raw.get("cache_read_input_tokens"),
            "cache_write_tokens": usage_raw.get("cache_creation_input_tokens"),
        }

    def _convert_user(self, raw: dict[str, Any], uuid: str, timestamp: str, is_meta: bool) -> list[dict[str, Any]]:
        message = raw.get("message", {})
        if not isinstance(message, dict):
            return []
        content = message.get("content")
        events: list[dict[str, Any]] = []

        text = _extract_text_content(content)
        has_only_tool_results = _has_tool_results_only(content)
        if not has_only_tool_results and text:
            if is_meta:
                events.append(self._build_meta_event(uuid, timestamp, _truncate(text, _MAX_OUTPUT_LENGTH)))
            else:
                events.append(
                    {
                        "timestamp": timestamp,
                        "type": "user_message",
                        "event_id": f"{uuid}-user",
                        "source": "claude/raw_transcript",
                        "role": "user",
                        "content": text,
                        "message_uuid": uuid,
                    }
                )

        if isinstance(content, list):
            for block in content:
                tool_result = self._maybe_build_tool_result(block, uuid, timestamp)
                if tool_result is not None:
                    events.append(tool_result)

        return events

    def _build_meta_event(self, uuid: str, timestamp: str, text: str) -> dict[str, Any]:
        """Framework-injected user message (stop-hook output etc.) -> common tool_result.

        Mirrors ``common_transcript.sh``'s reclassification so meta messages
        don't masquerade as real user input downstream.
        """
        return {
            "timestamp": timestamp,
            "type": "tool_result",
            "event_id": f"{uuid}-meta",
            "source": "claude/raw_transcript",
            "tool_call_id": f"meta-{uuid}",
            "tool_name": "meta",
            "output": text,
            "is_error": False,
            "message_uuid": uuid,
        }

    def _maybe_build_tool_result(self, block: Any, uuid: str, timestamp: str) -> dict[str, Any] | None:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            return None
        tool_use_id = block.get("tool_use_id")
        if not isinstance(tool_use_id, str) or not tool_use_id:
            return None
        result_content = _flatten_tool_result_content(block.get("content", ""))
        result_content = _truncate(result_content, _MAX_OUTPUT_LENGTH)
        tool_name = self.tool_name_by_call_id.get(tool_use_id, "unknown")
        return {
            "timestamp": timestamp,
            "type": "tool_result",
            "event_id": f"{uuid}-tool_result-{tool_use_id}",
            "source": "claude/raw_transcript",
            "tool_call_id": tool_use_id,
            "tool_name": tool_name,
            "output": result_content,
            "is_error": bool(block.get("is_error", False)),
            "message_uuid": uuid,
        }


def _extract_text_content(content: Any) -> str:
    """Concatenate plain text from a message content field (string or content-block list)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            if isinstance(text, str) and text:
                parts.append(text)
    return "\n".join(parts)


def _has_tool_results_only(content: Any) -> bool:
    """True iff ``content`` is a list whose blocks are all ``tool_result``."""
    if isinstance(content, str):
        return False
    if not isinstance(content, list):
        return True
    for block in content:
        if isinstance(block, dict):
            if block.get("type") != "tool_result":
                return False
        else:
            return False
    return True


def _flatten_tool_result_content(result_content: Any) -> str:
    """Coerce a tool_result block's ``content`` field into a single string."""
    if isinstance(result_content, str):
        return result_content
    if isinstance(result_content, list):
        parts: list[str] = []
        for item in result_content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
            else:
                # Non-text, non-string blocks (e.g. image content) are not
                # surfaceable as plain text and are intentionally dropped.
                pass
        return "\n".join(parts)
    return str(result_content)


def _truncate(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return value[:max_length] + "..."
