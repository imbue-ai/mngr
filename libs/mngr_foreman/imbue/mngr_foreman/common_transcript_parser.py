"""Normalize a mngr *common-transcript* JSONL stream into foreman UI events.

Several agent types (opencode, codex, pi-coding, ...) do not hand foreman a raw,
agent-native session log the way claude does. Instead their mngr plugin emits the
shared, agent-agnostic **common transcript** -- the canonical envelope defined in
:mod:`imbue.mngr.agents.common_transcript_records` (``user_message`` /
``assistant_message`` / ``tool_result`` records, each already carrying ``type`` /
``event_id`` / ``source`` plus the flat ``text`` / ``tool_calls`` / ``content`` /
``output`` payload the foreman frontend renders).

Because that envelope is already almost exactly foreman's own UI-event shape (the
claude parser in :mod:`~imbue.mngr_foreman.transcript_parser` produces the same
field names), this "parser" is a thin normalizer, not a re-implementation of the
claude parser. One shared normalizer serves every common-transcript agent
(opencode, codex, pi-coding) -- their records are the identical envelope, so a
per-harness parser module would be pure duplication. Per line it:

1. parses the JSON and keeps only the three known record types (envelope
   discipline: a line missing ``event_id`` or ``timestamp`` is skipped);
2. dedups on ``event_id`` (the tailer re-reads old bytes on rotation, and a
   backfill+live overlap can re-present a line);
3. renames the common schema's ``finish_reason`` to the ``stop_reason`` key the
   claude events use, so the frontend reads one name across agent types;
4. re-caps a ``tool_result``'s ``output`` to ``max_tool_output_chars`` (the
   common emitters cap their own output, but foreman honors its own bound too);
5. sorts by ``timestamp``.

Everything else on a record (``role``, ``model``, ``usage``, the ordered
``parts`` array, and any per-agent extras) is passed through untouched -- the
frontend ignores keys it does not use, so passing them through is both minimal
and forward-compatible. Common records carry only a 200-char ``input_preview``
per tool call (no ``input_full``), so tool calls render as a compact one-line
label rather than a diff; the frontend already falls back to ``input_preview``
when ``input_full`` is absent.
"""

from __future__ import annotations

import json
from typing import Any
from typing import Final

from loguru import logger

# The common-transcript record types foreman renders. Any other line (an unknown
# future record type, or a malformed one) is skipped rather than emitted.
_RENDERED_RECORD_TYPES: Final[frozenset[str]] = frozenset({"user_message", "assistant_message", "tool_result"})


def _truncate(content: str, max_chars: int) -> str:
    """Cap ``content`` to ``max_chars`` (0 = unlimited), appending an ellipsis when clipped."""
    if max_chars <= 0 or len(content) <= max_chars:
        return content
    return content[:max_chars] + "..."


def parse_common_transcript_lines(
    lines: list[str],
    existing_event_ids: set[str] | None = None,
    tool_name_by_call_id: dict[str, str] | None = None,
    max_tool_output_chars: int = 20000,
) -> list[dict[str, Any]]:
    """Normalize common-transcript JSONL ``lines`` into foreman UI event dicts, sorted by timestamp.

    ``existing_event_ids`` holds the IDs already emitted, for dedup across calls
    (mutated in place; a fresh set is used when None). ``tool_name_by_call_id`` is
    accepted only for signature parity with the claude parser and is ignored --
    common ``tool_result`` records already carry their own ``tool_name``, so no
    cross-message resolution is needed. ``max_tool_output_chars`` caps a
    ``tool_result``'s ``output`` length (0 means unlimited).
    """
    if existing_event_ids is None:
        existing_event_ids = set()

    new_events: list[tuple[str, dict[str, Any]]] = []

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as e:
            logger.debug("Skipping malformed common-transcript line: {}", e)
            continue

        if not isinstance(record, dict):
            continue
        record_type = record.get("type", "")
        event_id = record.get("event_id", "")
        timestamp = record.get("timestamp", "")
        if record_type not in _RENDERED_RECORD_TYPES or not event_id or not timestamp:
            continue
        if event_id in existing_event_ids:
            continue
        existing_event_ids.add(event_id)

        event: dict[str, Any] = dict(record)
        if record_type == "assistant_message":
            # The common schema names the stop reason ``finish_reason`` (the OTel
            # GenAI term); the claude events use ``stop_reason``. Normalize to one
            # key so the frontend reads it the same way across agent types.
            event["stop_reason"] = event.pop("finish_reason", None)
        elif record_type == "tool_result":
            event["output"] = _truncate(str(event.get("output", "")), max_tool_output_chars)
        else:
            # user_message: content is rendered verbatim, so it passes through
            # unchanged -- no field needs normalizing or capping.
            pass

        new_events.append((timestamp, event))

    new_events.sort(key=lambda item: item[0])
    return [event for _, event in new_events]
