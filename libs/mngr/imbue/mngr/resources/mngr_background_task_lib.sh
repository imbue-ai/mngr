#!/usr/bin/env bash
# Writes, clears and checks background task records, one file per task at $MNGR_AGENT_STATE_DIR/background_tasks/<name>.

# Reduce a record name to the characters [A-Za-z0-9_.-].
_mngr_background_task_file_name() {
    printf '%s' "$1" | tr -c 'A-Za-z0-9_.-' '_'
}

# Encode text as a record value, e.g. `cloud session` as `cloud%20session`; text that needs od prints nothing without it.
_mngr_background_task_encode() {
    local unreserved='ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~-'
    local LC_ALL=C
    case "$1" in
        *[!$unreserved]*) ;;
        *) printf '%s' "$1"; return 0 ;;
    esac
    command -v od >/dev/null 2>&1 || return 0
    printf '%s' "$1" | od -An -v -tx1 | awk -v unreserved="$unreserved" '
        BEGIN {
            for (code = 33; code < 127; code++) {
                char = sprintf("%c", code)
                if (index(unreserved, char) > 0) kept[sprintf("%02x", code)] = char
            }
        }
        { for (i = 1; i <= NF; i++) printf "%s", (($i in kept) ? kept[$i] : "%" toupper($i)) }
    '
}

# Echo the agent's record directory, or nothing when MNGR_AGENT_STATE_DIR is unset.
mngr_background_task_dir() {
    [ -n "${MNGR_AGENT_STATE_DIR:-}" ] || return 0
    printf '%s/background_tasks' "$MNGR_AGENT_STATE_DIR"
}

# mngr_background_task_write <name> [kind] [native_type]: write one record, e.g. `seen:1792575895 kind:SHELL native_type:shell`.
mngr_background_task_write() {
    local name="$1" kind="${2:-}" native_type="${3:-}"
    local record_dir file_name line staging_path attempt
    [ -n "$name" ] || return 0
    record_dir=$(mngr_background_task_dir)
    [ -n "$record_dir" ] || return 0
    file_name=$(_mngr_background_task_file_name "$name")
    case "$file_name" in
        '' | .*) return 0 ;;
    esac
    mkdir -p "$record_dir" 2>/dev/null || return 0
    line="seen:$(date +%s)"
    kind=$(_mngr_background_task_encode "$kind") || kind=""
    [ -z "$kind" ] || line="$line kind:$kind"
    native_type=$(_mngr_background_task_encode "$native_type") || native_type=""
    [ -z "$native_type" ] || line="$line native_type:$native_type"
    # When two writers race on one name, one write may be dropped; the record is always one whole line and no staging file is left.
    attempt=0
    while :; do
        staging_path="$record_dir/.$file_name.$$.$attempt.tmp"
        if (set -C; : > "$staging_path") 2>/dev/null; then
            break
        fi
        [ -e "$staging_path" ] || return 0
        attempt=$((attempt + 1))
    done
    if printf '%s\n' "$line" > "$staging_path" 2>/dev/null && mv -f "$staging_path" "$record_dir/$file_name" 2>/dev/null; then
        return 0
    fi
    rm -f "$staging_path" 2>/dev/null
    return 0
}

# Succeed when a record was seen no later than now and within the last HOOK_BACKGROUND_TASK_KEEPALIVE_SECONDS seconds.
mngr_background_task_any_recent() {
    local record_dir now window
    record_dir=$(mngr_background_task_dir)
    [ -n "$record_dir" ] || return 1
    [ -d "$record_dir" ] || return 1
    # Anything but a positive decimal count of seconds is the default window.
    window="${HOOK_BACKGROUND_TASK_KEEPALIVE_SECONDS:-}"
    case "$window" in
        '' | *[!0-9]* | 0*) window=86400 ;;
    esac
    now=$(date +%s)
    for mngr_task_record in "$record_dir"/*; do
        [ -f "$mngr_task_record" ] || continue
        # The verdict travels in a variable because awk's `exit` runs END, whose own exit status would replace it.
        LC_ALL=C awk -F '[ ]' -v now="$now" -v window="$window" '
            NR == 1 {
                sub(/\r+$/, "")
                for (i = 1; i <= NF; i++)
                    if ($i ~ /^seen:/) {
                        seen = substr($i, 6)
                        if (seen ~ /^[0-9]+$/ && length(seen) <= 12 && seen + 0 <= now + 0 && seen + window > now) recent = 1
                        break
                    }
                exit
            }
            END { exit !recent }
        ' "$mngr_task_record" 2>/dev/null && return 0
    done
    return 1
}

# mngr_background_task_clear <prefix>...: remove the records whose names start with any of the prefixes.
mngr_background_task_clear() {
    local record_dir prefix
    record_dir=$(mngr_background_task_dir)
    [ -n "$record_dir" ] || return 0
    for prefix in "$@"; do
        [ -n "$prefix" ] || continue
        rm -f "$record_dir/$prefix"* 2>/dev/null
    done
    return 0
}
