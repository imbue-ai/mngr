#!/usr/bin/env bash
# Usage writer for codex agents.
#
# Reads the raw codex rollout stream at logs/codex_transcript/events.jsonl
# (produced verbatim by stream_transcript.sh) and emits one `cost_snapshot`
# event per `token_count` rollout item to events/codex/usage/events.jsonl, which
# `mngr usage` reads.
#
# codex reports cumulative token usage per session (info.total_token_usage) plus
# rate-limit windows, but no dollar cost -- so cost is left null (the reader
# estimates it from tokens via the pricing table) and the reader aggregates
# session-cumulatively (freshest token_count per session wins). codex's
# input_tokens INCLUDES cached, so we emit input = input_tokens - cached and
# cache_read = cached (the wire convention; OpenAI has no cache-write surcharge).
#
# token_count carries 5h (primary) / 7d (secondary) windows in subscription
# (ChatGPT-plan) mode; we map them onto the rate_limits window schema, which also
# classifies the session SUBSCRIPTION vs API_KEY.
#
# This is provisioned by mngr_codex_usage and launched by codex_background_tasks.sh
# (which runs it iff present), so usage events are only written when their reader
# is installed.
#
# Usage: codex_usage.sh [--single-pass]
#
# Environment:
#   MNGR_AGENT_STATE_DIR  - agent state directory (contains events/, logs/)

set -euo pipefail

AGENT_DATA_DIR="${MNGR_AGENT_STATE_DIR:?MNGR_AGENT_STATE_DIR must be set}"
INPUT_FILE="$AGENT_DATA_DIR/logs/codex_transcript/events.jsonl"
OUTPUT_FILE="$AGENT_DATA_DIR/events/codex/usage/events.jsonl"
# Cursor state (lines already consumed + last-seen session/model) so each poll
# is O(new lines), not O(whole transcript). Mirrors stream_transcript.sh's
# per-rollout offset approach.
STATE_FILE="$AGENT_DATA_DIR/plugin/codex/.usage_cursor"
POLL_INTERVAL=5

emit_new_usage_events() {
    if [ ! -f "$INPUT_FILE" ]; then
        return
    fi
    _INPUT_FILE="$INPUT_FILE" _OUTPUT_FILE="$OUTPUT_FILE" _STATE_FILE="$STATE_FILE" python3 << 'EMIT_SCRIPT' 2>>"$AGENT_DATA_DIR/events/logs/codex_usage_stderr.log" || true
import json
import os

_SOURCE = "codex/usage"


def _meta_value(obj, payload, key):
    """Read ``key`` from the item's payload, falling back to the top-level object."""
    if isinstance(payload, dict) and payload.get(key) is not None:
        return payload.get(key)
    return obj.get(key)


def _tokens_from_total_usage(total_usage):
    """Map codex cumulative usage to the wire token buckets (input is cache-exclusive)."""
    if not isinstance(total_usage, dict):
        return None
    input_tokens = total_usage.get("input_tokens")
    cached = total_usage.get("cached_input_tokens")
    output_tokens = total_usage.get("output_tokens")
    if isinstance(input_tokens, int) and isinstance(cached, int):
        non_cached_input = input_tokens - cached
    else:
        non_cached_input = input_tokens
    return {
        "input": non_cached_input,
        "output": output_tokens,
        "cache_read": cached,
        # OpenAI caching is automatic (read discount only); no cache-write bucket.
        "cache_creation": None,
    }


def _window(entry):
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


def _rate_limits(raw_rate_limits):
    if not isinstance(raw_rate_limits, dict):
        return None
    windows = {}
    # codex's `primary` is the shorter (5h) window; `secondary` the weekly one.
    primary = _window(raw_rate_limits.get("primary"))
    if primary is not None:
        windows["five_hour"] = {**primary, "label": "5h"}
    secondary = _window(raw_rate_limits.get("secondary"))
    if secondary is not None:
        windows["seven_day"] = {**secondary, "label": "7d"}
    return windows or None


def _load_state(state_file):
    """Return (offset_bytes, line_no, session_id, model) from the cursor file (defaults if absent)."""
    try:
        with open(state_file) as handle:
            state = json.load(handle)
        return (
            int(state.get("offset", 0)),
            int(state.get("line_no", 0)),
            state.get("session_id"),
            state.get("model"),
        )
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return 0, 0, None, None


def _save_state(state_file, offset, line_no, session_id, model):
    os.makedirs(os.path.dirname(state_file), exist_ok=True)
    with open(state_file, "w") as handle:
        json.dump({"offset": offset, "line_no": line_no, "session_id": session_id, "model": model}, handle)


def emit():
    input_file = os.environ["_INPUT_FILE"]
    output_file = os.environ["_OUTPUT_FILE"]
    state_file = os.environ["_STATE_FILE"]
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
    new_events = []
    with open(input_file) as handle:
        handle.seek(offset)
        for raw in handle:
            line_no += 1
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
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
            event = {
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


emit()
EMIT_SCRIPT
}

main() {
    mkdir -p "$(dirname "$OUTPUT_FILE")"
    if [ "${1:-}" = "--single-pass" ]; then
        emit_new_usage_events
        return
    fi
    while true; do
        emit_new_usage_events
        sleep "$POLL_INTERVAL"
    done
}

main "${1:-}"
