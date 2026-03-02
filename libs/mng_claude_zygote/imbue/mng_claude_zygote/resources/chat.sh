#!/bin/bash
# Chat wrapper for changeling conversations.
#
# Manages conversation threads backed by the `llm` CLI tool. Events are
# written to the standard log structure: logs/<source>/events.jsonl
#
# Event sources used:
#   logs/conversations/events.jsonl  - conversation lifecycle (created, model changed)
#
# Usage:
#   chat --new [message]              Create a new conversation (user-initiated)
#   chat --new --as-agent [message]   Create a new conversation (agent-initiated)
#   chat --resume <cid>              Resume an existing conversation
#   chat --list                      List all conversations
#   chat --help                      Show usage information
#   chat                             List conversations and show help hint
#
# Environment:
#   MNG_AGENT_STATE_DIR  - agent state directory (contains logs/)
#   MNG_HOST_DIR         - host data directory (contains commands/)
#   MNG_AGENT_WORK_DIR   - agent work directory (contains talking/PROMPT.md)

set -euo pipefail

AGENT_DATA_DIR="${MNG_AGENT_STATE_DIR:?MNG_AGENT_STATE_DIR must be set}"
CONVERSATIONS_EVENTS="$AGENT_DATA_DIR/logs/conversations/events.jsonl"
LLM_TOOLS_DIR="${MNG_HOST_DIR:?MNG_HOST_DIR must be set}/commands/llm_tools"
LOG_FILE="${MNG_HOST_DIR}/logs/chat.log"
TALKING_PROMPT="${MNG_AGENT_WORK_DIR:-}/talking/PROMPT.md"

# Nanosecond-precision UTC timestamp in ISO 8601 format.
iso_timestamp_ns() {
    date -u +"%Y-%m-%dT%H:%M:%S.%NZ"
}

# Generate a unique event ID (timestamp-based + random hex for uniqueness)
generate_event_id() {
    echo "evt-$(date +%s%N)-$(head -c 4 /dev/urandom | xxd -p)"
}

# Log a message to the log file (not to stdout, since chat is interactive)
log() {
    local ts
    ts=$(iso_timestamp_ns)
    mkdir -p "$(dirname "$LOG_FILE")"
    echo "[$ts] $*" >> "$LOG_FILE"
}

get_default_model() {
    python3 -c "
import tomllib, pathlib, sys
p = pathlib.Path('${MNG_AGENT_STATE_DIR}/settings.toml')
try:
    s = tomllib.loads(p.read_text()) if p.exists() else {}
    print(s.get('chat', {}).get('model', 'claude-opus-4-6'))
except Exception as e:
    print(f'WARNING: failed to load settings: {e}', file=sys.stderr)
    print('claude-opus-4-6')
" 2>>"$LOG_FILE" || echo "claude-opus-4-6"
}

generate_cid() {
    echo "conv-$(date +%s)-$(head -c 4 /dev/urandom | xxd -p)"
}

# Append a conversation_created event to logs/conversations/events.jsonl
# Uses the standard envelope: timestamp, type, event_id, source + conversation fields
append_conversation_event() {
    local cid="$1"
    local model="$2"
    local event_type="${3:-conversation_created}"
    local timestamp
    timestamp=$(iso_timestamp_ns)
    local event_id
    event_id=$(generate_event_id)
    mkdir -p "$(dirname "$CONVERSATIONS_EVENTS")"
    printf '{"timestamp":"%s","type":"%s","event_id":"%s","source":"conversations","conversation_id":"%s","model":"%s"}\n' \
        "$timestamp" "$event_type" "$event_id" "$cid" "$model" >> "$CONVERSATIONS_EVENTS"
    log "Appended event: type=$event_type cid=$cid model=$model event_id=$event_id"
}

build_tool_args() {
    local args=""
    if [ -f "$LLM_TOOLS_DIR/context_tool.py" ]; then
        args="$args --functions $LLM_TOOLS_DIR/context_tool.py"
    fi
    if [ -f "$LLM_TOOLS_DIR/extra_context_tool.py" ]; then
        args="$args --functions $LLM_TOOLS_DIR/extra_context_tool.py"
    fi
    echo "$args"
}

# Build the system prompt from talking/PROMPT.md (and GLOBAL.md if present).
# Returns the prompt text, or empty string if no prompt file is found.
build_system_prompt() {
    local prompt=""
    local global_md="${MNG_AGENT_WORK_DIR:-}/GLOBAL.md"

    if [ -n "${MNG_AGENT_WORK_DIR:-}" ] && [ -f "$global_md" ]; then
        prompt="$(cat "$global_md")"
        log "Loaded GLOBAL.md system prompt (${#prompt} chars)"
    fi

    if [ -n "${MNG_AGENT_WORK_DIR:-}" ] && [ -f "$TALKING_PROMPT" ]; then
        if [ -n "$prompt" ]; then
            prompt="$prompt"$'\n\n'"$(cat "$TALKING_PROMPT")"
        else
            prompt="$(cat "$TALKING_PROMPT")"
        fi
        log "Loaded talking/PROMPT.md system prompt (total ${#prompt} chars)"
    else
        log "No talking prompt found at $TALKING_PROMPT"
    fi

    echo "$prompt"
}

