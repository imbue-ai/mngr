"""Normalize codex's common-transcript JSONL into foreman UI events.

Like pi-coding -- and unlike claude, whose raw session JSONL foreman parses with
the bespoke ``transcript_parser`` -- codex's mngr plugin already emits mngr's
agent-agnostic *common transcript*
(``events/codex/common_transcript/events.jsonl``, produced by
``mngr_codex``'s ``common_transcript.sh`` / ``common_transcript_convert.py`` from
the raw rollout stream), one normalized record per line. Those records are nearly
the exact dicts ``static/app.js`` renders, so this "parser" is a thin
pass-through: it validates each line, dedups on ``event_id``, renames codex's
``finish_reason`` to the ``stop_reason`` the claude events use, re-applies
foreman's output cap when it is stricter than codex's own, and returns the
records in timestamp order.

Codex's common records mirror pi's schema exactly (same envelope: ``user_message``
/ ``assistant_message`` / ``tool_result`` with the same field names), which is why
this file is a peer of ``pi_transcript.py`` rather than a shared module -- each
harness owns its normalizer so the two can diverge without entangling. Codex-only
traits the pass-through preserves untouched: ``model``, ``usage``, and
``finish_reason`` are always ``None`` (codex's converter does not populate them),
and each tool invocation is its own assistant_message carrying a single
``tool_call`` with a ~200-char ``input_preview`` (no ``input_full`` -> no diff
view). The frontend already falls back to ``input_preview`` when ``input_full`` is
absent, so no client change is needed.
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


def parse_codex_common_lines(
    lines: list[str],
    existing_event_ids: set[str] | None = None,
    # Unused: codex stamps ``tool_name`` on each ``tool_result`` directly. Accepted so
    # the call site stays uniform with the claude parser (which threads a tool-name map).
    tool_name_by_call_id: dict[str, str] | None = None,
    # Re-cap ``tool_result`` output to this many chars (0 = unlimited). codex already
    # caps at 2000, so this only ever tightens.
    max_tool_output_chars: int = 20000,
) -> list[dict[str, Any]]:
    """Parse codex common-transcript JSONL lines into foreman UI events, sorted by timestamp.

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
            logger.warning("Skipping malformed codex transcript line: {}", e)
            continue
        if not isinstance(event, dict):
            continue

        # event_id is codex's dedup key (synthesized from the line index, stable and
        # unique within the file). A record without one cannot be de-duplicated
        # safely, so drop it.
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            continue
        if event_id in existing_event_ids:
            continue

        event_type = event.get("type", "")
        # codex names the assistant turn's stop signal ``finish_reason`` (always None
        # today); the claude events (and any shared consumer) use ``stop_reason``.
        # Normalize so the harnesses' events are interchangeable.
        if event_type == "assistant_message" and "finish_reason" in event:
            event["stop_reason"] = event.pop("finish_reason")
        # Re-cap tool output only when foreman is configured stricter than codex's
        # own 2000-char cap; codex cannot recover a tail it already dropped.
        if event_type == "tool_result":
            output = event.get("output")
            if isinstance(output, str):
                event["output"] = _truncate(output, max_tool_output_chars)

        existing_event_ids.add(event_id)
        new_events.append((str(event.get("timestamp", "")), event))

    new_events.sort(key=lambda x: x[0])
    return [event for _, event in new_events]
