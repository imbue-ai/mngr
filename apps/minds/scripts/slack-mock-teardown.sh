#!/usr/bin/env bash
# Reverse slack-mock-setup.sh side effects. Best-effort; never fails.
#
# Inputs:
#   VM_NAME   lima VM (optional; if absent, in-VM cleanup is skipped)
set -uo pipefail

VM_NAME="${VM_NAME:-}"
STATE_DIR=/tmp/slack-mock
LIMA_BIN_DIR=/Applications/Minds.app/Contents/Resources/lima/bin
export PATH="$LIMA_BIN_DIR:$PATH"

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

# 3. In-VM cleanup.
if [[ -n "$VM_NAME" ]] && command -v limactl >/dev/null; then
  log "in-VM cleanup for $VM_NAME"
  limactl shell "$VM_NAME" -- sudo bash -c '
    set +e
    sed -i.bak "/# slack-mock/d" /etc/hosts
    rm -f /usr/local/share/ca-certificates/slack-mock.crt
    update-ca-certificates --fresh >/dev/null 2>&1
    rm -f /tmp/slack-mock-ca.crt
    latchkey auth clear slack 2>/dev/null
  ' 2>/dev/null || log "in-VM cleanup non-fatal failure (continuing)"
fi

# 4. Leave cert + logs for artifact upload; only remove on explicit purge.
if [[ "${PURGE:-0}" == "1" ]]; then
  log "PURGE=1: removing $STATE_DIR"
  sudo rm -rf "$STATE_DIR"
fi

log "done"
