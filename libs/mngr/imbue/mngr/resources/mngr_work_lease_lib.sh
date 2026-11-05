#!/usr/bin/env bash
# mngr_work_lease_lib.sh -- Declare the outstanding work that will wake an agent.
#
# Sourced by the turn-end hooks of agent plugins that can name what their CLI
# left running (claude's wait_for_stop_hook.sh). A lease tells mngr that the
# agent's turn has ended but something is coming back to wake it, so the agent
# reads RUNNING_BACKGROUND rather than WAITING.
#
# One file per unit of work, at $MNGR_AGENT_STATE_DIR/leases/<name>. Its first
# line is whitespace-separated <key>:<value> tokens:
#
#   until:<n>    the lease holds only until that epoch second
#
# A lease must carry until:, else it could never expire and would pin the agent
# forever; mngr drops one that does not. Readers ignore tokens they do not
# know, so the grammar can grow. The Python side of the same protocol is
# imbue.mngr.hosts.work_leases.
#
#   - mngr_work_lease_dir
#       Echo the agent's lease directory, or nothing when
#       MNGR_AGENT_STATE_DIR is unset -- the signal that there is no agent to
#       lease against.
#
#   - mngr_work_lease_declare <name> <ttl_seconds>
#       Write one lease that expires ttl_seconds from now. The name is reduced
#       to a safe file name, so a caller may pass an id straight from its CLI.
#       Never fails its caller: a lease that cannot be written is one the agent
#       does without, and a hook under `set -e` must not die on it.
#
#   - mngr_work_lease_clear <prefix> [prefix...]
#       Remove the leases whose names start with any of these prefixes. A
#       writer passes only its own prefixes: the directory belongs to the agent
#       and anything may lease there, and someone else's lease expires on its
#       own bound.
#
# Nothing runs at source time and the env is only read inside the functions, so
# sourcing this lib can never fail.

# Reduce a lease name to characters that are safe both in a file name and in the
# tab-delimited stream the batched reader collects leases over, so an id from an
# agent CLI can be passed through unexamined.
_mngr_work_lease_file_name() {
    printf '%s' "$1" | tr -c 'A-Za-z0-9_.-' '_'
}

mngr_work_lease_dir() {
    [ -n "${MNGR_AGENT_STATE_DIR:-}" ] || return 0
    printf '%s/leases' "$MNGR_AGENT_STATE_DIR"
}

mngr_work_lease_declare() {
    local name="$1" ttl_seconds="$2"
    local lease_dir file_name now line
    [ -n "$name" ] || return 0
    lease_dir=$(mngr_work_lease_dir)
    [ -n "$lease_dir" ] || return 0
    file_name=$(_mngr_work_lease_file_name "$name")
    [ -n "$file_name" ] || return 0
    mkdir -p "$lease_dir" 2>/dev/null || return 0
    now=$(date +%s)
    line="until:$((now + ttl_seconds))"
    # Renamed into place, so a reader never sees a half-written line: a lease
    # missing its bound is dropped, which would read as the work having ended.
    # The staging name starts with a dot because a reader collects leases with
    # `leases/*`, which a dotfile does not match -- a visible .tmp would read
    # as a second lease of its own for as long as it existed.
    if printf '%s\n' "$line" > "$lease_dir/.$file_name.tmp" 2>/dev/null; then
        mv -f "$lease_dir/.$file_name.tmp" "$lease_dir/$file_name" 2>/dev/null || true
    fi
    return 0
}

mngr_work_lease_clear() {
    local lease_dir prefix
    lease_dir=$(mngr_work_lease_dir)
    [ -n "$lease_dir" ] || return 0
    for prefix in "$@"; do
        [ -n "$prefix" ] || continue
        rm -f "$lease_dir/$prefix"* 2>/dev/null
    done
    return 0
}
