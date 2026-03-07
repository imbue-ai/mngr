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
# The script prints a URL that opens directly into a new conversation.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Create a temporary workspace that mimics what a real agent would have.
TMPDIR_BASE="${TMPDIR:-/tmp}"
WORK_DIR=$(mktemp -d "$TMPDIR_BASE/mng-web-dev.XXXXXX")

cleanup() {
    echo "[launch] Cleaning up $WORK_DIR" >&2
    rm -rf "$WORK_DIR"
}
trap cleanup EXIT

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

export MNG_AGENT_STATE_DIR="$AGENT_STATE_DIR"
export MNG_AGENT_NAME="dev-agent"
export MNG_HOST_NAME="localhost"
export MNG_AGENT_WORK_DIR="$AGENT_WORK_DIR"
export LLM_USER_PATH="$LLM_DATA_DIR"

cd "$REPO_ROOT"

# Start the server in the background so we can extract the port.
uv run python -m imbue.mng_claude_changeling.resources.web_server &
SERVER_PID=$!

# Wait for the server to register itself (writes port to servers/events.jsonl).
SERVERS_FILE="$AGENT_STATE_DIR/events/servers/events.jsonl"
for _ in $(seq 1 30); do
    if [ -s "$SERVERS_FILE" ]; then
        PORT=$(python3 -c "import json; print(json.loads(open('$SERVERS_FILE').readlines()[-1])['url'].split(':')[-1])")
        echo ""
        echo "  http://127.0.0.1:${PORT}/chat?cid=NEW"
        echo ""
        wait "$SERVER_PID"
        exit $?
    fi
    sleep 0.1
done

echo "[launch] ERROR: server did not start within 3 seconds" >&2
kill "$SERVER_PID" 2>/dev/null || true
exit 1
