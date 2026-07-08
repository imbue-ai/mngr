#!/usr/bin/env bash
# warm-window.sh -- open a fresh 5h window as soon as the last one has elapsed.
set -euo pipefail

WARMER="window-warmer"
PROJECT_DIR="$HOME/code/my-project"   # any git repo already trusted in Claude Code

# The warmer does no real work, so its repo is irrelevant -- any trusted one
# works. cron starts in $HOME (not a repo), so cd in to give `mngr create` a git
# root.
cd "$PROJECT_DIR"

snapshot="$(mngr usage --format json)"

# Fire only when the last recorded 5h window has already reset -- resets_at in the
# past (vs the snapshot's own `now`) means a fresh window is open and unclaimed.
elapsed="$(jq -r '
  .now as $now
  | .sources[]
  | select(.source == "claude")
  | select((.five_hour.resets_at // 0) > 0 and .five_hour.resets_at < $now)
  | "yes"
' <<<"$snapshot")"

[[ "$elapsed" == "yes" ]] || exit 0

# Clean up any warmer left over from an interrupted run, then spin up a fresh one.
mngr destroy "$WARMER" --force 2>/dev/null || true
mngr create "$WARMER" claude --no-connect -- --model haiku

# One cheap prompt opens the new 5h window. Wait for the turn to finish, then
# destroy the warmer -- it's a throwaway, and its usage events are preserved on
# destroy (the `preserve_on_destroy` usage-plugin option, on by default).
mngr message "$WARMER" --message 'just say hi'
mngr wait "$WARMER" WAITING --timeout 5m
mngr destroy "$WARMER" --force
