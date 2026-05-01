#!/bin/bash
# Rate-limit cache merge writer for mngr_usage.
#
# Modes:
#   statusline  Reads a Claude Code statusline JSON object from stdin; extracts
#               .rate_limits.{five_hour,seven_day,overage} and folds each window
#               into the cache (used_percentage, resets_at, source=statusline).
#   sdk         Reads stream-json lines from stdin (output of `claude -p
#               --output-format=stream-json --verbose`); for every
#               type=="rate_limit_event" line, folds the rate_limit_info fields
#               into the cache (status, resets_at, is_using_overage, source=sdk).
#
# Per-window "last write wins": each writer fills only the fields it knows,
# leaving other fields unchanged. Concurrency: flock on a sibling .lock file
# guards the read-modify-write. Atomic: writes via temp + mv.
#
# Cache path resolution order:
#   1. $MNGR_RATE_LIMITS_CACHE (if set)
#   2. $MNGR_PROFILE_DIR/usage/claude_rate_limits.json (if MNGR_PROFILE_DIR set)
#   3. $HOME/.mngr/profiles/default/usage/claude_rate_limits.json (last resort)
set -euo pipefail

mode="${1:-}"
if [ -z "$mode" ]; then
  echo "usage: $0 (statusline|sdk)" >&2
  exit 64
fi

cache="${MNGR_RATE_LIMITS_CACHE:-}"
if [ -z "$cache" ]; then
  if [ -n "${MNGR_PROFILE_DIR:-}" ]; then
    cache="$MNGR_PROFILE_DIR/usage/claude_rate_limits.json"
  else
    cache="${HOME:-/tmp}/.mngr/profiles/default/usage/claude_rate_limits.json"
  fi
fi

cache_dir=$(dirname "$cache")
mkdir -p "$cache_dir"

lock="$cache.lock"
tmp="$cache.tmp.$$"
now=$(date +%s)

# Drain stdin once so jq processes it; the input stream is consumed.
input=$(cat)

# Read existing cache or default to a minimal valid document.
existing='{"schema_version":1,"windows":{}}'
if [ -f "$cache" ]; then
  existing=$(cat "$cache" 2>/dev/null || echo '{"schema_version":1,"windows":{}}')
  # Validate JSON; reset on corruption.
  if ! printf '%s' "$existing" | jq empty >/dev/null 2>&1; then
    existing='{"schema_version":1,"windows":{}}'
  fi
fi

merge_statusline() {
  # Merge statusline shape: .rate_limits.{five_hour,seven_day,overage}.{used_percentage,resets_at}
  printf '%s' "$existing" | jq \
    --argjson now "$now" \
    --argjson payload "$input" \
    '
    def fold(window_key; src):
      if src == null then .
      else
        .windows[window_key] = (
          (.windows[window_key] // {}) +
          {
            used_percentage: (src.used_percentage // src.utilization // null),
            resets_at: ((src.resets_at // src.reset // null) | if . == null then null else (. | tonumber? // null) end),
            source: "statusline",
            updated_at: $now
          }
        )
      end;
    .schema_version = (.schema_version // 1)
    | .windows = (.windows // {})
    | fold("five_hour"; ($payload.rate_limits.five_hour // null))
    | fold("seven_day"; ($payload.rate_limits.seven_day // null))
    | fold("overage";   ($payload.rate_limits.overage   // null))
    '
}

merge_sdk() {
  # Merge stream-json events; only rate_limit_event lines matter.
  # We process line by line so a multi-event stream folds in order.
  result="$existing"
  while IFS= read -r line || [ -n "$line" ]; do
    [ -z "$line" ] && continue
    type=$(printf '%s' "$line" | jq -r 'try .type // empty' 2>/dev/null)
    if [ "$type" != "rate_limit_event" ]; then
      continue
    fi
    info=$(printf '%s' "$line" | jq -c 'try .rate_limit_info // {}' 2>/dev/null)
    [ -z "$info" ] && continue
    raw_kind=$(printf '%s' "$info" | jq -r 'try .rateLimitType // empty' 2>/dev/null)
    case "$raw_kind" in
      5h|five_hour|fiveHour|FIVE_HOUR) window_key="five_hour" ;;
      7d|seven_day|sevenDay|SEVEN_DAY) window_key="seven_day" ;;
      overage|OVERAGE) window_key="overage" ;;
      *) continue ;;
    esac
    result=$(printf '%s' "$result" | jq \
      --arg key "$window_key" \
      --argjson now "$now" \
      --argjson info "$info" \
      '
      .schema_version = (.schema_version // 1)
      | .windows = (.windows // {})
      | .windows[$key] = (
          (.windows[$key] // {}) +
          {
            status: ($info.status // null),
            resets_at: (($info.resetsAt // $info.resets_at // null) | if . == null then null else (. | tonumber? // null) end),
            is_using_overage: ($info.isUsingOverage // $info.is_using_overage // null),
            source: "sdk",
            updated_at: $now
          }
        )
      ')
  done <<EOF
$input
EOF
  printf '%s' "$result"
}

case "$mode" in
  statusline) merged=$(merge_statusline) ;;
  sdk)        merged=$(merge_sdk) ;;
  *)
    echo "unknown mode: $mode (expected statusline|sdk)" >&2
    exit 64
    ;;
esac

# flock-protected atomic write.
write_cache() {
  printf '%s' "$merged" > "$tmp"
  mv -f "$tmp" "$cache"
}

if command -v flock >/dev/null 2>&1; then
  (
    flock -x 9
    write_cache
  ) 9>"$lock"
else
  # macOS without coreutils flock: best-effort atomic rename without locking.
  write_cache
fi
