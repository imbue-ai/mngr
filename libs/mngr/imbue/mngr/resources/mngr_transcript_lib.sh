#!/usr/bin/env bash
# mngr_transcript_lib.sh -- Shared primitives for raw-transcript streamers.
#
# Sourced by per-agent stream_transcript.sh scripts (claude, ...) to
# share the parts of raw-transcript capture that are structurally identical
# regardless of the agent's native session schema:
#
#   - mngr_transcript_build_id_set OUTPUT_FILE FIELD
#       Populate the associative array _MNGR_TRANSCRIPT_ID_SET with every value
#       of FIELD found in OUTPUT_FILE. Used at startup and when a late-
#       appearing session file needs reconciliation. Correlation fields (uuid,
#       id) are matched with a bash regex, so no jq is required.
#
#   - mngr_transcript_reconcile_offset SESSION_FILE FIELD
#       Echo the highest 1-indexed line of SESSION_FILE whose FIELD value is
#       already present in _MNGR_TRANSCRIPT_ID_SET, or 0 if there is none. Used
#       to recover after a crash that may have dropped the stored offset. Scans
#       forward, so it needs no `tac` (GNU coreutils, absent on macOS).
#
#   - mngr_transcript_emit_lines_range SESSION_FILE START END OUTPUT_FILE
#       Append lines START..END (inclusive, 1-indexed) of SESSION_FILE to
#       OUTPUT_FILE using sed with a bounded range. The caller computes
#       end via wc -l before reading, so any lines appended to SESSION_FILE
#       between wc and sed are deferred to the next poll cycle (TOCTOU-safe).
#
#   - mngr_transcript_percent_encode_path PATH
#       Echo PATH with characters not safe in a single filename ('/', '%')
#       percent-encoded. Used by streamers whose offset keys are file paths;
#       streamers whose keys are uuid-shaped (claude) can skip it.
#
# All functions are pure / read-only except for population of
# _MNGR_TRANSCRIPT_ID_SET. Callers are responsible for declaring that array
# (so it survives function-local scope) and for clearing it when no longer
# needed.

# The ERE matching a top-level "<FIELD>": "<value>" pair, capturing the value.
# Matched inline in the loops below: a command substitution would fork a subshell
# per line, and a session file runs to tens of thousands of lines.
#
# Limitations: matches the *first* such pair in the line. Agent-emitted JSONL
# events keep correlation fields (uuid, id) at the top, so the first match is the
# right one.
_MNGR_TRANSCRIPT_FIELD_ERE='"%s"[[:space:]]*:[[:space:]]*"([^"]*)"'

# Build _MNGR_TRANSCRIPT_ID_SET from every FIELD value in OUTPUT_FILE.
mngr_transcript_build_id_set() {
    local output_file="$1"
    local field="$2"
    _MNGR_TRANSCRIPT_ID_SET=()
    if [ ! -s "$output_file" ]; then
        return 0
    fi
    local pattern
    printf -v pattern "$_MNGR_TRANSCRIPT_FIELD_ERE" "$field"
    local line
    while IFS= read -r line; do
        if [[ "$line" =~ $pattern ]] && [ -n "${BASH_REMATCH[1]}" ]; then
            _MNGR_TRANSCRIPT_ID_SET["${BASH_REMATCH[1]}"]=1
        fi
    done < "$output_file"
}

# Scan SESSION_FILE to find the last emitted line.
#
# Returns the highest 1-indexed line number whose FIELD value is already in
# _MNGR_TRANSCRIPT_ID_SET, or 0 if no already-emitted line is found.
mngr_transcript_reconcile_offset() {
    local session_file="$1"
    local field="$2"

    if [ ! -s "$session_file" ] || [ ${#_MNGR_TRANSCRIPT_ID_SET[@]} -eq 0 ]; then
        echo 0
        return 0
    fi

    local pattern
    printf -v pattern "$_MNGR_TRANSCRIPT_FIELD_ERE" "$field"
    local idx=0
    local found=0
    local line value
    while IFS= read -r line; do
        idx=$((idx + 1))
        if [[ "$line" =~ $pattern ]]; then
            value="${BASH_REMATCH[1]}"
            if [ -n "$value" ] && [ "${_MNGR_TRANSCRIPT_ID_SET[$value]+exists}" ]; then
                found=$idx
            fi
        fi
    done < "$session_file"

    echo "$found"
}

# Append lines START..END of SESSION_FILE to OUTPUT_FILE.
#
# Uses sed with a bounded range so any lines appended between the caller's
# wc -l and our read are not emitted (they will be picked up on the next
# poll cycle, and the saved offset accurately reflects what landed in the
# output file).
mngr_transcript_emit_lines_range() {
    local session_file="$1"
    local start="$2"
    local end="$3"
    local output_file="$4"
    sed -n "${start},${end}p" "$session_file" >> "$output_file"
}

# Percent-encode a path so it can be used as a single filename.
# Encodes '%' and '/' only; other characters pass through. Distinct paths
# always produce distinct outputs, so this is safe as a per-file key.
mngr_transcript_percent_encode_path() {
    local path="$1"
    local encoded="${path//%/%25}"
    encoded="${encoded//\//%2F}"
    printf '%s\n' "$encoded"
}
