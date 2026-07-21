"""Normalize pi-coding's common-transcript JSONL into foreman UI events.

Unlike claude -- whose raw session JSONL foreman parses with the bespoke
``transcript_parser`` -- pi's mngr lifecycle extension already emits mngr's
agent-agnostic *common transcript*
(``events/pi-coding/common_transcript/events.jsonl``), one normalized record per
line. Those records are nearly the exact dicts ``static/app.js`` renders, so this
"parser" is a thin pass-through: it validates each line, dedups on ``event_id``,
renames pi's ``finish_reason`` to the ``stop_reason`` the claude events use,
re-applies foreman's output cap when it is stricter than pi's own, and returns the
records in timestamp order.

Known gaps versus claude events (accepted; see the pi-coding foreman spec): pi
carries only a ~200-char ``input_preview`` per tool call (no ``input_full`` -> no
diff view) and drops image blocks. The frontend already falls back to
``input_preview`` when ``input_full`` is absent, so no client change is needed.
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger


def _truncate(content: str, max_chars: int) -> str:
    """Head-truncate ``content`` to ``max_chars`` (0 means unlimited)."""
    if max_chars <= 0 or len(content) <= max_chars:
        return content
    return content[:max_chars] + "..."


def parse_pi_common_lines(
    lines: list[str],
    existing_event_ids: set[str] | None = None,
    # Unused: pi stamps ``tool_name`` on each record directly. Accepted so the call
    # site stays uniform with the claude parser (which threads a tool-name map).
    tool_name_by_call_id: dict[str, str] | None = None,
    # Re-cap ``tool_result`` output to this many chars (0 = unlimited). pi already
    # caps at 2000, so this only ever tightens.
    max_tool_output_chars: int = 20000,
) -> list[dict[str, Any]]:
    """Parse pi common-transcript JSONL lines into foreman UI events, sorted by timestamp.

    ``existing_event_ids`` (mutated in place) dedups across calls; None starts fresh.
    """
    if existing_event_ids is None:
        existing_event_ids = set()

    new_events: list[tuple[str, dict[str, Any]]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as e:
            # A malformed line means a truncated/corrupt mirror write (the tailer
            # already holds back trailing partial lines); surface it, don't swallow.
            logger.warning("Skipping malformed pi transcript line: {}", e)
            continue
        if not isinstance(event, dict):
            continue

        # event_id is pi's dedup key (dense + unique within the file). A record
        # without one cannot be de-duplicated safely, so drop it.
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            continue
        if event_id in existing_event_ids:
            continue

        event_type = event.get("type", "")
        # pi names the assistant turn's stop signal ``finish_reason``; the claude
        # events (and any shared consumer) use ``stop_reason``. Normalize so the
        # two harnesses' events are interchangeable.
        if event_type == "assistant_message" and "finish_reason" in event:
            event["stop_reason"] = event.pop("finish_reason")
        # Re-cap tool output only when foreman is configured stricter than pi's
        # own 2000-char cap; pi cannot recover a tail it already dropped.
        if event_type == "tool_result":
            output = event.get("output")
            if isinstance(output, str):
                event["output"] = _truncate(output, max_tool_output_chars)

        existing_event_ids.add(event_id)
        new_events.append((str(event.get("timestamp", "")), event))

    new_events.sort(key=lambda x: x[0])
    return [event for _, event in new_events]
