#!/usr/bin/env bash
# Chat wrapper for mind conversations.
#
# Manages conversation threads backed by the `llm` CLI tool. Conversation
# metadata (tags, model, created_at) is stored in the mind_conversations
# table in the llm sqlite database at $LLM_USER_PATH/logs.db.
#
# Usage:
#   chat --new --name <name>                        Create or resume a named conversation (user-initiated)
#   chat --new --name <name> --as-agent <message>   Create or resume a named conversation (agent-initiated)
#   chat --resume <conversation-id>                 Resume an existing conversation by ID
#   chat --list                                     List all conversations
#   chat --help                                     Show usage information
#   chat                                            List conversations and show help hint
#
# Environment:
#   MNG_AGENT_STATE_DIR  - agent state directory (contains events/, commands/)
#   MNG_AGENT_WORK_DIR   - agent work directory (contains talking/PROMPT.md)
#   MNG_LLM_MODEL        - model to use for llm commands (default: claude-opus-4.6)
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

# Get the model from MNG_LLM_MODEL env var, falling back to hardcoded default.
get_model() {
    echo "${MNG_LLM_MODEL:-claude-opus-4.6}"
}

generate_conversation_id() {
    echo "conv-$(date +%s)-$(head -c 4 /dev/urandom | xxd -p)"
}

