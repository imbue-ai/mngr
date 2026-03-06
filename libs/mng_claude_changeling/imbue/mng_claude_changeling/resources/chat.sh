#!/bin/bash
# Chat wrapper for changeling conversations.
#
# Manages conversation threads backed by the `llm` CLI tool. Events are
# written to the standard log structure: events/<source>/events.jsonl
#
# Event sources used:
#   events/conversations/events.jsonl  - conversation lifecycle (created, model changed)
#
# Usage:
#   chat --new [message]              Create a new conversation (user-initiated)
#   chat --new --as-agent [message]   Create a new conversation (agent-initiated)
#   chat --resume <conversation-id>  Resume an existing conversation
#   chat --list                      List all conversations
#   chat --help                      Show usage information
#   chat                             List conversations and show help hint
#
# Environment:
#   MNG_AGENT_STATE_DIR  - agent state directory (contains events/)
#   MNG_HOST_DIR         - host data directory (contains commands/)
#   MNG_AGENT_WORK_DIR   - agent work directory (contains talking/PROMPT.md)

set -euo pipefail

AGENT_DATA_DIR="${MNG_AGENT_STATE_DIR:?MNG_AGENT_STATE_DIR must be set}"
CONVERSATIONS_EVENTS="$AGENT_DATA_DIR/events/conversations/events.jsonl"
LLM_TOOLS_DIR="${MNG_HOST_DIR:?MNG_HOST_DIR must be set}/commands/llm_tools"
TALKING_PROMPT="${MNG_AGENT_WORK_DIR:-}/talking/PROMPT.md"

# Configure and source the shared logging library
_MNG_LOG_TYPE="chat"
_MNG_LOG_SOURCE="logs/chat"
_MNG_LOG_FILE="${MNG_HOST_DIR}/events/logs/chat/events.jsonl"
# shellcheck source=../../../../mng/imbue/mng/resources/mng_log.sh
source "$MNG_HOST_DIR/commands/mng_log.sh"

LOG_FILE="$_MNG_LOG_FILE"

# Nanosecond-precision UTC timestamp in ISO 8601 format.
iso_timestamp_ns() {
    date -u +"%Y-%m-%dT%H:%M:%S.%NZ"
}

# Generate a unique event ID (random UUID4 hex with evt- prefix)
generate_event_id() {
    echo "evt-$(head -c 16 /dev/urandom | xxd -p)"
}

# Log a message to the log file (not to stdout, since chat is interactive)
log() {
    log_info "$*"
}

