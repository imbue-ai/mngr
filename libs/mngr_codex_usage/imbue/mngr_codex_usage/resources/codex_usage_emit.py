#!/usr/bin/env python3
"""Usage event emitter for codex agents (invoked by codex_usage.sh).

Reads new lines from the raw codex rollout stream and appends one
``cost_snapshot`` event per ``token_count`` rollout item to the usage events
file, advancing a byte-offset cursor so each poll is O(new lines), not O(whole
transcript).

Invoked as ``python3 codex_usage_emit.py`` with the input/output/state paths
passed via the ``_INPUT_FILE`` / ``_OUTPUT_FILE`` / ``_STATE_FILE`` environment
variables that codex_usage.sh sets. Split out of the shell script (it used to be
an inline ``python3`` heredoc) so the logic is lintable, type-checked, and
unit-testable directly rather than only through a subprocess.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any
from typing import Union

# A parsed-JSON value of unspecified shape. Stdlib-only (pydantic isn't importable
# under the host's bare python3). Spelled with Union, not ``|``: this assignment runs
# at import, and ``|`` on types needs python 3.10+. noqa stops ruff rewriting it.
JsonValue = Union[str, int, float, bool, None, list, dict]  # noqa: UP007

_SOURCE = "codex/usage"


def _meta_value(obj: dict[str, Any], payload: JsonValue, key: str) -> JsonValue:
    """Read ``key`` from the item's payload, falling back to the top-level object."""
    if isinstance(payload, dict) and payload.get(key) is not None:
        return payload.get(key)
    return obj.get(key)


def _tokens_from_total_usage(total_usage: JsonValue) -> dict[str, Any] | None:
    """Map codex cumulative usage to the wire token buckets (input is cache-exclusive).

    Returns None for a non-dict input and also for a dict with no usable buckets
    (e.g. ``{}``), so the caller's "tokens is None and rate_limits is None" guard
    drops content-free token blocks rather than emitting an all-None snapshot that
    the reader would price as a spurious $0.00 estimated session.
    """
    if not isinstance(total_usage, dict):
        return None
    input_tokens = total_usage.get("input_tokens")
    cached = total_usage.get("cached_input_tokens")
    output_tokens = total_usage.get("output_tokens")
    if isinstance(input_tokens, int) and isinstance(cached, int):
        non_cached_input = input_tokens - cached
    else:
        non_cached_input = input_tokens
    buckets = {
        "input": non_cached_input,
        "output": output_tokens,
        "cache_read": cached,
        # OpenAI caching is automatic (read discount only); no cache-write bucket.
        "cache_creation": None,
    }
    if all(value is None for value in buckets.values()):
        return None
    return buckets


def _window(entry: JsonValue) -> dict[str, Any] | None:
    """Map a codex rate-limit entry to the window schema; window_seconds from window_minutes."""
    if not isinstance(entry, dict):
        return None
    window_minutes = entry.get("window_minutes")
    window_seconds = window_minutes * 60 if isinstance(window_minutes, int) else None
    return {
        "used_percentage": entry.get("used_percent"),
        "resets_at": entry.get("resets_at"),
        "window_seconds": window_seconds,
    }


def _rate_limits(raw_rate_limits: JsonValue) -> dict[str, Any] | None:
    if not isinstance(raw_rate_limits, dict):
        return None
    windows: dict[str, Any] = {}
    # codex's `primary` is the shorter (5h) window; `secondary` the weekly one.
    primary = _window(raw_rate_limits.get("primary"))
    if primary is not None:
        windows["five_hour"] = {**primary, "label": "5h"}
    secondary = _window(raw_rate_limits.get("secondary"))
    if secondary is not None:
        windows["seven_day"] = {**secondary, "label": "7d"}
    return windows or None


