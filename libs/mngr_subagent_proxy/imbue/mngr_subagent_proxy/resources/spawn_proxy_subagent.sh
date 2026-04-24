#!/usr/bin/env bash
#
# PreToolUse:Agent hook. Rewrites the Task tool invocation to route
# through an mngr-managed proxy subagent instead of Claude's native
# nested Agent loop. On any error, pass through so the native Task
# tool runs unchanged.

set -uo pipefail
umask 077

PASS_THROUGH='{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}'

log_err() {
    printf 'spawn_proxy_subagent: %s\n' "$*" >&2
}

emit_pass_through() {
    printf '%s\n' "$PASS_THROUGH"
    exit 0
}

emit_depth_limit_pass_through() {
    local depth="$1"
    local max_depth="$2"
    log_err "depth ${depth}/${max_depth} reached, passing through to native Task"
    printf '%s\n' "{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"allow\",\"systemMessage\":\"mngr_subagent_proxy: depth limit (${depth}/${max_depth}) reached; running native Claude Task instead of mngr-owned subagent.\"}}"
    exit 0
}

trap 'log_err "unexpected failure at line $LINENO"; emit_pass_through' ERR

INPUT=$(cat 2>/dev/null || true)
if [ -z "$INPUT" ]; then
    log_err "empty stdin"
    emit_pass_through
fi

if ! command -v jq >/dev/null 2>&1; then
    log_err "jq not available"
    emit_pass_through
fi

STATE_DIR="${MNGR_AGENT_STATE_DIR:-}"
if [ -z "$STATE_DIR" ]; then
    log_err "MNGR_AGENT_STATE_DIR unset"
    emit_pass_through
fi

PARENT_NAME="${MNGR_AGENT_NAME:-}"
if [ -z "$PARENT_NAME" ]; then
    log_err "MNGR_AGENT_NAME unset"
    emit_pass_through
fi

# Depth guard: skip proxy if we are already nested too deep.
DEPTH="${MNGR_SUBAGENT_DEPTH:-0}"
MAX_DEPTH="${MNGR_MAX_SUBAGENT_DEPTH:-3}"
case "$DEPTH" in ''|*[!0-9]*) DEPTH=0 ;; esac
case "$MAX_DEPTH" in ''|*[!0-9]*) MAX_DEPTH=3 ;; esac
if [ "$DEPTH" -ge "$MAX_DEPTH" ]; then
    emit_depth_limit_pass_through "$DEPTH" "$MAX_DEPTH"
fi

TOOL_USE_ID=$(printf '%s' "$INPUT" | jq -r '.tool_use_id // empty')
ORIG_PROMPT=$(printf '%s' "$INPUT" | jq -r '.tool_input.prompt // empty')
ORIG_DESC=$(printf '%s' "$INPUT" | jq -r '.tool_input.description // empty')
ORIG_SUBAGENT_TYPE=$(printf '%s' "$INPUT" | jq -r '.tool_input.subagent_type // empty')
ORIG_RUN_BG=$(printf '%s' "$INPUT" | jq -r '.tool_input.run_in_background // false')

if [ -z "$TOOL_USE_ID" ] || [ -z "$ORIG_PROMPT" ]; then
    log_err "missing tool_use_id or prompt in hook input"
    emit_pass_through
fi

# Slug: lowercase, non-alnum -> '-', collapse repeats, trim, cap 30.
slugify() {
    printf '%s' "$1" \
        | tr '[:upper:]' '[:lower:]' \
        | tr -c 'a-z0-9' '-' \
        | tr -s '-' \
        | sed -e 's/^-//' -e 's/-$//' \
        | cut -c1-30 \
        | sed -e 's/-$//'
}

SLUG=$(slugify "${ORIG_DESC:-subagent}")
if [ -z "$SLUG" ]; then
    SLUG="subagent"
fi

TID_SUFFIX="${TOOL_USE_ID: -8}"
TARGET_NAME="${PARENT_NAME}--subagent-${SLUG}-${TID_SUFFIX}"

PARENT_CWD=$(pwd -P 2>/dev/null || pwd)

PROMPTS_DIR="$STATE_DIR/subagent_prompts"
MAP_DIR="$STATE_DIR/subagent_map"
CMD_DIR="$STATE_DIR/proxy_commands"
RESULTS_DIR="$STATE_DIR/subagent_results"

if ! mkdir -p "$PROMPTS_DIR" "$MAP_DIR" "$CMD_DIR" "$RESULTS_DIR"; then
    log_err "failed to create state subdirs under $STATE_DIR"
    emit_pass_through
fi

PROMPT_FILE="$PROMPTS_DIR/${TOOL_USE_ID}.md"
MAP_FILE="$MAP_DIR/${TOOL_USE_ID}.json"
SCRIPT_FILE="$CMD_DIR/wait-${TOOL_USE_ID}.sh"

# Write the original prompt verbatim (not JSON-encoded).
if ! printf '%s' "$ORIG_PROMPT" > "$PROMPT_FILE"; then
    log_err "failed to write prompt file $PROMPT_FILE"
    emit_pass_through
fi
chmod 600 "$PROMPT_FILE" 2>/dev/null || true

