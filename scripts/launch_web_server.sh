#!/usr/bin/env bash
# Minimal script to launch the changeling web server for local UI iteration.
#
# This sets up a temporary directory structure that satisfies the web server's
# env var requirements, then runs it. The database tables are created
# automatically by llm on first use.
#
# Usage:
#   ./scripts/launch_web_server.sh
#
# Then open: http://127.0.0.1:8787/chat?cid=NEW

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PORT=8787

# Create a temporary workspace that mimics what a real agent would have.
TMPDIR_BASE="${TMPDIR:-/tmp}"
WORK_DIR=$(mktemp -d "$TMPDIR_BASE/mng-web-dev.XXXXXX")

# Agent state directory (holds events/ subdirectories)
AGENT_STATE_DIR="$WORK_DIR/agent_state"
mkdir -p "$AGENT_STATE_DIR/events/servers"
mkdir -p "$AGENT_STATE_DIR/events/messages"

# LLM data directory (holds logs.db -- tables created automatically by llm)
LLM_DATA_DIR="$WORK_DIR/llm_data"
mkdir -p "$LLM_DATA_DIR"

# Agent work directory (contains changelings.toml)
AGENT_WORK_DIR="$WORK_DIR/workdir"
mkdir -p "$AGENT_WORK_DIR"

export UV_TOOL_BIN_DIR="$(dirname "$(which mng)")"
export UV_TOOL_DIR="$(dirname "$UV_TOOL_BIN_DIR")"
export MNG_AGENT_STATE_DIR="$AGENT_STATE_DIR"
export MNG_AGENT_NAME="dev-agent"
export MNG_HOST_NAME="localhost"
export MNG_AGENT_WORK_DIR="$AGENT_WORK_DIR"
export LLM_USER_PATH="$LLM_DATA_DIR"
export WEB_SERVER_PORT="$PORT"

cd "$REPO_ROOT"

SERVER_PID=""

stop_server() {
    if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null
        wait "$SERVER_PID" 2>/dev/null || true
        SERVER_PID=""
    fi
}

start_server() {
    uv run python -m imbue.mng_claude_changeling.resources.web_server < /dev/null &
    SERVER_PID=$!
    echo "[launch] Server started (PID $SERVER_PID)"
}

cleanup() {
    stop_server
    echo "[launch] Cleaning up $WORK_DIR" >&2
    rm -rf "$WORK_DIR"
}
trap cleanup EXIT

echo ""
echo "  http://127.0.0.1:${PORT}/chat?cid=NEW"
echo ""
echo "  Press Enter to restart the web server, Ctrl-C to quit."
echo ""

start_server

while true; do
    read -r || break
    echo "[launch] Restarting web server..."
    stop_server
    start_server
    echo "[launch] Web server restarted."
done
