#!/usr/bin/env bash
# mngr_transcript_lib.sh -- Shared primitives for raw-transcript streamers.
#
# Sourced by per-agent stream_transcript.sh scripts (claude, ...) to
# share the parts of raw-transcript capture that are structurally identical
# regardless of the agent's native session schema:
#
#   - mngr_transcript_extract_field FIELD LINE
#       Extract the first top-level "<FIELD>": "<value>" string from one JSONL
#       line. Used to read uuid / id / similar correlation fields without
#       requiring jq. Echoes the bare value (no quotes) on stdout, empty if no
#       match. Always returns 0 (safe under `set -e` command substitution).
#
#   - mngr_transcript_build_id_set OUTPUT_FILE FIELD
#       Populate the associative array _MNGR_TRANSCRIPT_ID_SET with every value
#       of FIELD found in OUTPUT_FILE. Used at startup and when a late-
#       appearing session file needs reconciliation.
#
#   - mngr_transcript_reconcile_offset SESSION_FILE FIELD
#       Echo the largest line offset N such that lines 1..N of SESSION_FILE
#       have FIELD values already present in _MNGR_TRANSCRIPT_ID_SET. Used to
#       recover after a crash that may have dropped the stored offset. Echoes
#       0 if no match is found or the file is empty. Scans forward rather than
#       reversing the file, so it needs no `tac` (GNU coreutils, absent on
#       macOS).
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

# Set _MNGR_TRANSCRIPT_FIELD_PATTERN to the ERE matching a top-level
# "<FIELD>": "<value>" pair, capturing the value.
#
# The per-line loops below match with this pattern directly rather than calling
# mngr_transcript_extract_field, because a command substitution forks a subshell
# per line -- prohibitive on a session file with tens of thousands of lines.
_mngr_transcript_set_field_pattern() {
    _MNGR_TRANSCRIPT_FIELD_PATTERN="\"$1\"[[:space:]]*:[[:space:]]*\"([^\"]*)\""
}

# Extract a top-level JSON string field from a single line.
#
# Limitations: matches the *first* "<FIELD>": "<value>" in the line. Nested
# occurrences inside arrays / sub-objects with the same key are also matched
# if they precede the top-level one, but in practice agent-emitted JSONL
# events keep correlation fields (uuid, id) at the top so the first match
# is the right one.
mngr_transcript_extract_field() {
    local field="$1"
    local line="$2"
    _mngr_transcript_set_field_pattern "$field"
    if [[ "$line" =~ $_MNGR_TRANSCRIPT_FIELD_PATTERN ]]; then
        printf '%s\n' "${BASH_REMATCH[1]}"
    fi
    return 0
}

# Build _MNGR_TRANSCRIPT_ID_SET from every FIELD value in OUTPUT_FILE.
mngr_transcript_build_id_set() {
    local output_file="$1"
    local field="$2"
    _MNGR_TRANSCRIPT_ID_SET=()
    if [ ! -s "$output_file" ]; then
        return 0
    fi
    _mngr_transcript_set_field_pattern "$field"
    local line
    while IFS= read -r line; do
        if [[ "$line" =~ $_MNGR_TRANSCRIPT_FIELD_PATTERN ]] && [ -n "${BASH_REMATCH[1]}" ]; then
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

    _mngr_transcript_set_field_pattern "$field"
    local idx=0
    local found=0
    local line value
    while IFS= read -r line; do
        idx=$((idx + 1))
        if [[ "$line" =~ $_MNGR_TRANSCRIPT_FIELD_PATTERN ]]; then
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
