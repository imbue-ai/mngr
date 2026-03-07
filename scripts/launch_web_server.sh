#!/usr/bin/env bash
# Minimal script to launch the changeling web server for local UI iteration.
#
# This sets up a temporary directory structure that satisfies the web server's
# env var requirements, then runs it directly via uv.
#
# Usage:
#   ./scripts/launch_web_server.sh
#
# The server will print its port to stderr. Open http://127.0.0.1:<port> in
# your browser.

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

# LLM data directory (holds logs.db)
LLM_DATA_DIR="$WORK_DIR/llm_data"
mkdir -p "$LLM_DATA_DIR"

# Create a minimal llm logs.db with the changeling_conversations table so the
# conversations page works without a real llm installation.
sqlite3 "$LLM_DATA_DIR/logs.db" <<'SQL'
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    name TEXT,
    model TEXT
);
CREATE TABLE IF NOT EXISTS changeling_conversations (
    conversation_id TEXT PRIMARY KEY,
    tags TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS responses (
    id TEXT PRIMARY KEY,
    system TEXT,
    prompt TEXT,
    response TEXT,
    model TEXT,
    datetime_utc TEXT,
    conversation_id TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    token_details TEXT,
    duration_ms INTEGER
);
SQL

# Agent work directory (contains changelings.toml)
AGENT_WORK_DIR="$WORK_DIR/workdir"
mkdir -p "$AGENT_WORK_DIR"

echo "[launch] Workspace: $WORK_DIR" >&2
echo "[launch] Starting web server..." >&2

export MNG_AGENT_STATE_DIR="$AGENT_STATE_DIR"
export MNG_AGENT_NAME="dev-agent"
export MNG_HOST_NAME="localhost"
export MNG_AGENT_WORK_DIR="$AGENT_WORK_DIR"
export LLM_USER_PATH="$LLM_DATA_DIR"

cd "$REPO_ROOT"
exec uv run python -m imbue.mng_claude_changeling.resources.web_server
