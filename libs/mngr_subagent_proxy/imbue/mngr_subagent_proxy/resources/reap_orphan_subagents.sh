#!/usr/bin/env bash
#
# SessionStart hook. Cleans up per-tool_use_id state files left behind
# when a previous parent session died mid-subagent. Also destroys
# any mngr proxy agents that still linger in a terminal lifecycle
# state. Silent cleanup: emits nothing and always exits 0.

set -uo pipefail
umask 077

log_err() {
    printf 'reap_orphan_subagents: %s\n' "$*" >&2
}

trap 'log_err "unexpected failure at line $LINENO"; exit 0' ERR

# Drain stdin; we don't need it.
cat >/dev/null 2>&1 || true

STATE_DIR="${MNGR_AGENT_STATE_DIR:-}"
if [ -z "$STATE_DIR" ]; then
    exit 0
fi

MAP_DIR="$STATE_DIR/subagent_map"
if [ ! -d "$MAP_DIR" ]; then
    exit 0
fi

shopt -s nullglob
MAP_FILES=("$MAP_DIR"/*.json)
shopt -u nullglob
if [ "${#MAP_FILES[@]}" -eq 0 ]; then
    exit 0
fi

if ! command -v jq >/dev/null 2>&1; then
    log_err "jq not available"
    exit 0
fi

# Snapshot current mngr agent list once. Map target_name -> lifecycle_state.
AGENT_LIST_JSON=$(uv run mngr list --format json 2>/dev/null || printf '[]')
if ! printf '%s' "$AGENT_LIST_JSON" | jq -e . >/dev/null 2>&1; then
    AGENT_LIST_JSON='[]'
fi

cleanup_tid() {
    local tid="$1"
    shred -u "$STATE_DIR/proxy_commands/env-${tid}.env" 2>/dev/null \
        || rm -f "$STATE_DIR/proxy_commands/env-${tid}.env" 2>/dev/null || true
    rm -f \
        "$STATE_DIR/subagent_map/${tid}.json" \
        "$STATE_DIR/subagent_prompts/${tid}.md" \
        "$STATE_DIR/subagent_results/${tid}.txt" \
        "$STATE_DIR/proxy_commands/wait-${tid}.sh" \
        "$STATE_DIR/proxy_commands/initialized-${tid}" \
        2>/dev/null || true
}

for map_file in "${MAP_FILES[@]}"; do
    [ -f "$map_file" ] || continue
    base=$(basename "$map_file")
    tid="${base%.json}"
    [ -n "$tid" ] || continue

    target=$(jq -r '.target_name // empty' "$map_file" 2>/dev/null || true)
    if [ -z "$target" ]; then
        cleanup_tid "$tid"
        continue
    fi

    state=$(printf '%s' "$AGENT_LIST_JSON" \
        | jq -r --arg name "$target" '
            (.[]? | select(.name == $name)
                | (.lifecycle_state // .state // .status // ""))
            // ""' \
        2>/dev/null || true)
    state_upper=$(printf '%s' "$state" | tr '[:lower:]' '[:upper:]')

    if [ -z "$state" ]; then
        # Agent no longer exists; just drop the side files.
        cleanup_tid "$tid"
        continue
    fi

    case "$state_upper" in
        DONE|STOPPED|FAILED|DESTROYED|TERMINATED)
            # Detach so the SessionStart hook does not block on mngr destroy
            # latency; mirrors the pattern in rewrite_subagent_result.sh.
            (
                nohup uv run mngr destroy "$target" --yes \
                    >/dev/null 2>>"$STATE_DIR/subagent_destroy.log" &
            ) </dev/null >/dev/null 2>&1
            cleanup_tid "$tid"
            ;;
        *)
            : # still live; leave it alone
            ;;
    esac
done

exit 0
