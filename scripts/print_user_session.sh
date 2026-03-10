#!/bin/bash
# Print the conversation history for the current agent session.
# Wrapper around filter_transcript.py that discovers session files
# and feeds them through the filter.
#
# Usage: print_user_session.sh [filter_transcript.py options...]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FILTER="$SCRIPT_DIR/filter_transcript.py"
PATHS_SCRIPT="$SCRIPT_DIR/export_transcript_paths.sh"

if [ ! -f "$FILTER" ]; then
    echo "filter_transcript.py not found at $FILTER" >&2
    exit 1
fi

# Discover session files (only tracked + current, no agent_dir scan)
INCLUDE_AGENT_DIR=false bash "$PATHS_SCRIPT" 2>/dev/null | while IFS=$'\t' read -r _source path; do
    [ -f "$path" ] && python3 "$FILTER" "$@" "$path"
done
