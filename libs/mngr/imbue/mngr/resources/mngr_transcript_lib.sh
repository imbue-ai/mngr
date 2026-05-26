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
#       appearing session file needs reconciliation. Iterates line-by-line via
#       mngr_transcript_extract_field so the extraction is consistent with the
#       per-line lookup in mngr_transcript_reconcile_offset.
#
#   - mngr_transcript_reconcile_offset SESSION_FILE FIELD
#       Echo the largest line offset N such that lines 1..N of SESSION_FILE
#       have FIELD values already present in _MNGR_TRANSCRIPT_ID_SET. Used to
#       recover after a crash that may have dropped the stored offset. Echoes
#       0 if no match is found or the file is empty.
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
    local pattern="\"${field}\"[[:space:]]*:[[:space:]]*\"([^\"]*)\""
    if [[ "$line" =~ $pattern ]]; then
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
    local line value
    while IFS= read -r line; do
        value=$(mngr_transcript_extract_field "$field" "$line")
        if [ -n "$value" ]; then
            _MNGR_TRANSCRIPT_ID_SET["$value"]=1
        fi
    done < "$output_file"
}

# Reverse-scan SESSION_FILE to find the last emitted line.
#
# Walks the file backwards (via `tac`) and returns the 1-indexed line number
# whose FIELD value is already in _MNGR_TRANSCRIPT_ID_SET. Returns 0 if no
# already-emitted line is found.
mngr_transcript_reconcile_offset() {
    local session_file="$1"
    local field="$2"

    if [ ! -s "$session_file" ] || [ ${#_MNGR_TRANSCRIPT_ID_SET[@]} -eq 0 ]; then
        echo 0
        return 0
    fi

    local file_lines
    file_lines=$(wc -l < "$session_file")
    file_lines=${file_lines// /}

    local reverse_idx=0
    local line value found
    while IFS= read -r line; do
        reverse_idx=$((reverse_idx + 1))
        value=$(mngr_transcript_extract_field "$field" "$line")
        if [ -n "$value" ] && [ "${_MNGR_TRANSCRIPT_ID_SET[$value]+exists}" ]; then
            found=$((file_lines - reverse_idx + 1))
            echo "$found"
            return 0
        fi
    done < <(tac "$session_file")

    echo 0
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
