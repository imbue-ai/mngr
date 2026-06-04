#!/usr/bin/env bash
# Raw-transcript streaming for antigravity agents.
#
# Antigravity writes one JSONL transcript per conversation at
# $ANTIGRAVITY_APP_DATA_DIR/brain/<conv_id>/.system_generated/logs/transcript.jsonl.
# Multiple agy instances on the same host share that brain/ directory, so a
# naive watch-all would interleave foreign conversations into our output.
#
# To scope the watch to *this* agent, we read the per-agent conversation-ids
# file that the PreInvocation capture hook maintains
# (capture_conversation_id.sh; see CONVERSATION_IDS_FILENAME in
# antigravity_config.py). Every uuid recorded there is owned by this agent.
# New conversation IDs can appear at any time -- /fork, /new, /switch, resume
# -- so the discovery step re-runs every poll cycle. (Earlier versions grepped
# agy's --log-file for `Created conversation <uuid>` lines; the hook is the
# single source of truth now, which also avoids depending on agy's log
# wording.)
#
# Per-conversation offsets are stored in
# <agent-state-dir>/plugin/antigravity/.transcript_offsets/<conv_id>
# (uuids are already filename-safe; no percent encoding needed) so the
# script can resume efficiently after restarts. Antigravity's JSONL records
# carry `step_index` which is unique only within a conversation (not across
# conversations), so we cannot use the mngr_transcript_reconcile_offset
# helper that mngr_claude relies on -- it builds a global id set from the
# merged output and would treat duplicate step_indexes as already-emitted.
# Instead we trust the stored offset; if a crash occurred between an emit
# and the matching `_save_offset`, restart will re-emit at most the
# duplicate lines in question. The downstream common_transcript.sh dedupes
# by event_id so terminal output is not corrupted.
#
# Output is the raw bytes agy wrote: this script never rewrites or
# reschematises content. The common_transcript.sh converter reads from
# the raw output produced here.
#
# Usage: stream_transcript.sh [--single-pass]
#
# Environment:
#   MNGR_AGENT_STATE_DIR        - agent state directory (contains commands/)
#   ANTIGRAVITY_APP_DATA_DIR    - agy app-data dir (default ~/.gemini/antigravity-cli)

set -euo pipefail

AGENT_DATA_DIR="${MNGR_AGENT_STATE_DIR:?MNGR_AGENT_STATE_DIR must be set}"
APP_DATA_DIR="${ANTIGRAVITY_APP_DATA_DIR:-$HOME/.gemini/antigravity-cli}"
# Conversation-ids file written by capture_conversation_id.sh; kept in sync
# with CONVERSATION_IDS_FILENAME in antigravity_config.py.
CONVERSATION_IDS_FILE="$AGENT_DATA_DIR/antigravity_conversation_ids"
OUTPUT_FILE="$AGENT_DATA_DIR/logs/antigravity_transcript/events.jsonl"
OFFSET_DIR="$AGENT_DATA_DIR/plugin/antigravity/.transcript_offsets"
POLL_INTERVAL=1

mkdir -p "$(dirname "$OUTPUT_FILE")" "$OFFSET_DIR"
touch "$OUTPUT_FILE"

# Configure and source the shared logging library
_MNGR_LOG_TYPE="stream_transcript"
_MNGR_LOG_SOURCE="logs/stream_transcript"
_MNGR_LOG_FILE="$AGENT_DATA_DIR/events/logs/stream_transcript/events.jsonl"
# shellcheck source=mngr_log.sh
source "$MNGR_AGENT_STATE_DIR/commands/mngr_log.sh"

# Keys are conversation UUIDs; values are line counts already emitted.
declare -A _OFFSET_BY_ID=()

_line_count() {
    if [ -f "$1" ]; then
        wc -l < "$1"
    else
        echo 0
    fi
}

_load_stored_offset() {
    if [ -f "$OFFSET_DIR/$1" ]; then
        cat "$OFFSET_DIR/$1"
    else
        echo 0
    fi
}

_save_offset() {
    echo "$2" > "$OFFSET_DIR/$1"
}

# Transcript path for a given conversation UUID.
_transcript_path() {
    echo "$APP_DATA_DIR/brain/$1/.system_generated/logs/transcript.jsonl"
}

# Discover conversation IDs owned by this agent from the capture-hook file.
#
# capture_conversation_id.sh appends a uuid to CONVERSATION_IDS_FILE whenever
# the active conversation changes (new session, /fork, /new, /switch, resume).
# We read every distinct uuid from it. The file is re-read on every poll
# cycle; this is cheap relative to the JSONL emit step and keeps the
# implementation stateless. The grep validates the uuid shape defensively so a
# stray line can't inject a bogus conversation id.
#
# Echoes one uuid per line.
_find_conversation_ids() {
    if [ ! -f "$CONVERSATION_IDS_FILE" ]; then
        return 0
    fi
    # uuid pattern: 8-4-4-4-12 hex chars
    grep -oE "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$" "$CONVERSATION_IDS_FILE" 2>/dev/null \
        | sort -u
}

