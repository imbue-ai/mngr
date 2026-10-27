#!/usr/bin/env bash
# Robust transcript streaming for Claude agents.
#
# Watches ALL Claude session JSONL files and appends new lines to
# logs/claude_transcript/events.jsonl. Designed to handle:
#   - Any session file being written to at any time (not just the "current" one)
#   - Restarts (reconciles per-session offsets against the output file)
#   - Late-appearing session files (re-checks each poll cycle, no timeouts)
#   - Sessions added out of order or with gaps
#   - Two instances at once: the poll daemon and a `--single-pass` flush
#
# Per-session line offsets are stored in
# <agent-state-dir>/plugin/claude/.transcript_offsets/<session_id> so the
# script can resume efficiently. On startup, stored offsets are verified
# against the output file using UUID-based lookups -- if the stored offset
# is wrong (e.g. crash between emit and offset save), the script works
# backwards through the session file to find the last line that actually
# made it into the output.
#
# The output file and an offset are only consistent with each other while the
# shared convert lock is held, so both the emit (read offset, append, write
# offset) and the reconciliation that reads the pair run under it.
#
# Each cycle also clears the 'active' marker a terminal interrupt strands
# (see "Interrupt-stranded marker heal" below).
#
# Usage: stream_transcript.sh
#
# Requires environment variables:
#   MNGR_AGENT_STATE_DIR  - the agent's state directory (contains commands/)
#
# Optional environment variables:
#   MNGR_HOST_DIR         - the host directory the heal emits its activity
#                           event under; the heal is skipped when unset

set -euo pipefail

SESSION_HISTORY="${MNGR_AGENT_STATE_DIR:?MNGR_AGENT_STATE_DIR must be set}/claude_session_id_history"
OUTPUT_FILE="$MNGR_AGENT_STATE_DIR/logs/claude_transcript/events.jsonl"
OFFSET_DIR="$MNGR_AGENT_STATE_DIR/plugin/claude/.transcript_offsets"
POLL_INTERVAL=1

mkdir -p "$(dirname "$OUTPUT_FILE")" "$OFFSET_DIR"
touch "$OUTPUT_FILE"

# Configure and source the shared logging library
_MNGR_LOG_TYPE="stream_transcript"
_MNGR_LOG_SOURCE="logs/stream_transcript"
_MNGR_LOG_FILE="$MNGR_AGENT_STATE_DIR/events/logs/stream_transcript/events.jsonl"
# shellcheck source=mngr_log.sh
source "$MNGR_AGENT_STATE_DIR/commands/mngr_log.sh"
# shellcheck source=mngr_transcript_lib.sh
source "$MNGR_AGENT_STATE_DIR/commands/mngr_transcript_lib.sh"
# shellcheck source=mngr_common_transcript_lib.sh
source "$MNGR_AGENT_STATE_DIR/commands/mngr_common_transcript_lib.sh"

# Per-session state (bash 4+ associative arrays)
# Note: explicit =() is required for set -u compatibility (empty associative
# arrays are "unbound" under set -u without it).
declare -A _FILE_BY_SID=()    # session_id -> resolved file path ("" if not yet found)
declare -A _OFFSET_BY_SID=()  # session_id -> lines already emitted from this session
_KNOWN_HISTORY_LINES=0        # lines of the history file already processed

# Line counts captured once at the top of each poll cycle by
# _refresh_line_counts. Counting every tracked file in a single wc invocation
# holds a poll cycle to one fork regardless of how many sessions the agent has
# resumed through; counting them one file at a time made an idle agent's cost
# grow with the length of its session history and never shrink.
declare -A _LINES_BY_SID=()   # session_id -> line count observed this cycle
_HISTORY_LINES_NOW=0          # line count of the history file observed this cycle

# UUID lookup set, populated by mngr_transcript_build_id_set during
# reconciliation and cleared once reconciliation finishes.
declare -A _MNGR_TRANSCRIPT_ID_SET=()

# Helpers

# Find the JSONL file for a session ID, setting _FOUND_SESSION_JSONL to its path
# (or "" when it does not exist yet).
# Claude stores session files at $CLAUDE_CONFIG_DIR/projects/<hash>/<session_id>.jsonl
# Falls back to ~/.claude/projects/ when CLAUDE_CONFIG_DIR is not set.
# A chat that has never been messaged has no file, so this runs every poll cycle
# for as long as the chat stays idle; a glob keeps that from spawning a process.
_find_session_jsonl() {
    local match
    _FOUND_SESSION_JSONL=""
    for match in "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/"*/"${1}.jsonl"; do
        if [ -f "$match" ]; then
            _FOUND_SESSION_JSONL="$match"
            return 0
        fi
    done
}

