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

# Directory this script was installed into; the emitter module is installed
# alongside it (in the host's commands/ dir in production, in resources/ under
# test), so resolve it relative to ourselves rather than via MNGR_AGENT_STATE_DIR.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
    _INPUT_FILE="$INPUT_FILE" _OUTPUT_FILE="$OUTPUT_FILE" _STATE_FILE="$STATE_FILE" \
        python3 "$SCRIPT_DIR/codex_usage_emit.py" 2>>"$AGENT_DATA_DIR/events/logs/codex_usage_stderr.log" || true
}

main() {
    mkdir -p "$(dirname "$OUTPUT_FILE")"
    # Ensure the stderr-log dir exists before emit_new_usage_events redirects to
    # it; otherwise the redirect-open fails under `set -e` and the emitter is
    # silently skipped. Self-contained rather than relying on launch order.
    mkdir -p "$AGENT_DATA_DIR/events/logs"
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
