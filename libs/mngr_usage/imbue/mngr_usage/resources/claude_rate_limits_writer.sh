#!/bin/bash
# Rate-limit cache merge writer for mngr_usage.
#
# Reads a Claude Code statusline JSON object from stdin; extracts
# .rate_limits.{five_hour,seven_day,overage} and folds each window into the
# cache (used_percentage, resets_at, source=statusline). Per-window field-level
# merge: only the fields this writer knows about are touched. This is purely
# defensive -- it preserves any extra fields present in older cache files (or
# any future writer's fields) so a schema bump never silently drops data.
#
# Concurrency: when flock is available, the entire read-modify-write of the
# cache file runs inside an exclusive flock on a sibling .lock file, so
# concurrent writers cannot lose each other's window updates. Without flock
# (e.g. macOS without coreutils), the read-modify-write is best-effort and
# concurrent writers can clobber each other.
# Atomic: writes via temp + mv.
#
# Cache path resolution order:
#   1. $MNGR_RATE_LIMITS_CACHE (if set)
#   2. $MNGR_PROFILE_DIR/usage/claude_rate_limits.json (if MNGR_PROFILE_DIR set)
#   3. $HOME/.mngr/profiles/default/usage/claude_rate_limits.json (last resort)
set -euo pipefail

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

# Read the existing cache, fold the new payload in, and write atomically.
# Must run inside the flock critical section so concurrent writers cannot
# observe the same starting cache and overwrite each other's window updates.
merge_and_write() {
  local existing='{"schema_version":1,"windows":{}}'
  if [ -f "$cache" ]; then
    existing=$(cat "$cache" 2>/dev/null || echo '{"schema_version":1,"windows":{}}')
    # Validate JSON; reset on corruption.
    if ! printf '%s' "$existing" | jq empty >/dev/null 2>&1; then
      existing='{"schema_version":1,"windows":{}}'
    fi
  fi

  local merged
  merged=$(printf '%s' "$existing" | jq \
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
    ')

  printf '%s' "$merged" > "$tmp"
  mv -f "$tmp" "$cache"
}

if command -v flock >/dev/null 2>&1; then
  (
    flock -x 9
    merge_and_write
  ) 9>"$lock"
else
  # macOS without coreutils flock: read-modify-write is not serialized, so
  # concurrent writers can lose updates. Atomic mv still prevents torn JSON.
  merge_and_write
fi