get_default_model() {
    local _stderr_file
    _stderr_file=$(mktemp)
    local _model
    _model=$(python3 -c "
import tomllib, pathlib, sys
p = pathlib.Path('${MNG_AGENT_WORK_DIR:-}/.changelings/settings.toml')
if p.exists():
    try:
        s = tomllib.loads(p.read_text())
        model = s.get('chat', {}).get('model')
        if model:
            print(model)
            sys.exit(0)
    except Exception as e:
        print(f'WARNING: failed to load settings from {p}: {e}', file=sys.stderr)
print('claude-opus-4.6')
" 2>"$_stderr_file") || true
    if [ -s "$_stderr_file" ]; then
        log_error "Failed to load settings: $(cat "$_stderr_file")"
    fi
    rm -f "$_stderr_file"
    echo "${_model:-claude-opus-4.6}"
}

generate_conversation_id() {
    echo "conv-$(date +%s)-$(head -c 4 /dev/urandom | xxd -p)"
}

# Append a conversation_created event to events/conversations/events.jsonl
# Uses the standard envelope: timestamp, type, event_id, source + conversation fields
# Optional 4th argument is a JSON object of tags (e.g. '{"daily":"2026-03-04"}')
append_conversation_event() {
    local conversation_id="$1"
    local model="$2"
    local event_type="${3:-conversation_created}"
    local tags="${4:-}"
    local timestamp
    timestamp=$(iso_timestamp_ns)
    local event_id
    event_id=$(generate_event_id)
    mkdir -p "$(dirname "$CONVERSATIONS_EVENTS")"
    if [ -n "$tags" ]; then
        printf '{"timestamp":"%s","type":"%s","event_id":"%s","source":"conversations","conversation_id":"%s","model":"%s","tags":%s}\n' \
            "$timestamp" "$event_type" "$event_id" "$conversation_id" "$model" "$tags" >> "$CONVERSATIONS_EVENTS"
    else
        printf '{"timestamp":"%s","type":"%s","event_id":"%s","source":"conversations","conversation_id":"%s","model":"%s"}\n' \
            "$timestamp" "$event_type" "$event_id" "$conversation_id" "$model" >> "$CONVERSATIONS_EVENTS"
    fi
    log "Appended event: type=$event_type conversation_id=$conversation_id model=$model event_id=$event_id tags=$tags"
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
    local conversation_id
    conversation_id=$(generate_conversation_id)

    log "Creating new conversation: conversation_id=$conversation_id model=$model as_agent=$as_agent message_len=${#message}"

    # Build system prompt args for llm live-chat only (llm inject does not support -s).
    local system_prompt
    system_prompt=$(build_system_prompt)
    local sys_args=()
    if [ -n "$system_prompt" ]; then
        sys_args=(-s "$system_prompt")
    fi

    if [ "$as_agent" = true ]; then
        append_conversation_event "$conversation_id" "$model" "conversation_created"
        if [ -n "$message" ]; then
            log "Injecting agent message into conversation $conversation_id"
            llm inject --cid "$conversation_id" -m "$model" "$message"
            log "Agent message injected successfully"
        fi
        echo "$conversation_id"
    else
        local tool_args
        tool_args=$(build_tool_args)
        log "Starting live-chat session: model=$model tool_args='$tool_args'"

        # llm live-chat creates a conversation in the llm database. We need
        # to register the correct conversation ID in our events file. Record
        # the current max rowid so the background process can find the NEW
        # conversation (rather than picking up a stale one from a prior session).
        local _llm_db _max_rowid
        _llm_db=$(llm logs path 2>/dev/null || echo "")
        _max_rowid=0
        if [ -n "$_llm_db" ] && [ -f "$_llm_db" ]; then
            _max_rowid=$(sqlite3 "$_llm_db" "SELECT COALESCE(MAX(rowid), 0) FROM conversations" 2>/dev/null || echo "0")
        fi

        (
            # Poll for a conversation created after we started (rowid > saved).
            for _i in $(seq 1 60); do
                sleep 1
                if [ -n "$_llm_db" ] && [ -f "$_llm_db" ]; then
                    _new_conversation_id=$(sqlite3 "$_llm_db" \
                        "SELECT id FROM conversations WHERE rowid > $_max_rowid ORDER BY rowid ASC LIMIT 1" \
                        2>/dev/null || true)
                    if [ -n "$_new_conversation_id" ]; then
                        append_conversation_event "$_new_conversation_id" "$model" "conversation_created"
                        log "Recorded conversation event for new conversation_id=$_new_conversation_id (rowid > $_max_rowid)"
                        break
                    fi
                fi
            done
        ) &

        # shellcheck disable=SC2086
        if [ -n "$message" ]; then
            exec llm live-chat -m "$model" "${sys_args[@]}" $tool_args "$message"
        else
            exec llm live-chat -m "$model" "${sys_args[@]}" $tool_args
        fi
    fi
}

resume_conversation() {
    local conversation_id="$1"
    shift

    log "Resuming conversation: conversation_id=$conversation_id"

    # Get the model from the latest event for this conversation
    local model
    model=$(grep -F "\"conversation_id\":\"$conversation_id\"" "$CONVERSATIONS_EVENTS" 2>/dev/null \
        | tail -1 \
        | jq -r '.model' 2>/dev/null \
        || get_default_model)

    log "Resolved model for conversation $conversation_id: $model"

    local tool_args
    tool_args=$(build_tool_args)
    local system_prompt
    system_prompt=$(build_system_prompt)
    local sys_args=()
    if [ -n "$system_prompt" ]; then
        sys_args=(-s "$system_prompt")
    fi
    log "Starting live-chat session (resume): conversation_id=$conversation_id model=$model tool_args='$tool_args'"
    # shellcheck disable=SC2086
    exec llm live-chat --show-history -c --cid "$conversation_id" -m "$model" "${sys_args[@]}" $tool_args
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
messages_file = '${AGENT_DATA_DIR}/events/messages/events.jsonl'
conversations = {}
line_num = 0
for line in open(events_file):
    line_num += 1
    line = line.strip()
    if not line:
        continue
    try:
        event = json.loads(line)
        conversation_id = event['conversation_id']
        conversations[conversation_id] = event
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
            message = json.loads(line)
            conversation_id = message.get('conversation_id', '')
            ts = message.get('timestamp', '')
            if conversation_id and ts:
                if conversation_id not in updated_at or ts > updated_at[conversation_id]:
                    updated_at[conversation_id] = ts
        except (json.JSONDecodeError, KeyError) as e:
            print(f'WARNING: malformed message event line: {e}', file=sys.stderr)
            continue

# Filter out internal conversations (tagged with 'internal')
visible_conversations = {conversation_id: e for conversation_id, e in conversations.items() if 'internal' not in e.get('tags', {})}

for conversation_id, event in visible_conversations.items():
    event['updated_at'] = updated_at.get(conversation_id, event.get('timestamp', '?'))

sorted_conversations = sorted(visible_conversations.values(), key=lambda r: r.get('updated_at', ''), reverse=True)

for event in sorted_conversations:
    tags = event.get('tags', {})
    tags_str = '  tags=' + json.dumps(tags) if tags else ''
    print(f\"  {event.get('conversation_id','?')}  model={event.get('model', '?')}  created_at={event.get('timestamp', '?')}  updated_at={event.get('updated_at', '?')}{tags_str}\")
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