# Record the map entry.
if ! jq -cn \
    --arg target "$TARGET_NAME" \
    --arg subagent_type "$ORIG_SUBAGENT_TYPE" \
    --arg parent_cwd "$PARENT_CWD" \
    --argjson run_in_background "$ORIG_RUN_BG" \
    '{target_name: $target, subagent_type: $subagent_type, parent_cwd: $parent_cwd, run_in_background: $run_in_background}' \
    > "$MAP_FILE"; then
    log_err "failed to write map file $MAP_FILE"
    emit_pass_through
fi
chmod 600 "$MAP_FILE" 2>/dev/null || true

# Generate per-tool_use_id wait-script. Values are baked in as literals
# (shell-quoted via printf %q) so the script does not depend on the
# hook's env at run-time beyond MNGR_AGENT_STATE_DIR / MNGR_SUBAGENT_DEPTH.
Q_TID=$(printf '%q' "$TOOL_USE_ID")
Q_TARGET=$(printf '%q' "$TARGET_NAME")
Q_PARENT_CWD=$(printf '%q' "$PARENT_CWD")

{
    printf '%s\n' '#!/usr/bin/env bash'
    printf '%s\n' 'set -euo pipefail'
    printf '%s\n' 'umask 077'
    printf '\n'
    printf 'TID=%s\n' "$Q_TID"
    printf 'TARGET_NAME=%s\n' "$Q_TARGET"
    printf 'PARENT_CWD=%s\n' "$Q_PARENT_CWD"
    printf '%s\n' 'STATE_DIR="${MNGR_AGENT_STATE_DIR:?MNGR_AGENT_STATE_DIR not set}"'
    printf '%s\n' 'ENV_FILE="$STATE_DIR/proxy_commands/env-$TID.env"'
    printf '%s\n' 'INIT_FLAG="$STATE_DIR/proxy_commands/initialized-$TID"'
    printf '%s\n' 'PROMPT_FILE="$STATE_DIR/subagent_prompts/$TID.md"'
    printf '%s\n' 'RESULT_FILE="$STATE_DIR/subagent_results/$TID.txt"'
    printf '\n'
    printf '%s\n' 'if [ ! -f "$INIT_FLAG" ]; then'
    printf '%s\n' '    env | grep -Ev '"'"'^(MNGR_AGENT_STATE_DIR|MNGR_AGENT_NAME|MAIN_CLAUDE_SESSION_ID|MNGR_HOST_DIR)='"'"' > "$ENV_FILE"'
    printf '%s\n' '    uv run mngr create "$TARGET_NAME:$PARENT_CWD" \'
    printf '%s\n' '        --agent-type claude \'
    printf '%s\n' '        --transfer=none \'
    printf '%s\n' '        --no-ensure-clean \'
    printf '%s\n' '        --no-connect \'
    printf '%s\n' '        --env-file "$ENV_FILE" \'
    printf '%s\n' '        --message-file "$PROMPT_FILE" \'
    printf '%s\n' '        --env MNGR_SUBAGENT_DEPTH=$((${MNGR_SUBAGENT_DEPTH:-0}+1))'
    printf '%s\n' '    shred -u "$ENV_FILE" 2>/dev/null || rm -f "$ENV_FILE"'
    printf '%s\n' '    touch "$INIT_FLAG"'
    printf '%s\n' 'fi'
    printf '\n'
    printf '%s\n' 'mkdir -p "$(dirname "$RESULT_FILE")"'
    printf '%s\n' 'output=$(uv run python -m imbue.mngr_subagent_proxy.subagent_wait "$TARGET_NAME")'
    printf '%s\n' 'case "$output" in'
    printf '%s\n' '    END_TURN:*)'
    printf '%s\n' '        printf '"'"'%s'"'"' "${output#END_TURN:}" > "$RESULT_FILE"'
    printf '%s\n' '        echo "DONE"'
    printf '%s\n' '        ;;'
    printf '%s\n' '    PERMISSION_REQUIRED:*)'
    printf '%s\n' '        echo "NEED_PERMISSION: $TARGET_NAME"'
    printf '%s\n' '        ;;'
    printf '%s\n' '    *)'
    printf '%s\n' '        echo "ERROR: unexpected subagent_wait output: $output" >&2'
    printf '%s\n' '        exit 1'
    printf '%s\n' '        ;;'
    printf '%s\n' 'esac'
} > "$SCRIPT_FILE"

chmod 755 "$SCRIPT_FILE" 2>/dev/null || true

NEW_PROMPT=$(printf 'MNGR_PROXY_AGENT=%s\nMNGR_PROXY_SCRIPT=%s\n\nRun Bash($MNGR_PROXY_SCRIPT). See mngr-proxy agent for details.' \
    "$TARGET_NAME" "$SCRIPT_FILE")

RESPONSE=$(jq -cn \
    --arg description "$ORIG_DESC" \
    --arg prompt "$NEW_PROMPT" \
    --argjson run_in_background "$ORIG_RUN_BG" \
    '{
        hookSpecificOutput: {
            hookEventName: "PreToolUse",
            permissionDecision: "allow",
            updatedInput: {
                description: $description,
                subagent_type: "mngr-proxy",
                prompt: $prompt,
                run_in_background: $run_in_background
            }
        }
    }') || {
    log_err "failed to build response JSON"
    emit_pass_through
}

printf '%s\n' "$RESPONSE"
exit 0
