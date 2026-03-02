#!/bin/bash
# Event watcher for changeling agents.
#
# Watches event log files (logs/<source>/events.jsonl) for new entries and
# sends unhandled events to the primary agent via `mng message`.
#
# Watched sources:
#   logs/messages/events.jsonl     - conversation messages
#   logs/scheduled/events.jsonl    - scheduled trigger events
#   logs/mng_agents/events.jsonl   - agent state transitions
#   logs/stop/events.jsonl         - agent stop events
#
# Each event in these files includes the standard envelope (timestamp, type,
# event_id, source) so the watcher can format meaningful messages.
#
# Usage: event_watcher.sh
#
# Environment:
#   MNG_AGENT_STATE_DIR  - agent state directory (contains logs/)
#   MNG_AGENT_NAME       - name of the primary agent to send messages to
#   MNG_HOST_DIR         - host data directory (contains logs/ for log output)

set -euo pipefail

AGENT_DATA_DIR="${MNG_AGENT_STATE_DIR:?MNG_AGENT_STATE_DIR must be set}"
AGENT_NAME="${MNG_AGENT_NAME:?MNG_AGENT_NAME must be set}"
HOST_DIR="${MNG_HOST_DIR:?MNG_HOST_DIR must be set}"
# All event sources the watcher monitors for new events
LOGS_DIR="$AGENT_DATA_DIR/logs"
OFFSETS_DIR="$AGENT_DATA_DIR/logs/.event_offsets"
LOG_FILE="$HOST_DIR/logs/event_watcher.log"

# Read settings from settings.toml, fall back to defaults
mkdir -p "$(dirname "$LOG_FILE")"
_SETTINGS_JSON=$(python3 -c "
import tomllib, pathlib, json, sys
p = pathlib.Path('${MNG_AGENT_STATE_DIR}/settings.toml')
try:
    s = tomllib.loads(p.read_text()) if p.exists() else {}
    w = s.get('watchers', {})
    print(json.dumps({
        'poll': w.get('event_poll_interval_seconds', 3),
        'sources': w.get('watched_event_sources', ['messages', 'scheduled', 'mng_agents', 'stop'])
    }))
except Exception as e:
    print(f'WARNING: failed to load settings: {e}', file=sys.stderr)
    print(json.dumps({'poll': 3, 'sources': ['messages', 'scheduled', 'mng_agents', 'stop']}))
" 2>>"$LOG_FILE" || echo '{"poll": 3, "sources": ["messages", "scheduled", "mng_agents", "stop"]}')

POLL_INTERVAL=$(echo "$_SETTINGS_JSON" | python3 -c "import json, sys; print(json.load(sys.stdin)['poll'])" 2>/dev/null || echo 3)

# Read watched sources as a bash array
_SOURCES_STR=$(echo "$_SETTINGS_JSON" | python3 -c "import json, sys; print(' '.join(json.load(sys.stdin)['sources']))" 2>/dev/null || echo "messages scheduled mng_agents stop")
# shellcheck disable=SC2206
WATCHED_SOURCES=($_SOURCES_STR)

log() {
    local ts
    ts=$(date -u +"%Y-%m-%dT%H:%M:%S.%NZ")
    local msg="[$ts] $*"
    echo "$msg"
    mkdir -p "$(dirname "$LOG_FILE")"
    echo "$msg" >> "$LOG_FILE"
}

log_debug() {
    local ts
    ts=$(date -u +"%Y-%m-%dT%H:%M:%S.%NZ")
    mkdir -p "$(dirname "$LOG_FILE")"
    echo "[$ts] [debug] $*" >> "$LOG_FILE"
}

# Check for new lines in an events.jsonl file and send them to the primary agent.
# The offset file uses the parent directory name as key (the source name).
check_and_send_new_events() {
    local file="$1"
    # Use the parent directory name as the source identifier
    local source_dir
    source_dir=$(basename "$(dirname "$file")")
    local offset_file="$OFFSETS_DIR/$source_dir.offset"

    local current_offset=0
    if [ -f "$offset_file" ]; then
        current_offset=$(cat "$offset_file")
    fi

    if [ ! -f "$file" ]; then
        return
    fi

    local total_lines
    total_lines=$(wc -l < "$file" 2>/dev/null || echo 0)
    total_lines=$(echo "$total_lines" | tr -d '[:space:]')

    if [ "$total_lines" -le "$current_offset" ]; then
        return
    fi

    local new_count=$((total_lines - current_offset))
    local new_lines
    new_lines=$(tail -n +"$((current_offset + 1))" "$file" | head -n "$new_count")

    if [ -z "$new_lines" ]; then
        return
    fi

    log "Found $new_count new event(s) from source '$source_dir' (offset $current_offset -> $total_lines)"
    log_debug "New events from $source_dir: $(echo "$new_lines" | head -c 500)"

    local message
    message="New $source_dir event(s):
$new_lines"

    log "Sending $new_count event(s) from '$source_dir' to agent '$AGENT_NAME'"
    local send_stderr
    send_stderr=$(mktemp)
    if uv run mng message "$AGENT_NAME" -m "$message" 2>"$send_stderr"; then
        echo "$total_lines" > "$offset_file"
        log "Events sent successfully, offset updated to $total_lines"
    else
        log "ERROR: failed to send events from $source_dir to $AGENT_NAME: $(cat "$send_stderr")"
    fi
    rm -f "$send_stderr"
}

check_all_sources() {
    for source in "${WATCHED_SOURCES[@]}"; do
        local events_file="$LOGS_DIR/$source/events.jsonl"
        if [ -f "$events_file" ]; then
            check_and_send_new_events "$events_file"
        fi
    done
}

main() {
    mkdir -p "$OFFSETS_DIR"

    log "Event watcher started"
    log "  Agent data dir: $AGENT_DATA_DIR"
    log "  Agent name: $AGENT_NAME"
    log "  Watched sources: ${WATCHED_SOURCES[*]}"
    log "  Offsets dir: $OFFSETS_DIR"
    log "  Log file: $LOG_FILE"
    log "  Poll interval: ${POLL_INTERVAL}s"

    if command -v inotifywait &>/dev/null; then
        log "Using inotifywait for file watching"
        while true; do
            local watch_dirs=()
            for source in "${WATCHED_SOURCES[@]}"; do
                watch_dirs+=("$LOGS_DIR/$source")
            done

            log_debug "Waiting for file changes in: ${watch_dirs[*]}"
            inotifywait -q -r -t "$POLL_INTERVAL" -e modify,create "${watch_dirs[@]}" 2>/dev/null || true
            check_all_sources
        done
    else
        log "inotifywait not available, using polling"
        while true; do
            check_all_sources
            sleep "$POLL_INTERVAL"
        done
    fi
}

main
