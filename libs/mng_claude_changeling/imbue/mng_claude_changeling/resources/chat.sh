#!/bin/bash
# Chat wrapper for changeling conversations.
#
# Manages conversation threads backed by the `llm` CLI tool. Conversation
# metadata (tags, model, created_at) is stored in the changeling_conversations
# table in the llm sqlite database at $LLM_USER_PATH/logs.db.
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
#   MNG_AGENT_STATE_DIR  - agent state directory (contains events/, commands/)
#   MNG_AGENT_WORK_DIR   - agent work directory (contains talking/PROMPT.md)
#   LLM_USER_PATH        - llm data directory (contains logs.db)

set -euo pipefail

AGENT_DATA_DIR="${MNG_AGENT_STATE_DIR:?MNG_AGENT_STATE_DIR must be set}"
LLM_TOOLS_DIR="${MNG_AGENT_STATE_DIR}/commands/llm_tools"
TALKING_PROMPT="${MNG_AGENT_WORK_DIR:-}/talking/PROMPT.md"

# Path to the llm database (LLM_USER_PATH is always set during provisioning)
if [ -z "${LLM_USER_PATH:-}" ]; then
    echo "ERROR: LLM_USER_PATH must be set" >&2
    exit 1
fi
_LLM_DB="${LLM_USER_PATH}/logs.db"

# Configure and source the shared logging library
_MNG_LOG_TYPE="chat"
_MNG_LOG_SOURCE="logs/chat"
_MNG_LOG_FILE="${MNG_AGENT_STATE_DIR}/events/logs/chat/events.jsonl"
# shellcheck source=../../../../mng/imbue/mng/resources/mng_log.sh
source "$MNG_AGENT_STATE_DIR/commands/mng_log.sh"

LOG_FILE="$_MNG_LOG_FILE"

# set this so that we dont get back funny-looking echo output
export LLM_MATCHED_RESPONSE="Thinking..."

# Nanosecond-precision UTC timestamp in ISO 8601 format.
iso_timestamp_ns() {
    date -u +"%Y-%m-%dT%H:%M:%S.%NZ"
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
p = pathlib.Path('${MNG_AGENT_WORK_DIR:-}/changelings.toml')
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

# Insert a conversation record into the changeling_conversations table.
# The model is not stored here -- it lives in the llm conversations table.
# Args: conversation_id, [tags_json]
# tags_json defaults to '{}' if not provided.
insert_conversation_record() {
    local conversation_id="$1"
    local tags="${2:-\{\}}"
    local created_at
    created_at=$(iso_timestamp_ns)

    mng changelingdb insert "$_LLM_DB" "$conversation_id" "$tags" "$created_at"
    log "Inserted conversation record: conversation_id=$conversation_id tags=$tags"
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
        insert_conversation_record "$conversation_id" '{"name":"(new chat)"}'
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
        # to register it in changeling_conversations. Record the current max
        # rowid so the background process can find the NEW conversation.
        local _max_rowid
        _max_rowid=0
        if [ -f "$_LLM_DB" ]; then
            _max_rowid=$(mng changelingdb max-rowid "$_LLM_DB")
        fi

        (
            # Poll for a conversation created after we started (rowid > saved).
            for _i in $(seq 1 60); do
                sleep 1
                if [ -f "$_LLM_DB" ]; then
                    _new_conversation_id=$(mng changelingdb poll-new "$_LLM_DB" "$_max_rowid")
                    if [ -n "$_new_conversation_id" ]; then
                        insert_conversation_record "$_new_conversation_id" '{"name":"(new chat)"}'
                        log "Recorded conversation for new conversation_id=$_new_conversation_id (rowid > $_max_rowid)"
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

    # Get the model from the changeling_conversations table
    local model
    model=$(mng changelingdb lookup-model "$_LLM_DB" "$conversation_id")
    if [ -z "$model" ]; then
        model=$(get_default_model)
    fi

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
    if [ ! -f "$_LLM_DB" ]; then
        echo "No conversations yet."
        return 0
    fi

    # Check if the changeling_conversations table exists and has rows
    local _row_count
    _row_count=$(mng changelingdb count "$_LLM_DB")
    if [ "$_row_count" = "0" ]; then
        echo "No conversations yet."
        return 0
    fi

    log "Listing conversations from $_LLM_DB"

    echo "Conversations:"
    echo "=============="
    python3 -c "
import json, sys, sqlite3
from pathlib import Path

db_path = sys.argv[1]
messages_file = sys.argv[2]

conversations = {}
try:
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    try:
        rows = conn.execute(
            'SELECT cc.conversation_id, c.model, cc.tags, cc.created_at '
            'FROM changeling_conversations cc '
            'LEFT JOIN conversations c ON cc.conversation_id = c.id'
        ).fetchall()
    finally:
        conn.close()
    for conversation_id, model, tags_json, created_at in rows:
        try:
            tags = json.loads(tags_json) if tags_json else {}
        except json.JSONDecodeError:
            tags = {}
        conversations[conversation_id] = {
            'conversation_id': conversation_id,
            'model': model or '?',
            'timestamp': created_at or '?',
            'tags': tags,
        }
except (sqlite3.Error, OSError) as e:
    print(f'WARNING: failed to read conversations from database: {e}', file=sys.stderr)

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

for conversation_id, event in conversations.items():
    event['updated_at'] = updated_at.get(conversation_id, event.get('timestamp', '?'))

sorted_conversations = sorted(conversations.values(), key=lambda r: r.get('updated_at', ''), reverse=True)

for event in sorted_conversations:
    tags = event.get('tags', {})
    tags_str = '  tags=' + json.dumps(tags) if tags else ''
    print(f\"  {event.get('conversation_id','?')}  model={event.get('model', '?')}  created_at={event.get('timestamp', '?')}  updated_at={event.get('updated_at', '?')}{tags_str}\")
" "$_LLM_DB" "${AGENT_DATA_DIR}/events/messages/events.jsonl"

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
