#!/usr/bin/env bash
# User-tester escape hatch for "Backend not yet available. Retrying..." in minds.app.
#
# Symptom: chat panel for one or more agents shows "Backend not yet available,
# Retrying..." indefinitely.
#
# Cause: the `svc-system_interface` tmux window in the agent's Lima VM died
# (workspace_server crashed without auto-restart) OR the workspace_server is
# still booting when minds.app's 60s readiness probe gave up. In either case
# the chat panel keeps polling but never recovers.
#
# What this script does: for each Lima VM, check if port 8000 (the
# workspace_server's bind address inside the VM) is listening. If not,
# respawn the svc-system_interface tmux window with the same command
# services.toml would run. Idempotent and safe to re-run.
#
# Usage:
#   bash apps/minds/scripts/revive-agent-chat.sh
#
# After running, reload the chat tab in minds.app. Typically takes ~5s for
# the workspace_server to bind and the chat panel proxy to go 503 -> 200.

set -uo pipefail

for vm in $(limactl list -q); do
  if limactl shell --workdir / "$vm" -- ss -tln 2>/dev/null | grep -q ':8000\b'; then
    echo "$vm: workspace_server alive, skipping"
    continue
  fi
  AID=$(limactl shell --workdir / "$vm" -- ls /mngr/agents/ 2>/dev/null | head -1)
  SESS=$(limactl shell --workdir / "$vm" -- tmux list-sessions -F '#{session_name}' 2>/dev/null | head -1)
  if [ -z "$AID" ] || [ -z "$SESS" ]; then
    echo "$vm: missing agent id ($AID) or tmux session ($SESS), skipping"
    continue
  fi
  echo "$vm: reviving svc-system_interface for agent $AID, session $SESS..."
  limactl shell --workdir /code "$vm" -- bash -c "printf '%s\n' \
    '#!/bin/bash' \
    'set -a; . /mngr/env 2>/dev/null; . /mngr/agents/$AID/env 2>/dev/null; set +a' \
    'cd /code; exec >> /tmp/svc-system_interface.log 2>&1' \
    'python3 scripts/forward_port.py --url http://localhost:8000 --name system_interface 2>/dev/null || true' \
    'exec minds-workspace-server' \
    > /tmp/restart_si.sh && chmod +x /tmp/restart_si.sh"
  limactl shell --workdir /code "$vm" -- tmux new-window -t "=$SESS" -n svc-system_interface "bash /tmp/restart_si.sh"
done
echo
echo "Done. Reload the chat tab in minds.app -- workspace_server should bind within ~5s."
