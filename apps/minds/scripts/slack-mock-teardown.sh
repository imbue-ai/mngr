#!/usr/bin/env bash
# Reverse slack-mock-setup.sh side effects. Best-effort; never fails.
set -uo pipefail

STATE_DIR=/tmp/slack-mock
LATCHKEY_BIN=/Applications/Minds.app/Contents/Resources/latchkey/bin/latchkey
export MINDS_ELECTRON_EXEC_PATH=/Applications/Minds.app/Contents/MacOS/Minds

log() { printf '[slack-mock-teardown] %s\n' "$*" >&2; }

# 1. Stop socat (root-owned PID).
if [[ -f "$STATE_DIR/socat.pid" ]]; then
  PID=$(cat "$STATE_DIR/socat.pid" 2>/dev/null || echo "")
  if [[ -n "$PID" ]]; then
    log "stopping socat pid=$PID"
    sudo kill "$PID" 2>/dev/null || true
  fi
  sudo rm -f "$STATE_DIR/socat.pid"
fi

# 2. Stop mock (user-owned PID).
if [[ -f "$STATE_DIR/mock.pid" ]]; then
  PID=$(cat "$STATE_DIR/mock.pid" 2>/dev/null || echo "")
  if [[ -n "$PID" ]]; then
    log "stopping mock pid=$PID"
    kill "$PID" 2>/dev/null || true
  fi
  rm -f "$STATE_DIR/mock.pid"
fi

# 3. /etc/hosts cleanup.
log "removing slack-mock /etc/hosts entry"
sudo sed -i.bak '/# slack-mock/d' /etc/hosts 2>/dev/null || true

# 4. Remove trusted cert. -c matches by common name.
log "removing trusted cert (CN=slack.com)"
sudo security delete-certificate -c "slack.com" \
  /Library/Keychains/System.keychain 2>/dev/null || true

# 5. Clear pre-seeded latchkey slack cred so it doesn't bleed across runs.
log "clearing latchkey slack auth"
LATCHKEY_DIRECTORY="$HOME/.minds/latchkey" \
  "$LATCHKEY_BIN" auth clear slack 2>/dev/null || true

# 6. Leave cert + logs for artifact upload; only remove on explicit purge.
if [[ "${PURGE:-0}" == "1" ]]; then
  log "PURGE=1: removing $STATE_DIR"
  sudo rm -rf "$STATE_DIR"
fi

log "done"