_line_count() {
    if [ -f "$1" ]; then
        wc -l < "$1"
    else
        echo 0
    fi
}

# Count the history file and every resolved session file in one wc invocation,
# caching the results for the rest of the cycle. Any file wc could not report on
# is simply left out of the cache, so its caller falls back to _line_count.
_refresh_line_counts() {
    _LINES_BY_SID=()
    _HISTORY_LINES_NOW=0

    local sid path count
    local -a paths=()
    local -A sid_by_path=()

    if [ -f "$SESSION_HISTORY" ]; then
        paths+=("$SESSION_HISTORY")
    fi
    for sid in "${!_FILE_BY_SID[@]}"; do
        path="${_FILE_BY_SID[$sid]}"
        if [ -n "$path" ] && [ -f "$path" ]; then
            paths+=("$path")
            sid_by_path["$path"]="$sid"
        fi
    done

    [ "${#paths[@]}" -gt 0 ] || return 0

    # `wc -l` prints "<count> <path>" per file, plus a trailing "<count> total"
    # line when given more than one. The path is the last field, so a path
    # containing spaces survives the read intact, and the "total" line matches
    # no tracked path and is therefore ignored.
    local counts_output
    counts_output=$(wc -l "${paths[@]}" 2>/dev/null || true)
    while read -r count path; do
        if [ "$path" = "$SESSION_HISTORY" ]; then
            _HISTORY_LINES_NOW=$count
        else
            sid="${sid_by_path[$path]:-}"
            if [ -n "$sid" ]; then
                _LINES_BY_SID[$sid]=$count
            fi
        fi
    done <<< "$counts_output"
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

# Try to resolve the file path for a session (caches result once found).
_try_resolve_file() {
    local sid="$1"
    if [ -n "${_FILE_BY_SID[$sid]:-}" ]; then
        return 0
    fi
    _find_session_jsonl "$sid"
    if [ -n "$_FOUND_SESSION_JSONL" ]; then
        _FILE_BY_SID[$sid]="$_FOUND_SESSION_JSONL"
        log_debug "Resolved session $sid -> $_FOUND_SESSION_JSONL"
        return 0
    fi
    return 1
}

# Reconciliation (restart recovery)
#
# Field extraction, id-set construction, and reverse-scan reconciliation
# come from mngr_transcript_lib.sh. The shared helpers operate on the
# global _MNGR_TRANSCRIPT_ID_SET, populated by mngr_transcript_build_id_set.

# Reconcile the stored offsets of the given sessions against the output file.
#
# Runs under the convert lock: reading the output file and the offsets unlocked
# can pair a pre-append output with a post-append offset, and reconciling that
# pair rewinds the offset onto lines the other instance already streamed.
# Unresolved sessions are skipped, so their stored offset stands until the cycle
# that resolves them.
_reconcile_offsets_locked() {
    if ! mngr_common_transcript_acquire_lock; then
        log_warn "Could not acquire transcript lock to reconcile offsets; keeping stored offsets"
        return
    fi

    mngr_transcript_build_id_set "$OUTPUT_FILE" "uuid"

    local sid stored reconciled
    for sid in "$@"; do
        if [ -z "${_FILE_BY_SID[$sid]:-}" ]; then
            continue
        fi
        stored="${_OFFSET_BY_SID[$sid]}"
        reconciled=$(mngr_transcript_reconcile_offset "${_FILE_BY_SID[$sid]}" "uuid")
        _OFFSET_BY_SID[$sid]=$reconciled
        if [ "$reconciled" != "$stored" ]; then
            log_info "Reconciled offset for $sid: $stored -> $reconciled"
            _save_offset "$sid" "$reconciled"
        fi
    done

    # Free the UUID set -- not needed until next reconciliation
    _MNGR_TRANSCRIPT_ID_SET=()

    mngr_common_transcript_release_lock
}

# Session processing

# Check a session file for new lines and append them to the output.
# The shared mngr_transcript_emit_lines_range uses sed with a bounded range
# to avoid a TOCTOU race: the line count is captured at time T1 (by this
# cycle's _refresh_line_counts, or directly when running outside a cycle), and
# sed reads exactly lines offset+1..file_lines. If Claude appends more
# lines between T1 and the sed read, those extra lines are NOT emitted
# (they'll be picked up on the next poll cycle), and the saved offset
# accurately reflects what was actually emitted.
_emit_new_lines() {
    local sid="$1"
    local session_file="${_FILE_BY_SID[$sid]}"

    # Use the count captured for this cycle when there is one; fall back to a
    # direct count for callers outside a poll cycle (the startup backlog pass)
    # and for sessions discovered after the cycle's counts were taken.
    local file_lines
    if [ -n "${_LINES_BY_SID[$sid]+exists}" ]; then
        file_lines="${_LINES_BY_SID[$sid]}"
    else
        file_lines=$(_line_count "$session_file")
    fi

    if [ "$file_lines" -le "${_OFFSET_BY_SID[$sid]}" ]; then
        return
    fi

    if ! mngr_common_transcript_acquire_lock; then
        log_warn "Could not acquire transcript lock for session $sid; deferring emit"
        return
    fi

    # Re-read under the lock: this script's other instance may have advanced the
    # offset since this process last looked, and the in-memory value would
    # re-emit every line that instance streamed.
    local offset
    offset=$(_load_stored_offset "$sid")
    _OFFSET_BY_SID[$sid]=$offset

    if [ "$file_lines" -le "$offset" ]; then
        mngr_common_transcript_release_lock
        return
    fi

    local start=$((offset + 1))
    mngr_transcript_emit_lines_range "$session_file" "$start" "$file_lines" "$OUTPUT_FILE"

    local new_count=$((file_lines - offset))
    _OFFSET_BY_SID[$sid]=$file_lines
    _save_offset "$sid" "$file_lines"

    mngr_common_transcript_release_lock

    log_debug "Emitted $new_count line(s) from session $sid (offset $offset -> $file_lines)"
}

# Check the history file for new session IDs.
_check_for_new_sessions() {
    [ -f "$SESSION_HISTORY" ] || return 0

    # Counted by _refresh_line_counts at the top of this cycle.
    local current_lines="$_HISTORY_LINES_NOW"
    [ "$current_lines" -le "$_KNOWN_HISTORY_LINES" ] && return 0

    local start=$((_KNOWN_HISTORY_LINES + 1))
    while read -r sid _rest; do
        if [ -n "$sid" ] && [ -z "${_FILE_BY_SID[$sid]+exists}" ]; then
            _FILE_BY_SID[$sid]=""
            _OFFSET_BY_SID[$sid]=0
            log_info "Discovered new session: $sid"
        fi
    done < <(tail -n "+${start}" "$SESSION_HISTORY")

    _KNOWN_HISTORY_LINES=$current_lines
}

# Initialization

_initialize() {
    # Load all known sessions from history
    if [ -f "$SESSION_HISTORY" ]; then
        while read -r sid _rest; do
            if [ -n "$sid" ]; then
                _FILE_BY_SID[$sid]=""
                _OFFSET_BY_SID[$sid]=$(_load_stored_offset "$sid")
            fi
        done < "$SESSION_HISTORY"
        _KNOWN_HISTORY_LINES=$(_line_count "$SESSION_HISTORY")
    fi

    log_info "Loaded ${#_FILE_BY_SID[@]} session(s) from history"

    # Resolve files for all known sessions, then reconcile their offsets together
    local sid
    for sid in "${!_FILE_BY_SID[@]}"; do
        _try_resolve_file "$sid" || true
    done

    if [ "${#_FILE_BY_SID[@]}" -gt 0 ]; then
        _reconcile_offsets_locked "${!_FILE_BY_SID[@]}"
    fi
}

# Interrupt-stranded marker heal
#
# Claude fires NO hook when the user interrupts a turn in the terminal (Esc):
# not at interrupt time, and not the idle_prompt Notification afterwards. The
# 'active' marker the UserPromptSubmit hook created is therefore stranded: the
# lifecycle reports RUNNING and the activity updater keeps refreshing
# activity/agent while the terminal sits idle at its prompt. The interrupt
# sentinel record claude appends to its session JSONL is the only on-disk
# evidence that the turn died, and this streamer already tails those files, so
# it clears the markers the same way the idle hooks do (rm both markers + one
# activity event). Guarded by the marker's mtime so a turn submitted after the
# interrupt is never un-marked, and by the prompt queue so a turn the interrupt
# itself started is not either (see _is_interrupt_flushing_queued_prompt). It
# runs every cycle, the Stop hook's single-pass flush included; a turn that
# reached Stop did not end on an interrupt, so that pass never heals.

# Shared opening of both sentinel shapes ("[Request interrupted by user]" and
# the mid-tool "[Request interrupted by user for tool use]" variant).
_INTERRUPT_SENTINEL_PREFIX='[Request interrupted by user'

# How much older than the sentinel the marker's mtime must be before the heal
# clears it. Errs toward NOT clearing: a wrongly skipped heal leaves the stuck
# RUNNING state until the next turn or restart, while a wrong clear would hide
# a live turn.
_INTERRUPT_HEAL_GUARD_SECONDS=1

# Print the mtime of $1 as epoch seconds (GNU stat, then BSD stat).
_epoch_mtime() {
    stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null
}

# Print the epoch timestamp of a raw session line iff it is an interrupt
# sentinel record: a `user` record whose TEXT blocks -- never a tool_result
# quoting the sentinel, whose text is not extracted -- start with the sentinel
# prefix. Prints nothing for anything else. jq's fromdateiso8601 rejects
# fractional seconds, so they are stripped before parsing.
_interrupt_sentinel_epoch() {
    printf '%s' "$1" | jq -r --arg prefix "$_INTERRUPT_SENTINEL_PREFIX" '
        select(type == "object" and .type == "user")
        | ((.message? | objects | .content?) // "") as $content
        | (if ($content | type) == "string" then $content
           elif ($content | type) == "array" then
               ([$content[] | objects | select(.type == "text") | .text? | strings | select(. != "")] | join("\n"))
           else "" end) as $text
        | select($text | sub("^\\s+"; "") | startswith($prefix))
        | .timestamp | strings | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601
    ' 2>/dev/null
}

# How many trailing session lines the queued-prompt check reads. The dequeue it
# looks for is written within a few records of the sentinel.
_INTERRUPT_QUEUE_WINDOW_LINES=500

# Succeed iff the interrupt is flushing a queued prompt into a new turn. An
# interrupt with a prompt queued dequeues it around the sentinel and starts its
# turn, and no hook fires for that turn: the marker it runs under dates from the
# enqueue (UserPromptSubmit fires then), so it is older than the sentinel, and
# the sentinel stays the session's tail until the new turn's first reply. The
# dequeue is the tell: one after the interrupted turn's last assistant record is
# the next turn's. A dequeue that started the interrupted turn precedes that
# turn's replies. A session that cannot be read also succeeds, so the heal it
# guards is skipped rather than let through.
_is_interrupt_flushing_queued_prompt() {
    local verdict
    verdict=$(tail -n "$_INTERRUPT_QUEUE_WINDOW_LINES" "$1" 2>/dev/null | jq -R -s -r '
        [split("\n")[] | fromjson? | objects] as $records
        | ([$records[] | select(.type == "queue-operation" and .operation == "dequeue") | .timestamp // empty] | max) as $dequeued_at
        | ([$records[] | select(.type == "assistant") | .timestamp // empty] | max) as $replied_at
        | if $dequeued_at != null and ($replied_at == null or $dequeued_at > $replied_at) then "flushing" else "" end
    ' 2>/dev/null) || return 0
    [ "$verdict" = "flushing" ]
}

# Clear the 'active' (and 'permissions_waiting') marker stranded by a terminal
# interrupt: only when the CURRENT session's last record is an interrupt
# sentinel that postdates the marker's mtime by more than the guard band.
# Best-effort and positive-evidence-only -- anything unreadable or unparseable
# (including a partially-written last line, complete on the next cycle) skips
# the heal rather than failing the streamer.
_heal_interrupt_stranded_marker() {
    local marker="$MNGR_AGENT_STATE_DIR/active"
    if [ -z "${MNGR_HOST_DIR:-}" ] || [ ! -f "$marker" ]; then
        return 0
    fi

    # Only the current session's tail can prove the marker's turn is dead. The
    # sid must already be tracked (the same hook maintains the sid marker and
    # the history this script tracks), so an unknown sid is skipped rather than
    # force-resolved -- _try_resolve_file would register it without an offset.
    local sid
    sid=$(cat "$MNGR_AGENT_STATE_DIR/claude_session_id" 2>/dev/null) || return 0
    # An empty key is a fatal "bad array subscript" under set -u.
    if [ -z "$sid" ]; then
        return 0
    fi
    local session_file="${_FILE_BY_SID[$sid]:-}"
    if [ -z "$session_file" ] || [ ! -f "$session_file" ]; then
        return 0
    fi

    # Substring pre-filter before the jq parse: almost no tail line contains
    # the sentinel opening, and the jq fork is not free at a 1s poll interval.
    local last_line
    last_line=$(tail -n 1 "$session_file" 2>/dev/null) || return 0
    case "$last_line" in
        *"$_INTERRUPT_SENTINEL_PREFIX"*) ;;
        *) return 0 ;;
    esac

    local sentinel_epoch
    sentinel_epoch=$(_interrupt_sentinel_epoch "$last_line") || return 0
    case "$sentinel_epoch" in
        '' | *[!0-9]*) return 0 ;;
    esac

    if _is_interrupt_flushing_queued_prompt "$session_file"; then
        return 0
    fi

    # Read last, right before the rm, so a turn submitted while the checks
    # above ran is caught by the guard.
    local marker_mtime
    marker_mtime=$(_epoch_mtime "$marker") || return 0
    case "$marker_mtime" in
        '' | *[!0-9]*) return 0 ;;
    esac

    # A marker touched at/after the interrupt belongs to a turn started after
    # it; the band also absorbs marker-mtime vs transcript-timestamp skew.
    if [ "$marker_mtime" -ge $((sentinel_epoch - _INTERRUPT_HEAL_GUARD_SECONDS)) ]; then
        return 0
    fi

    # The same clear, idle stamp and activity event the idle hooks run (see
    # _CLEAR_ACTIVE_MARKERS_AND_EMIT_ACTIVITY_EVENT in claude_config.py and
    # mark_inactive in wait_for_stop_hook.sh); the event makes `mngr observe`
    # re-probe promptly. The event write is best-effort: the markers are
    # already cleared, so a failure only delays observers until their next poll.
    rm -f "$marker" "$MNGR_AGENT_STATE_DIR/permissions_waiting"
    date -u +"%Y-%m-%dT%H:%M:%S.000000000Z" > "$MNGR_AGENT_STATE_DIR/idle_since"
    {
        mkdir -p "$MNGR_HOST_DIR/events/mngr/activity" &&
            echo '{"source": "mngr/activity", "type": "activity", "event_id": "evt-'"$(head -c 16 /dev/urandom | xxd -p)"'", "timestamp": "'"$(date -u +"%Y-%m-%dT%H:%M:%S.000000000Z")"'", "reason": "interrupt_heal"}' \
                >> "$MNGR_HOST_DIR/events/mngr/activity/events.jsonl"
    } || log_warn "Failed to emit activity event after healing interrupt-stranded marker"
    log_info "Cleared active marker stranded by a terminal interrupt (session $sid)"
}

# Poll cycle (shared by main loop and single-pass mode)

_run_one_cycle() {
    # One wc pass covers the history file and every resolved session file, so
    # the rest of this cycle needs no further counting forks.
    _refresh_line_counts
    _check_for_new_sessions

    for sid in "${!_FILE_BY_SID[@]}"; do
        # Try to resolve file if not yet found (re-checked every cycle)
        if [ -z "${_FILE_BY_SID[$sid]}" ]; then
            if ! _try_resolve_file "$sid"; then
                continue
            fi
            # File just appeared -- reconcile against the output file to
            # find the true offset (handles both fresh starts and restarts)
            _reconcile_offsets_locked "$sid"
        fi

        _emit_new_lines "$sid"
    done

    _heal_interrupt_stranded_marker
}

# Main

main() {
    local is_single_pass=false
    if [ "${1:-}" = "--single-pass" ]; then
        is_single_pass=true
    fi

    log_info "Stream transcript started"
    log_info "  Session history: $SESSION_HISTORY"
    log_info "  Output: $OUTPUT_FILE"
    log_info "  Poll interval: ${POLL_INTERVAL}s"

    _initialize

    # Emit any backlog in history-file order (for rough ordering on startup)
    if [ -f "$SESSION_HISTORY" ]; then
        while read -r sid _rest; do
            if [ -n "$sid" ] && [ -n "${_FILE_BY_SID[$sid]:-}" ]; then
                _emit_new_lines "$sid"
            fi
        done < "$SESSION_HISTORY"
    fi

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
