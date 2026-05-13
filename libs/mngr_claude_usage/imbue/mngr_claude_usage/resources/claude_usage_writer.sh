#!/bin/bash
# Cost + rate-limit event writer for mngr_claude_usage.
#
# Reads a Claude Code statusline JSON object from stdin and appends one
# event line per render to the per-agent events file:
#
#     $MNGR_AGENT_STATE_DIR/events/claude/usage/events.jsonl
#
# Path can be overridden via $MNGR_USAGE_EVENTS_PATH for testing.
#
# What's captured from each statusline payload (per Claude Code docs):
#   - rate_limits   -- five_hour / seven_day / overage windows. Present only
#                      for Claude.ai Pro/Max subscribers, after the first
#                      API response of the session.
#   - cost          -- total_cost_usd / duration / lines-changed. Computed
#                      client-side, present for all auth modes (subscription
#                      AND direct ANTHROPIC_API_KEY).
#   - session_id    -- the Claude Code session UUID. Carried so cost can be
#                      correlated back to the session it accumulated in
#                      (cost resets per session, so a delta is meaningful
#                      only within one session_id).
#
# Event envelope follows mngr's standard shape (matching common_transcript
# and mngr/activity):
#
#     {"source":"claude/usage","type":"cost_snapshot",
#      "event_id":"evt-<hex>","timestamp":"<ISO 8601>",
#      "session_id":"<uuid>","cost":<cost-or-null>,
#      "rate_limits":<rate-limits-or-null>}
#
# session_id is contractually a non-empty string -- the reader drops events
# whose session_id is null/missing with a WARNING. The jq below falls back
# to null defensively if the upstream Claude Code payload ever omits it,
# but that's writer/upstream drift, not a canonical wire value.
#
# Append-only, no flock: appends shorter than PIPE_BUF (~4KB on Linux)
# are atomic w.r.t. concurrent appenders. Our event lines are well under
# that limit, so the file is safe under concurrent writers.
#
# Renders that have neither rate_limits nor cost (e.g. a payload before
# the first API response *and* before Claude Code attached any cost data)
# write nothing -- emitting an all-null event would just clutter the log.
set -euo pipefail

events_path="${MNGR_USAGE_EVENTS_PATH:-}"
if [ -z "$events_path" ]; then
  if [ -z "${MNGR_AGENT_STATE_DIR:-}" ]; then
    echo "claude_usage_writer: neither MNGR_USAGE_EVENTS_PATH nor MNGR_AGENT_STATE_DIR is set" >&2
    exit 64
  fi
  events_path="$MNGR_AGENT_STATE_DIR/events/claude/usage/events.jsonl"
fi

input=$(cat)

# Skip emission when the payload has neither rate_limits nor cost. jq's `try`
# returns null for non-object input; `|| echo "no"` keeps the writer a no-op
# (rather than a hard error under `set -euo pipefail`) when stdin isn't JSON.
should_emit=$(printf '%s' "$input" | jq -r '
  if ((try .rate_limits // null) != null) or ((try .cost // null) != null)
  then "yes" else "no" end
' 2>/dev/null || echo "no")
if [ "$should_emit" != "yes" ]; then
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
#
# window_seconds gives mngr_usage enough info to derive elapsed_percentage
# (= (1 - seconds_until_reset / window_seconds) * 100) without baking
# window-class knowledge into the reader. Overage has no fixed window
# length so it's omitted; the reader treats missing window_seconds as
# "no derived elapsed metrics for this window."
#
# The `type == "object"` guard handles unexpected `rate_limits` shapes
# (e.g. a string or array, if the statusline schema ever changes): the
# value is passed through unchanged, and the CLI reader's
# isinstance(dict) check filters the malformed event downstream. Without
# this guard, jq's reduce would error and `set -euo pipefail` would
# abort the writer.
labels='{"five_hour":"5h","seven_day":"7d","overage":"overage"}'
window_seconds='{"five_hour":18000,"seven_day":604800}'
event=$(printf '%s' "$input" | jq -c \
  --arg event_id "$event_id" \
  --arg timestamp "$timestamp" \
  --argjson labels "$labels" \
  --argjson window_seconds "$window_seconds" \
  '{
    source: "claude/usage",
    type: "cost_snapshot",
    event_id: $event_id,
    timestamp: $timestamp,
    session_id: (try .session_id // null),
    cost: (try .cost // null),
    rate_limits: (
      (try .rate_limits // null) as $rl |
      if $rl == null then null
      elif ($rl | type) == "object" then
        reduce ($rl | keys_unsorted[]) as $k ({};
          .[$k] = ($rl[$k]
                  + (if $labels[$k] then {label: $labels[$k]} else {} end)
                  + (if $window_seconds[$k] then {window_seconds: $window_seconds[$k]} else {} end))
        )
      else $rl
      end
    )
  }')

printf '%s\n' "$event" >> "$events_path"
