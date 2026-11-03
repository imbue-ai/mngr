#!/usr/bin/env bash
# Raw-transcript streamer for grok agents: runs grok_transcript_streamer.py (installed
# beside this script), which tails every root and child session's updates.jsonl into
# logs/grok_transcript/events.jsonl. See that module's docstring for the contract.
#
# Usage: stream_transcript.sh [--single-pass]
#   (no arguments)  stream until the agent state directory is removed
#   --single-pass   stream once and exit (the destroy-time flush)
#
# Environment:
#   MNGR_AGENT_STATE_DIR       - agent state directory (contains commands/, logs/, plugin/grok/)
#   MNGR_CONVERT_LOCK_TIMEOUT  - seconds to wait for the convert lock

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DATA_DIR="${MNGR_AGENT_STATE_DIR:?MNGR_AGENT_STATE_DIR must be set}"

_MNGR_LOG_TYPE="stream_transcript"
_MNGR_LOG_SOURCE="logs/stream_transcript"
_MNGR_LOG_FILE="$AGENT_DATA_DIR/events/logs/stream_transcript/events.jsonl"
# shellcheck source=mngr_log.sh
source "$MNGR_AGENT_STATE_DIR/commands/mngr_log.sh"

if ! command -v python3 >/dev/null 2>&1; then
    log_error "python3 is required to stream grok transcripts but was not found on PATH"
    exit 1
fi

exec python3 "$SCRIPT_DIR/grok_transcript_streamer.py" "$@"
