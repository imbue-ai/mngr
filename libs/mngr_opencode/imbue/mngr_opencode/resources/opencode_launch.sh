#!/usr/bin/env bash
# Launch orchestrator for an opencode agent.
#
# mngr drives opencode as a client-server pair rather than typing into a TUI:
#
#   1. a headless `opencode serve` (the SERVER) -- the lifecycle plugin's event
#      hook runs here, maintaining the active marker + raw transcript; and
#   2. an `opencode attach` TUI CLIENT in the foreground -- this is what the user
#      sees via `mngr connect`, and what the pane's process-name lifecycle
#      detection keys off (it reports `opencode`).
#
# Messages are delivered by POSTing to the server's HTTP API (see the plugin's
# send_message), and the attached client renders them, so the conversation is
# fully visible. To make that possible the session must exist before the client
# attaches, so this script pre-creates it (or reuses the recorded one on restart)
# and records its id; the server's actual bound port is recorded too so
# send_message knows where to POST.
#
# Args: any extra args are forwarded to `opencode attach` (user cli_args/agent_args).
#
# Environment (set by mngr's assemble_command):
#   MNGR_AGENT_STATE_DIR   - agent state dir (holds the port/session files, logs)
#   MNGR_OPENCODE_BIN      - the opencode command (e.g. "opencode")
#   MNGR_OPENCODE_PORT     - the per-agent port to ask the server to bind
#   MNGR_OPENCODE_WORKDIR  - URL-encoded directory for the session-create query
#                            (mngr encodes it in Python before passing it here)
#   OPENCODE_CONFIG_DIR / XDG_DATA_HOME - per-agent isolation (inherited by serve+attach)

set -uo pipefail

STATE="${MNGR_AGENT_STATE_DIR:?MNGR_AGENT_STATE_DIR must be set}"
BIN="${MNGR_OPENCODE_BIN:?MNGR_OPENCODE_BIN must be set}"
PORT="${MNGR_OPENCODE_PORT:?MNGR_OPENCODE_PORT must be set}"
WORKDIR="${MNGR_OPENCODE_WORKDIR:?MNGR_OPENCODE_WORKDIR must be set}"

ROOT_SESSION_FILE="$STATE/opencode_root_session"
PORT_FILE="$STATE/opencode_server_port"
READY_SENTINEL="$STATE/opencode_ready"
SERVER_LOG="$STATE/logs/opencode_server.log"
mkdir -p "$STATE/logs"

# Clear any stale readiness sentinel from a prior run before we (re)start, so
# wait_for_ready_signal can't return early against an old marker.
rm -f "$READY_SENTINEL"

# Start the headless server. MNGR_OPENCODE_ROLE=server is scoped to this command
# so the lifecycle plugin acts here and stays inert in the attach client below.
MNGR_OPENCODE_ROLE=server "$BIN" serve --port "$PORT" --hostname 127.0.0.1 >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

# Stop the server when the foreground client exits (mngr's stop also kills the
# whole pane process tree; this is the tidy path for a normal client quit).
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT INT TERM

# Wait for the server to report its listening URL, and capture the real bound port.
BOUND_PORT=""
for _ in $(seq 1 150); do
    BOUND_PORT=$(grep -oE 'listening on http://127\.0\.0\.1:[0-9]+' "$SERVER_LOG" 2>/dev/null \
        | grep -oE '[0-9]+$' | head -n 1)
    [ -n "$BOUND_PORT" ] && break
    kill -0 "$SERVER_PID" 2>/dev/null || break
    sleep 0.2
done
if [ -z "$BOUND_PORT" ]; then
    echo "opencode_launch.sh: server did not report a listening port; see $SERVER_LOG" >&2
    exit 1
fi
printf '%s' "$BOUND_PORT" > "$PORT_FILE"
BASE_URL="http://127.0.0.1:$BOUND_PORT"

# Reuse the recorded root session across stop/start; otherwise create one and
# record its id (so send_message can POST to it and so the next start resumes it).
# WORKDIR is already URL-encoded by mngr (Python) for the query value.
SESSION_ID=$(cat "$ROOT_SESSION_FILE" 2>/dev/null || true)
if [ -z "$SESSION_ID" ]; then
    SESSION_ID=$(curl -s -X POST "$BASE_URL/session?directory=$WORKDIR" \
        -H 'content-type: application/json' -d '{}' 2>/dev/null \
        | sed -n 's/.*"id":"\(ses_[A-Za-z0-9]*\)".*/\1/p' | head -n 1)
    if [ -z "$SESSION_ID" ]; then
        echo "opencode_launch.sh: failed to create an opencode session via $BASE_URL" >&2
        exit 1
    fi
    printf '%s' "$SESSION_ID" > "$ROOT_SESSION_FILE"
fi

# Signal readiness: the server is up and the session exists, so the agent can
# accept messages (which are delivered over the HTTP API, not by typing into the
# TUI). This is what wait_for_ready_signal polls for -- a real signal from the
# launch script rather than scraping the attach client's footer.
: > "$READY_SENTINEL"

# Liveness watcher: the attach client does NOT exit when the server dies, so
# without this a dead server would leave a live `opencode attach` in the pane and
# lifecycle detection would keep reporting RUNNING/WAITING against a broken agent.
# When the server exits, tear the whole pane process group down (this script is
# non-interactive, so the backgrounded watcher shares the group) so the pane goes
# DONE and lifecycle honestly reflects that the agent is gone.
( while kill -0 "$SERVER_PID" 2>/dev/null; do sleep 2; done; kill 0 2>/dev/null ) &
WATCHER_PID=$!

# Attach the TUI client to that session in the foreground. This is the pane the
# user interacts with via `mngr connect`; messages POSTed to the server render here.
"$BIN" attach "$BASE_URL" --session "$SESSION_ID" "$@"

# Normal client quit: stop the watcher (the EXIT trap stops the server).
kill "$WATCHER_PID" 2>/dev/null || true