# Insert a conversation record into the mind_conversations table.
# The model is not stored here -- it lives in the llm conversations table.
# Args: conversation_id, [tags_json]
# tags_json defaults to '{}' if not provided.
insert_conversation_record() {
    local conversation_id="$1"
    local tags="${2:-\{\}}"
    local created_at
    created_at=$(iso_timestamp_ns)

    mng llmdb insert "$_LLM_DB" "$conversation_id" "$tags" "$created_at"
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

# Build the llm template YAML file from GLOBAL.md and talking/PROMPT.md.
# The template is written atomically (write to tmp, then move) so it is
# safe to call concurrently.  Returns the template path if a system prompt
# was found, or empty string if no prompt files exist.
build_template() {
    local template_dir="$MNG_AGENT_STATE_DIR/plugin/llm"
    local template_path="$template_dir/template.yml"
    local tmp_path="$template_dir/template.yml.tmp"

    mkdir -p "$template_dir"

    local system_prompt=""
    local global_md="${MNG_AGENT_WORK_DIR:-}/GLOBAL.md"

    if [ -n "${MNG_AGENT_WORK_DIR:-}" ] && [ -f "$global_md" ]; then
        system_prompt="$(cat "$global_md")"
        log "Loaded GLOBAL.md for template (${#system_prompt} chars)"
    fi

    if [ -n "${MNG_AGENT_WORK_DIR:-}" ] && [ -f "$TALKING_PROMPT" ]; then
        if [ -n "$system_prompt" ]; then
            system_prompt="$system_prompt"$'\n\n'"$(cat "$TALKING_PROMPT")"
        else
            system_prompt="$(cat "$TALKING_PROMPT")"
        fi
        log "Loaded talking/PROMPT.md for template (total ${#system_prompt} chars)"
    fi

    if [ -n "$system_prompt" ]; then
        {
            echo "system: |"
            echo "$system_prompt" | sed 's/^/  /'
        } > "$tmp_path"
        mv "$tmp_path" "$template_path"
        log "Built template at $template_path"
        echo "$template_path"
    else
        log "No system prompt files found, no template built"
        # Clean up any stale template
        rm -f "$template_path" "$tmp_path"
        echo ""
    fi
}

# Build a JSON tags object with the given name.
build_tags_json() {
    local name="$1"
    python3 -c "import json, sys; print(json.dumps({'name': sys.argv[1]}))" "$name"
}

# Look up an existing conversation by name. Prints the conversation_id
# if found, or nothing if not found.
lookup_conversation_by_name() {
    local name="$1"
    if [ -f "$_LLM_DB" ]; then
        mng llmdb lookup-by-name "$_LLM_DB" "$name"
    fi
}

new_conversation() {
    local as_agent=false
    local message=""
    local name=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --as-agent)
                as_agent=true
                shift
                if [[ $# -gt 0 && "$1" != --* ]]; then
                    message="$1"
                    shift
                fi
                ;;
            --name)
                shift
                if [[ $# -gt 0 ]]; then
                    name="$1"
                    shift
                fi
                ;;
            *)
                echo "Unknown option for --new: $1" >&2
                exit 1
                ;;
        esac
    done

    if [ -z "$name" ]; then
        echo "ERROR: --name is required with --new" >&2
        exit 1
    fi

    local model
    model=$(get_model)

    log "new_conversation: name=$name model=$model as_agent=$as_agent message_len=${#message}"

    # Check if a conversation with this name already exists
    local existing_id
    existing_id=$(lookup_conversation_by_name "$name")

    if [ -n "$existing_id" ]; then
        log "Found existing conversation for name=$name: $existing_id"
        if [ "$as_agent" = true ]; then
            if [ -n "$message" ]; then
                log "Injecting agent message into existing conversation $existing_id"
                llm inject --cid "$existing_id" -m "$model" "$message"
                log "Agent message injected successfully"
            fi
            echo "$existing_id"
        else
            resume_conversation "$existing_id"
        fi
        return
    fi

    # No existing conversation -- create a new one
    log "No existing conversation for name=$name, creating new"

    local tags
    tags=$(build_tags_json "$name")

    if [ "$as_agent" = true ]; then
        local conversation_id
        conversation_id=$(generate_conversation_id)
        insert_conversation_record "$conversation_id" "$tags"
        if [ -n "$message" ]; then
            log "Injecting agent message into new conversation $conversation_id"
            llm inject --cid "$conversation_id" -m "$model" "$message"
            log "Agent message injected successfully"
        fi
        echo "$conversation_id"
    else
        local tool_args
        tool_args=$(build_tool_args)

        local template_path
        template_path=$(build_template)
        local template_args=()
        if [ -n "$template_path" ]; then
            template_args=(-t "$template_path")
        fi

        log "Starting live-chat session: model=$model tool_args='$tool_args'"

        # llm live-chat creates a conversation in the llm database. We need
        # to register it in mind_conversations. Record the current max
        # rowid so the background process can find the NEW conversation.
        local _max_rowid
        _max_rowid=0
        if [ -f "$_LLM_DB" ]; then
            _max_rowid=$(mng llmdb max-rowid "$_LLM_DB" 2>/dev/null)
        fi

        (
            # Poll for a conversation created after we started (rowid > saved).
            for _i in $(seq 1 60); do
                sleep 1
                if [ -f "$_LLM_DB" ]; then
                    _new_conversation_id=$(mng llmdb poll-new "$_LLM_DB" "$_max_rowid")
                    if [ -n "$_new_conversation_id" ]; then
                        insert_conversation_record "$_new_conversation_id" "$tags"
                        log "Recorded conversation for new conversation_id=$_new_conversation_id (rowid > $_max_rowid)"
                        break
                    fi
                fi
            done
        ) &

        # shellcheck disable=SC2086
        exec llm live-chat -m "$model" "${template_args[@]}" $tool_args
    fi
}

resume_conversation() {
    local conversation_id="$1"
    shift || true

    log "Resuming conversation: conversation_id=$conversation_id"

    # Get the model from the mind_conversations table
    local model
    model=$(mng llmdb lookup-model "$_LLM_DB" "$conversation_id")
    if [ -z "$model" ]; then
        model=$(get_model)
    fi

    log "Resolved model for conversation $conversation_id: $model"

    local tool_args
    tool_args=$(build_tool_args)

    local template_path
    template_path=$(build_template)
    local template_args=()
    if [ -n "$template_path" ]; then
        template_args=(-t "$template_path")
    fi

    log "Starting live-chat session (resume): conversation_id=$conversation_id model=$model tool_args='$tool_args'"
    # shellcheck disable=SC2086
    exec llm live-chat --show-history -c --cid "$conversation_id" -m "$model" "${template_args[@]}" $tool_args
}

list_conversations() {
    if [ ! -f "$_LLM_DB" ]; then
        echo "No conversations yet."
        return 0
    fi

    # Check if the mind_conversations table exists and has rows
    local _row_count
    _row_count=$(mng llmdb count "$_LLM_DB")
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
            'FROM mind_conversations cc '
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
    echo "chat - manage mind conversations"
    echo ""
    echo "Usage:"
    echo "  chat --new --name <name> [--as-agent <msg>]  Create or resume a named conversation"
    echo "  chat --resume <conversation-id>              Resume an existing conversation"
    echo "  chat --list                                  List all conversations"
    echo "  chat --help                                  Show this help message"
    echo ""
    echo "With no arguments, lists conversations (same as --list)."
    echo ""
    echo "Environment:"
    echo "  MNG_LLM_MODEL   Model for llm commands (default: claude-opus-4.6)"
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
