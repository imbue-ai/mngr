#!/usr/bin/env bash
# use-extra.sh -- run a dedicated agent through the tail of a 5h window, then
# stop it once the window or weekly pace rolls over.
set -euo pipefail

AGENT="my-agent"

snapshot="$(mngr usage --format json)"

# From account-level usage, classify what the agent should be doing. We keep
# age-stale readings (a quiet account is when there's leftover budget) but treat
# an already-reset 5h window (seconds_until_reset <= 0) as a reason to stop -- its
# cached numbers are from the previous window. Emit one of:
#   START -- (re)launch: in the tail of an open 5h window (>90% elapsed) with
#            spare capacity (5h budget left, weekly under the pace line above).
#   STOP  -- shut down: the 5h window left its tail / rolled over, OR weekly usage
#            passes the strict pace line, used% > elapsed%.
#   KEEP  -- hold the current state: still in the tail and under the strict weekly
#            line, but not eligible to (re)launch -- weekly is in the hysteresis band,
#            or the 5h budget is already spent.
#   ""    -- no Claude usage data this tick: do nothing.
# The two lines differ on purpose: the gap is hysteresis, so a running agent isn't
# stopped the moment it nudges past the START margin.
status="$(jq -r '
  .sources[]
  | select(.source == "claude")
  | (.five_hour.seconds_until_reset // 0) as $open
  | (.five_hour.elapsed_percentage // 0)  as $el5
  | (.five_hour.used_percentage // 100)   as $u5
  | (.seven_day.elapsed_percentage // 0)  as $elw
  | (.seven_day.used_percentage // 100)   as $uw
  | if   ($open <= 0 or $el5 <= 90 or $uw > $elw)                    then "STOP"
    elif ($u5 < 80 and $uw < $elw * (1 - 0.30 * (100 - $elw) / 100)) then "START"
    else                                                                 "KEEP"
    end
' <<<"$snapshot")"

# Branch on the agent's current lifecycle state.
state="$(mngr list --include "name == \"$AGENT\"" --format json | jq -r '.agents[0].state // "MISSING"')"

case "$state" in
  STOPPED)
    # Launch into the window's tail; the next tick's STOP shuts us down once it rolls.
    if [[ "$status" == "START" ]]; then
      mngr start "$AGENT" && mngr message "$AGENT" --message "continue where you left off"
    fi
    ;;
  RUNNING | WAITING)
    # Stop only on an explicit STOP (window left its tail, or weekly pace caught
    # up). KEEP -- or empty, i.e. no data this tick -- leaves it running.
    if [[ "$status" == "STOP" ]]; then
      mngr stop "$AGENT"
    fi
    ;;
  *)
    : # MISSING / DONE / REPLACED / UNKNOWN -- leave it alone.
    ;;
esac
