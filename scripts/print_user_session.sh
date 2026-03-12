#!/usr/bin/env bash
# Print out Claude Code conversation history in a way that is easier to read and analyze.
# Prints all sessions in chronological order using the session ID history file.
#
# Uses export_transcript.sh (from the mng resources) for raw JSONL extraction,
# then applies filtering and formatting.

set -euo pipefail

# Locate the export_transcript.sh script
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
EXPORT_SCRIPT="$REPO_ROOT/libs/mng/imbue/mng/resources/export_transcript.sh"

if [ ! -f "$EXPORT_SCRIPT" ]; then
    echo "export_transcript.sh not found at $EXPORT_SCRIPT" >&2
    exit 1
fi

# Extract raw JSONL, then filter and format
bash "$EXPORT_SCRIPT" | \
  grep -v "tool_use_id" | \
  grep -v 'content":"<' | \
  grep -v '"type":"progress"' | \
  grep -v '"type":"thinking"' | \
  grep -v '"type":"tool_use"' | \
  grep -v '"type":"system"' | \
  grep user | \
  jq '{type: .type, content:  .message.content}' | \
  jq -s 'reduce .[] as $msg ([]; if length > 0 and .[-1].type == "assistant" and $msg.type == "assistant" then .[-1].content[0].text += "\n\n" + $msg.content[0].text else . + [$msg] end)'