new_conversation() {
    local as_agent=false
    local message=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --as-agent) as_agent=true; shift ;;
            *) message="$1"; shift ;;
        esac
    done

    local model
    model=$(get_default_model)
    local cid
    cid=$(generate_cid)

    log "Creating new conversation: cid=$cid model=$model as_agent=$as_agent message_len=${#message}"

    append_conversation_event "$cid" "$model" "conversation_created"

    # Build system prompt args for llm live-chat only (llm inject does not support -s).
    local system_prompt
    system_prompt=$(build_system_prompt)
    local sys_args=()
    if [ -n "$system_prompt" ]; then
        sys_args=(-s "$system_prompt")
    fi

    if [ "$as_agent" = true ]; then
        if [ -n "$message" ]; then
            log "Injecting agent message into conversation $cid"
            llm inject --cid "$cid" -m "$model" "$message"
            log "Agent message injected successfully"
        fi
        echo "$cid"
    else
        local tool_args
        tool_args=$(build_tool_args)
        log "Starting live-chat session: cid=$cid model=$model tool_args='$tool_args'"
        if [ -n "$message" ]; then
            # shellcheck disable=SC2086
            exec llm live-chat --cid "$cid" -m "$model" "${sys_args[@]}" $tool_args "$message"
        else
            # shellcheck disable=SC2086
            exec llm live-chat --cid "$cid" -m "$model" "${sys_args[@]}" $tool_args
        fi
    fi
}

resume_conversation() {
    local cid="$1"
    shift

    log "Resuming conversation: cid=$cid"

    # Get the model from the latest event for this conversation
    local model
    model=$(grep -F "\"conversation_id\":\"$cid\"" "$CONVERSATIONS_EVENTS" 2>/dev/null \
        | tail -1 \
        | jq -r '.model' 2>/dev/null \
        || get_default_model)

    log "Resolved model for conversation $cid: $model"

    local tool_args
    tool_args=$(build_tool_args)
    local system_prompt
    system_prompt=$(build_system_prompt)
    local sys_args=()
    if [ -n "$system_prompt" ]; then
        sys_args=(-s "$system_prompt")
    fi
    log "Starting live-chat session (resume): cid=$cid model=$model tool_args='$tool_args'"
    # shellcheck disable=SC2086
    exec llm live-chat --show-history -c --cid "$cid" -m "$model" "${sys_args[@]}" $tool_args
}

list_conversations() {
    if [ ! -f "$CONVERSATIONS_EVENTS" ]; then
        echo "No conversations yet."
        return 0
    fi

    log "Listing conversations from $CONVERSATIONS_EVENTS"

    echo "Conversations:"
    echo "=============="
    python3 -c "
import json, os, sys
from pathlib import Path

events_file = '$CONVERSATIONS_EVENTS'
messages_file = '${AGENT_DATA_DIR}/logs/messages/events.jsonl'
convs = {}
line_num = 0
for line in open(events_file):
    line_num += 1
    line = line.strip()
    if not line:
        continue
    try:
        event = json.loads(line)
        cid = event['conversation_id']
        convs[cid] = event
    except (json.JSONDecodeError, KeyError) as e:
        print(f'  WARNING: malformed line {line_num} in {events_file}: {e}', file=sys.stderr)
        print(f'    line content: {line[:200]}', file=sys.stderr)
        continue

# Find latest message timestamp per conversation from messages events
updated_at = {}
if Path(messages_file).exists():
    for line in open(messages_file):
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            cid = msg.get('conversation_id', '')
            ts = msg.get('timestamp', '')
            if cid and ts:
                if cid not in updated_at or ts > updated_at[cid]:
                    updated_at[cid] = ts
        except (json.JSONDecodeError, KeyError) as e:
            print(f'WARNING: malformed message event line: {e}', file=sys.stderr)
            continue

for cid, event in convs.items():
    event['updated_at'] = updated_at.get(cid, event.get('timestamp', '?'))

sorted_convs = sorted(convs.values(), key=lambda r: r.get('updated_at', ''), reverse=True)

for event in sorted_convs:
    print(f\"  {event.get('conversation_id','?')}  model={event.get('model', '?')}  created_at={event.get('timestamp', '?')}  updated_at={event.get('updated_at', '?')}\")
"

    log "Listed conversations"
}

show_help() {
    echo "chat - manage changeling conversations"
    echo ""
    echo "Usage:"
    echo "  chat --new [--as-agent] [message]   Create a new conversation"
    echo "  chat --resume <conversation-id>     Resume an existing conversation"
    echo "  chat --list                         List all conversations"
    echo "  chat --help                         Show this help message"
    echo ""
    echo "With no arguments, lists conversations (same as --list)."
}

log "Invoked with args: $*"

case "${1:-}" in
    --new)
        shift
        new_conversation "$@"
        ;;
    --resume)
        shift
        if [ -z "${1:-}" ]; then
            echo "Usage: chat --resume <conversation-id>" >&2
            exit 1
        fi
        resume_conversation "$@"
        ;;
    --list)
        list_conversations
        ;;
    --help|-h)
        show_help
        ;;
    "")
        list_conversations
        echo ""
        echo "Run 'chat --help' for more options."
        ;;
    *)
        echo "Unknown option: $1" >&2
        echo "Run 'chat --help' for usage information." >&2
        exit 1
        ;;
esac
