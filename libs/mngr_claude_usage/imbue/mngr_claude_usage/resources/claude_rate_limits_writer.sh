#!/bin/bash
# Rate-limit event writer for mngr_claude_usage.
#
# Reads a Claude Code statusline JSON object from stdin. After the first
# successful API response of the session, that JSON includes a top-level
# `rate_limits` field with five_hour / seven_day / overage windows
# (Claude.ai subscriptions only). We emit one event line per render to
# the per-agent rate-limits events file:
#
#     $MNGR_AGENT_STATE_DIR/events/claude/rate_limits/events.jsonl
#
# Path can be overridden via $MNGR_RATE_LIMITS_EVENTS_PATH for testing.
#
# Event envelope follows mngr's standard shape (matching common_transcript
# and mngr/activity):
#
#     {"source":"claude/rate_limits","type":"rate_limit_snapshot",
#      "event_id":"evt-<hex>","timestamp":"<ISO 8601>","rate_limits":<payload>}
#
# Append-only, no flock: appends shorter than PIPE_BUF (~4KB on Linux)
# are atomic w.r.t. concurrent appenders. Our event lines are well under
# that limit, so the file is safe under concurrent writers.
#
# Renders that omit `rate_limits` (i.e. before the first API response
# of the session) write nothing -- emitting an event with all-null
# windows would just clutter the log.
set -euo pipefail

events_path="${MNGR_RATE_LIMITS_EVENTS_PATH:-}"
if [ -z "$events_path" ]; then
  if [ -z "${MNGR_AGENT_STATE_DIR:-}" ]; then
    echo "claude_rate_limits_writer: neither MNGR_RATE_LIMITS_EVENTS_PATH nor MNGR_AGENT_STATE_DIR is set" >&2
    exit 64
  fi
  events_path="$MNGR_AGENT_STATE_DIR/events/claude/rate_limits/events.jsonl"
fi

input=$(cat)

# Skip emission when the payload has no rate_limits field. jq's `try` returns
# empty for non-object input, but jq itself still exits non-zero when stdin
# is non-JSON; `|| true` keeps the writer a no-op (rather than a hard error)
# under pipefail so a malformed render doesn't break statusline rendering.
rate_limits=$(printf '%s' "$input" | jq -c 'try .rate_limits // empty' 2>/dev/null || true)
if [ -z "$rate_limits" ] || [ "$rate_limits" = "null" ]; then
  exit 0
fi

mkdir -p "$(dirname "$events_path")"

event_id="evt-$(head -c 16 /dev/urandom | xxd -p)"
timestamp=$(date -u +"%Y-%m-%dT%H:%M:%S.000000000Z")

# Decorate Claude Code's window keys (five_hour / seven_day / overage) with
# short human-display labels (5h / 7d / overage). mngr usage uses these
# labels for the per-window line prefix; without them the literal key would
# be shown ("five_hour: 9% used"). Window keys themselves stay
# identifier-safe so format templates like {five_hour.used_percentage}
# remain functional.
labels='{"five_hour":"5h","seven_day":"7d","overage":"overage"}'
event=$(printf '%s' "$rate_limits" | jq -c \
  --arg event_id "$event_id" \
  --arg timestamp "$timestamp" \
  --argjson labels "$labels" \
  '{source:"claude/rate_limits",type:"rate_limit_snapshot",event_id:$event_id,timestamp:$timestamp,
    rate_limits:(. as $rl | reduce (keys_unsorted[]) as $k ({}; .[$k] = ($rl[$k] + (if $labels[$k] then {label:$labels[$k]} else {} end))))}')

printf '%s\n' "$event" >> "$events_path"