def _load_state(state_file: str) -> tuple[int, int, str | None, str | None]:
    """Return (offset_bytes, line_no, session_id, model) from the cursor file (defaults if absent)."""
    # Absent cursor is the normal first-run case -- reprocess from the top silently.
    if not os.path.exists(state_file):
        return 0, 0, None, None
    try:
        with open(state_file) as handle:
            state = json.load(handle)
        return (
            int(state.get("offset", 0)),
            int(state.get("line_no", 0)),
            state.get("session_id"),
            state.get("model"),
        )
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
        # A present-but-corrupt cursor: reset to the top (the reader dedups the
        # re-emitted events), but surface it -- silent corruption hides bugs.
        logging.warning("resetting unreadable codex usage cursor %s: %s", state_file, exc)
        return 0, 0, None, None


def _save_state(state_file: str, offset: int, line_no: int, session_id: str | None, model: str | None) -> None:
    os.makedirs(os.path.dirname(state_file), exist_ok=True)
    with open(state_file, "w") as handle:
        json.dump({"offset": offset, "line_no": line_no, "session_id": session_id, "model": model}, handle)


def emit(input_file: str, output_file: str, state_file: str) -> None:
    if not os.path.exists(input_file):
        return

    offset, line_no, session_id, model = _load_state(state_file)
    # The transcript is append-only; a file shorter than our saved offset means it
    # rotated/truncated, so reprocess from the top rather than silently skipping a
    # fresh rollout's events.
    if os.path.getsize(input_file) < offset:
        offset, line_no, session_id, model = 0, 0, None, None

    # Seek to the saved byte offset and read only the new tail -- O(new bytes) per
    # poll, not O(whole transcript). session_meta / turn_context are persisted in
    # the state, so a token_count in the new tail still resolves its session/model
    # even though those lines were consumed in an earlier pass.
    new_events: list[dict[str, Any]] = []
    with open(input_file) as handle:
        handle.seek(offset)
        for raw in handle:
            line_no += 1
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError as exc:
                logging.warning("skipping malformed codex rollout line %d: %s", line_no, exc)
                continue
            payload = obj.get("payload")
            item_type = obj.get("type")
            if item_type == "session_meta":
                candidate = _meta_value(obj, payload, "id")
                if isinstance(candidate, str) and candidate:
                    session_id = candidate
                continue
            if item_type == "turn_context":
                candidate = _meta_value(obj, payload, "model")
                if isinstance(candidate, str) and candidate:
                    model = candidate
                continue
            if not (isinstance(payload, dict) and payload.get("type") == "token_count"):
                continue
            if not session_id:
                continue
            info = payload.get("info")
            tokens = _tokens_from_total_usage(info.get("total_token_usage")) if isinstance(info, dict) else None
            rate_limits = _rate_limits(payload.get("rate_limits"))
            if tokens is None and rate_limits is None:
                continue
            event: dict[str, Any] = {
                "source": _SOURCE,
                "type": "cost_snapshot",
                # event_id need only be present; the reader dedups by freshest-per-session,
                # so a re-emitted token_count (after a crash before the cursor advanced)
                # is harmless -- it carries the same cumulative reading.
                "event_id": "line-%d-usage" % line_no,
                "timestamp": obj.get("timestamp"),
                "session_id": session_id,
                # No reported cost -- the reader estimates from tokens + model.
                "cost": None,
                "tokens": tokens,
                "model": ("openai/%s" % model) if model else None,
                # rate_limits present => ChatGPT-plan subscription (imputed); else real API spend.
                "cost_mode": "SUBSCRIPTION" if rate_limits is not None else "API_KEY",
            }
            if rate_limits is not None:
                event["rate_limits"] = rate_limits
            new_events.append(event)
        new_offset = handle.tell()

    # Append events BEFORE advancing the cursor: a crash in between re-emits (the
    # reader collapses duplicates), but never drops an event.
    if new_events:
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        with open(output_file, "a") as out:
            for event in new_events:
                out.write(json.dumps(event, separators=(",", ":")) + "\n")
    _save_state(state_file, new_offset, line_no, session_id, model)


if __name__ == "__main__":
    emit(os.environ["_INPUT_FILE"], os.environ["_OUTPUT_FILE"], os.environ["_STATE_FILE"])
