#!/usr/bin/env bash
# Launch the installed minds.app and wait for its backend to come up.
# Success: ~/.minds/logs/minds-events.jsonl is written within the timeout.
# Failure: timeout, log macOS console + any partial state, exit non-zero.
set -uo pipefail

EVENTS_LOG="$HOME/.minds/logs/minds-events.jsonl"
ELECTRON_LOG="${ELECTRON_LOG:-/tmp/minds-electron.log}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-120}"

log() { printf '[verify] %s\n' "$*" >&2; }

if [[ ! -d /Applications/minds.app ]]; then
  log "FAIL: /Applications/minds.app not installed; run mac-runner-reset.sh with a URL first"
  exit 2
fi

# Launch the binary directly so we can capture Electron's stdout/stderr
# (the desktop_client backend uvicorn process is a child of Electron's
# main process; its stderr is the only place a startup crash or hanging
# `uv run` surfaces). `open /Applications/minds.app` discards that.
# Quarantine was already cleared in mac-runner-reset.sh.
: > "$ELECTRON_LOG"
log "launching Minds binary, stdout/stderr -> $ELECTRON_LOG"
nohup /Applications/minds.app/Contents/MacOS/Minds >"$ELECTRON_LOG" 2>&1 &
ELECTRON_PID=$!
log "  pid=$ELECTRON_PID"

log "waiting up to ${TIMEOUT_SECONDS}s for ${EVENTS_LOG}"
deadline=$(( SECONDS + TIMEOUT_SECONDS ))
while (( SECONDS < deadline )); do
  if [[ -s "$EVENTS_LOG" ]]; then
    log "backend up after $((SECONDS))s, head of events log:"
    head -5 "$EVENTS_LOG"
    exit 0
  fi
  sleep 2
done

log "FAIL: backend did not write events log within ${TIMEOUT_SECONDS}s"
log "Minds processes still alive:"
pgrep -afl '/Applications/minds.app/Contents/' || echo "  (none)"
log "Electron stdout/stderr ($ELECTRON_LOG):"
if [[ -s "$ELECTRON_LOG" ]]; then
  tail -100 "$ELECTRON_LOG"
else
  echo "  (empty)"
fi
log "Recent macOS unified-log entries from the Minds process:"
/usr/bin/log show --process Minds --last 3m 2>/dev/null | tail -40 || true
exit 1