# Append new lines from a transcript file to the output.
#
# Each line is augmented with `_mngr_conv_id` so the downstream
# common_transcript.sh can correlate tool calls with their results inside
# the same conversation (Antigravity's `step_index` is conversation-scoped,
# not globally unique, so the merged output needs the id added back).
# Uses a bounded line-range read to avoid a TOCTOU race between the
# wc and the read (any lines appended in between are deferred to the
# next poll cycle).
_emit_new_lines() {
    local conv_id="$1"
    local transcript_file
    transcript_file=$(_transcript_path "$conv_id")
    if [ ! -f "$transcript_file" ]; then
        return
    fi
    local offset="${_OFFSET_BY_ID[$conv_id]:-0}"

    local file_lines
    file_lines=$(_line_count "$transcript_file")
    if [ "$file_lines" -le "$offset" ]; then
        return
    fi

    local start=$((offset + 1))
    local end="$file_lines"
    local new_count=$((file_lines - offset))

    # Augment each line with _mngr_conv_id. Skip blank lines and lines
    # that fail to parse rather than aborting the whole emit -- agy may
    # write a partial line mid-flush which the next cycle will pick up
    # in full.
    sed -n "${start},${end}p" "$transcript_file" | _MNGR_CONV_ID="$conv_id" python3 -c '
import json, os, sys
conv_id = os.environ["_MNGR_CONV_ID"]
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        sys.stderr.write("skipping malformed transcript line\n")
        continue
    if not isinstance(obj, dict):
        sys.stderr.write("skipping non-object transcript line\n")
        continue
    obj["_mngr_conv_id"] = conv_id
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
' >> "$OUTPUT_FILE"

    _OFFSET_BY_ID[$conv_id]=$file_lines
    _save_offset "$conv_id" "$file_lines"

    log_debug "Emitted $new_count line(s) from conversation $conv_id (offset $offset -> $file_lines)"
}

# Trust the stored per-conversation offset.
#
# Antigravity's JSONL transcript only carries `step_index`, which is unique
# *within* a conversation but not across them, so we cannot use the global
# id-set reconciliation that mngr_claude relies on. If a crash interrupted
# the script between an emit and the matching `_save_offset`, restart will
# re-emit the duplicate lines; common_transcript.sh dedupes by event_id
# downstream so the user-visible transcript stays clean.
_record_conversation_offset() {
    local conv_id="$1"
    local log_prefix="$2"
    local transcript_file
    transcript_file=$(_transcript_path "$conv_id")
    if [ ! -f "$transcript_file" ]; then
        _OFFSET_BY_ID[$conv_id]=0
        return
    fi
    local stored
    stored=$(_load_stored_offset "$conv_id")
    # Defensive reset: if the on-disk transcript got shorter than the
    # stored offset (e.g. agy rewrote the file), start from 0 rather than
    # silently skipping the rest.
    local file_lines
    file_lines=$(_line_count "$transcript_file")
    if [ "$file_lines" -lt "$stored" ]; then
        log_warn "$log_prefix $conv_id: stored offset $stored > file lines $file_lines; resetting to 0"
        stored=0
        _save_offset "$conv_id" 0
    fi
    _OFFSET_BY_ID[$conv_id]=$stored
}

_initialize() {
    local conv_id
    while IFS= read -r conv_id; do
        [ -n "$conv_id" ] || continue
        _record_conversation_offset "$conv_id" "Loaded offset for"
    done < <(_find_conversation_ids)

    log_info "Tracked ${#_OFFSET_BY_ID[@]} conversation(s) at startup"
}

_run_one_cycle() {
    local current_ids=()
    local conv_id
    while IFS= read -r conv_id; do
        [ -n "$conv_id" ] || continue
        current_ids+=("$conv_id")
    done < <(_find_conversation_ids)

    for conv_id in "${current_ids[@]}"; do
        if [ -z "${_OFFSET_BY_ID[$conv_id]+exists}" ]; then
            _record_conversation_offset "$conv_id" "Picked up new conversation"
        fi
        _emit_new_lines "$conv_id"
    done
}

main() {
    local is_single_pass=false
    if [ "${1:-}" = "--single-pass" ]; then
        is_single_pass=true
    fi

    log_info "Stream transcript started"
    log_info "  Conversation IDs file: $CONVERSATION_IDS_FILE"
    log_info "  App data dir: $APP_DATA_DIR"
    log_info "  Output: $OUTPUT_FILE"
    log_info "  Poll interval: ${POLL_INTERVAL}s"

    _initialize

    if [ "$is_single_pass" = true ]; then
        _run_one_cycle
        return
    fi

    log_info "Entering main loop"

    while true; do
        _run_one_cycle
        sleep "$POLL_INTERVAL"
    done
}

main "${1:-}"
