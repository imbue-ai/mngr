#!/bin/bash
# Conversation watcher for changeling agents.
#
# Syncs messages from the llm database to the standard event log at
# logs/messages/events.jsonl. Works as an ID-based syncer: reads event IDs
# already present in the output file, queries recent responses from the DB
# for all tracked conversations in a single batch, and appends any events
# whose IDs are not yet in the file (in time order).
#
# Each message event includes the full envelope (timestamp, type, event_id,
# source) plus conversation_id and role, making every line self-describing.
#
# Usage: conversation_watcher.sh
#
# Environment:
#   MNG_AGENT_STATE_DIR  - agent state directory (contains logs/)
#   MNG_HOST_DIR         - host data directory (contains logs/ for log output)

set -euo pipefail

AGENT_DATA_DIR="${MNG_AGENT_STATE_DIR:?MNG_AGENT_STATE_DIR must be set}"
HOST_DIR="${MNG_HOST_DIR:?MNG_HOST_DIR must be set}"
CONVERSATIONS_EVENTS="$AGENT_DATA_DIR/logs/conversations/events.jsonl"
MESSAGES_EVENTS="$AGENT_DATA_DIR/logs/messages/events.jsonl"
LOG_FILE="$HOST_DIR/logs/conversation_watcher.log"

# Read poll interval from settings.toml, fall back to default
mkdir -p "$(dirname "$LOG_FILE")"
POLL_INTERVAL=$(python3 -c "
import tomllib, pathlib, sys
p = pathlib.Path('${MNG_AGENT_STATE_DIR}/settings.toml')
try:
    s = tomllib.loads(p.read_text()) if p.exists() else {}
    print(s.get('watchers', {}).get('conversation_poll_interval_seconds', 5))
except Exception as e:
    print(f'WARNING: failed to load settings: {e}', file=sys.stderr)
    print(5)
" 2>>"$LOG_FILE" || echo 5)

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

get_llm_db_path() {
    local db_path
    db_path=$(llm logs path 2>/dev/null || echo "")
    if [ -z "$db_path" ]; then
        local llm_user_path="${LLM_USER_PATH:-$HOME/.config/io.datasette.llm}"
        db_path="$llm_user_path/logs.db"
    fi
    echo "$db_path"
}

# Sync missing messages from the llm DB to logs/messages/events.jsonl.
#
# Uses Python with the built-in sqlite3 module for a single-pass, ID-based
# sync across all tracked conversations. Instead of querying each conversation
# individually, this does one batch query and deduplicates by event_id.
#
# Uses an adaptive window: starts by fetching the most recent 200 responses
# from the DB and checks which event IDs are missing from the output file.
# If ALL fetched events are missing (suggesting the file is far behind),
# doubles the window and retries until it finds events already in the file
# or runs out of DB rows.
sync_messages() {
    local db_path="$1"

    if [ ! -f "$db_path" ]; then
        log_debug "LLM database not found at $db_path"
        return
    fi

    local sync_stderr
    sync_stderr=$(mktemp)
    local result
    result=$(_CONVERSATIONS_FILE="$CONVERSATIONS_EVENTS" \
             _MESSAGES_FILE="$MESSAGES_EVENTS" \
             _DB_PATH="$db_path" \
             python3 << 'SYNC_SCRIPT' 2>"$sync_stderr" || true
import json
import os
import sqlite3
import sys


def sync():
    conversations_file = os.environ["_CONVERSATIONS_FILE"]
    messages_file = os.environ["_MESSAGES_FILE"]
    db_path = os.environ["_DB_PATH"]

    # Get tracked conversation IDs from logs/conversations/events.jsonl
    tracked_cids = set()
    if os.path.isfile(conversations_file):
        with open(conversations_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    tracked_cids.add(json.loads(line)["conversation_id"])
                except (json.JSONDecodeError, KeyError) as e:
                    print(f"WARNING: malformed conversation event line: {e}", file=sys.stderr)
                    continue

    if not tracked_cids:
        print("0")
        return

    # Get all event IDs already in messages/events.jsonl
    file_event_ids = set()
    if os.path.isfile(messages_file):
        with open(messages_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    file_event_ids.add(json.loads(line)["event_id"])
                except (json.JSONDecodeError, KeyError) as e:
                    print(f"WARNING: malformed message event line: {e}", file=sys.stderr)
                    continue

    if not os.path.isfile(db_path):
        print("0")
        return

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as e:
        print(f"WARNING: cannot open database: {e}", file=sys.stderr)
        print("0")
        return

    placeholders = ",".join("?" for _ in tracked_cids)
    cid_list = list(tracked_cids)

    # Adaptive window: start with 200 responses, double if all events are
    # missing (meaning we may need to look further back in the DB).
    window = 200
    missing_events = []

    while True:
        try:
            rows = conn.execute(
                f"SELECT id, datetime_utc, conversation_id, prompt, response "
                f"FROM responses "
                f"WHERE conversation_id IN ({placeholders}) "
                f"ORDER BY datetime_utc DESC "
                f"LIMIT ?",
                [*cid_list, window],
            ).fetchall()
        except sqlite3.Error as e:
            print(f"WARNING: sqlite3 query error: {e}", file=sys.stderr)
            break

        if not rows:
            break

        missing_events = []
        found_existing = False

        for row_id, ts, cid, prompt, response in rows:
            if prompt:
                eid = f"{row_id}-user"
                if eid in file_event_ids:
                    found_existing = True
                else:
                    missing_events.append((ts, 0, json.dumps({
                        "timestamp": ts,
                        "type": "message",
                        "event_id": eid,
                        "source": "messages",
                        "conversation_id": cid,
                        "role": "user",
                        "content": prompt,
                    }, separators=(",", ":"))))

            if response:
                eid = f"{row_id}-assistant"
                if eid in file_event_ids:
                    found_existing = True
                else:
                    missing_events.append((ts, 1, json.dumps({
                        "timestamp": ts,
                        "type": "message",
                        "event_id": eid,
                        "source": "messages",
                        "conversation_id": cid,
                        "role": "assistant",
                        "content": response,
                    }, separators=(",", ":"))))

        # If we found at least one event already in the file, we have looked
        # far enough back. If ALL events were missing and we got a full window
        # of rows, there may be even older missing events -- double and retry.
        if found_existing or len(rows) < window:
            break

        window *= 2

    conn.close()

    if not missing_events:
        print("0")
        return

    # Sort by (timestamp, sort_order) and append to file
    missing_events.sort(key=lambda x: (x[0], x[1]))

    os.makedirs(os.path.dirname(messages_file), exist_ok=True)
    with open(messages_file, "a") as f:
        for _, _, event_json in missing_events:
            f.write(event_json + "\n")

    print(str(len(missing_events)))


sync()
SYNC_SCRIPT
)

    if [ -s "$sync_stderr" ]; then
        log "WARNING: sync error: $(cat "$sync_stderr")"
    fi
    rm -f "$sync_stderr"

    local synced="${result:-0}"
    if [ "$synced" -gt 0 ] 2>/dev/null; then
        log "Synced $synced new message event(s) -> logs/messages/events.jsonl"
    else
        log_debug "No new messages to sync"
    fi
}

main() {
    mkdir -p "$(dirname "$MESSAGES_EVENTS")"

    local db_path
    db_path=$(get_llm_db_path)

    log "Conversation watcher started"
    log "  Agent data dir: $AGENT_DATA_DIR"
    log "  LLM database: $db_path"
    log "  Conversations events: $CONVERSATIONS_EVENTS"
    log "  Messages events: $MESSAGES_EVENTS"
    log "  Log file: $LOG_FILE"
    log "  Poll interval: ${POLL_INTERVAL}s"

    if command -v inotifywait &>/dev/null && [ -f "$db_path" ]; then
        log "Using inotifywait for file watching"
        while true; do
            inotifywait -q -t "$POLL_INTERVAL" -e modify,create "$db_path" "$CONVERSATIONS_EVENTS" 2>/dev/null || true
            db_path=$(get_llm_db_path)
            sync_messages "$db_path"
        done
    else
        if ! command -v inotifywait &>/dev/null; then
            log "inotifywait not available, using polling"
        elif [ ! -f "$db_path" ]; then
            log "LLM database not yet created at $db_path, using polling"
        fi
        while true; do
            db_path=$(get_llm_db_path)
            sync_messages "$db_path"
            sleep "$POLL_INTERVAL"
        done
    fi
}

main
