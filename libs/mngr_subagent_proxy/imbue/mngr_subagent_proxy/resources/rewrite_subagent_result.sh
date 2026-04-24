#!/usr/bin/env bash
#
# PostToolUse:Agent hook. Replaces the Haiku proxy's tool output with
# the real END_TURN content harvested from the mngr subagent, then
# tears down the subagent and cleans up per-tool_use_id state files.
# Exits 0 on any failure so Claude Code keeps running.

set -uo pipefail
umask 077

log_err() {
    printf 'rewrite_subagent_result: %s\n' "$*" >&2
}

trap 'log_err "unexpected failure at line $LINENO"; exit 0' ERR

INPUT=$(cat 2>/dev/null || true)
if [ -z "$INPUT" ]; then
    exit 0
fi

if ! command -v jq >/dev/null 2>&1; then
    log_err "jq not available"
    exit 0
fi

STATE_DIR="${MNGR_AGENT_STATE_DIR:-}"
if [ -z "$STATE_DIR" ]; then
    exit 0
fi

TID=$(printf '%s' "$INPUT" | jq -r '.tool_use_id // empty')
if [ -z "$TID" ]; then
    exit 0
fi

MAP_FILE="$STATE_DIR/subagent_map/${TID}.json"
if [ ! -f "$MAP_FILE" ]; then
    # Native subagent ran (PreToolUse passed through); nothing to do.
    exit 0
fi

TARGET_NAME=$(jq -r '.target_name // empty' "$MAP_FILE" 2>/dev/null || true)

RESULT_FILE="$STATE_DIR/subagent_results/${TID}.txt"
PROMPT_FILE="$STATE_DIR/subagent_prompts/${TID}.md"
SCRIPT_FILE="$STATE_DIR/proxy_commands/wait-${TID}.sh"
ENV_FILE="$STATE_DIR/proxy_commands/env-${TID}.env"
INIT_FLAG="$STATE_DIR/proxy_commands/initialized-${TID}"

if [ -s "$RESULT_FILE" ]; then
    OUTPUT_JSON=$(jq -Rs '.' < "$RESULT_FILE") || OUTPUT_JSON='""'
else
    ERR_MSG="ERROR: mngr subagent ${TARGET_NAME:-<unknown>} produced no result (crashed or proxy failed). Check the mngr agent log."
    OUTPUT_JSON=$(printf '%s' "$ERR_MSG" | jq -Rs '.') || OUTPUT_JSON='"ERROR"'
fi

RESPONSE=$(jq -cn \
    --argjson output "$OUTPUT_JSON" \
    '{hookSpecificOutput: {hookEventName: "PostToolUse", updatedToolOutput: $output}}') || {
    log_err "failed to build response JSON"
    RESPONSE='{"hookSpecificOutput":{"hookEventName":"PostToolUse","updatedToolOutput":"ERROR: rewrite_subagent_result failed to build response"}}'
}

printf '%s\n' "$RESPONSE"

# Best-effort teardown of the mngr subagent. Detach fully so the hook
# returns immediately regardless of mngr destroy latency.
if [ -n "$TARGET_NAME" ]; then
    (
        nohup uv run mngr destroy "$TARGET_NAME" --yes \
            >/dev/null 2>>"$STATE_DIR/subagent_destroy.log" &
    ) </dev/null >/dev/null 2>&1
fi

shred -u "$ENV_FILE" 2>/dev/null || rm -f "$ENV_FILE" 2>/dev/null || true
rm -f \
    "$PROMPT_FILE" \
    "$MAP_FILE" \
    "$RESULT_FILE" \
    "$SCRIPT_FILE" \
    "$INIT_FLAG" \
    2>/dev/null || true

exit 0
