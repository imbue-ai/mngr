#!/usr/bin/env bash
# Activity watcher script for mngr hosts.
# This script monitors activity files and calls shutdown.sh when the host becomes idle.
#
# Usage: activity_watcher.sh <host_data_dir>
#
# The script reads from <host_data_dir>/data.json:
#   - activity_sources: array of activity source names (e.g., ["BOOT", "USER", "AGENT"])
#   - idle_timeout_seconds: the idle timeout in seconds
#   - max_host_age: (optional) maximum host age in seconds from boot
#   - tmux_session_prefix: (optional) prefix for agent tmux sessions (e.g., "mngr-")
#
# Activity sources are converted to file patterns:
#   - Host-level sources (BOOT, USER, SSH): <host_data_dir>/activity/<source>
#   - Agent-level sources (CREATE, START, AGENT, PROCESS): <host_data_dir>/agents/*/activity/<source>
#
# When the maximum mtime of all matched files + idle_timeout < current_time,
# the script calls <host_data_dir>/commands/shutdown.sh.
#
# Additionally, if max_host_age exists in data.json, the script will trigger shutdown when:
#   current_time > boot_activity_file_mtime + max_host_age_seconds
# This ensures the host shuts down cleanly before external timeouts (e.g., Modal sandbox timeout).
#
# If tmux_session_prefix is set in data.json, the script also checks whether any
# tmux sessions with that prefix are running. If none are found, the host is shut
# down with stop_reason=STOPPED (rather than PAUSED) since all agents have exited.

set -euo pipefail

HOST_DATA_DIR="${1:-}"

if [ -z "$HOST_DATA_DIR" ]; then
    echo "Usage: activity_watcher.sh <host_data_dir>" >&2
    exit 1
fi

# Configure and source the shared logging library
_MNGR_LOG_TYPE="activity_watcher"
_MNGR_LOG_SOURCE="logs/activity_watcher"
_MNGR_LOG_FILE="$HOST_DATA_DIR/events/logs/activity_watcher/events.jsonl"
# shellcheck source=mngr_log.sh
source "$HOST_DATA_DIR/commands/mngr_log.sh"

# Write to both stdout and the JSONL log file
log() {
    echo "$*"
    log_info "$*"
}

DATA_JSON_PATH="$HOST_DATA_DIR/data.json"
HOST_LOCK_PATH="$HOST_DATA_DIR/host_lock"
BOOT_ACTIVITY_PATH="$HOST_DATA_DIR/activity/boot"
SHUTDOWN_SCRIPT="$HOST_DATA_DIR/commands/shutdown.sh"
# Check every 15 seconds to detect idle quickly while minimizing overhead
CHECK_INTERVAL=15

# Host-level activity sources (as opposed to agent-level)
HOST_LEVEL_SOURCES="boot user ssh"

# Get the mtime of a file as Unix timestamp, or empty string if file doesn't exist
get_mtime() {
    local file="$1"
    if [ -f "$file" ]; then
        # Try Linux stat first, then macOS stat
        stat -c %Y "$file" 2>/dev/null || stat -f %m "$file" 2>/dev/null || echo ""
    fi
}

# Check if a source is host-level (vs agent-level)
is_host_level_source() {
    local source="$1"
    local source_lower
    source_lower=$(echo "$source" | tr '[:upper:]' '[:lower:]')
    for hs in $HOST_LEVEL_SOURCES; do
        if [ "$source_lower" = "$hs" ]; then
            return 0
        fi
    done
    return 1
}

# Read activity_sources from data.json and return as space-separated lowercase list
get_activity_sources() {
    if [ ! -f "$DATA_JSON_PATH" ]; then
        echo ""
        return
    fi
    # Extract activity_sources array, convert to lowercase, output as space-separated
    jq -r '.activity_sources // [] | .[] | ascii_downcase' "$DATA_JSON_PATH" 2>/dev/null | tr '\n' ' '
}

# Read idle_timeout_seconds from data.json
get_idle_timeout_seconds() {
    if [ ! -f "$DATA_JSON_PATH" ]; then
        echo ""
        return
    fi
    jq -r '.idle_timeout_seconds // empty' "$DATA_JSON_PATH" 2>/dev/null
}

# Read max_host_age from data.json (optional field)
get_max_host_age() {
    if [ ! -f "$DATA_JSON_PATH" ]; then
        echo ""
        return
    fi
    jq -r '.max_host_age // empty' "$DATA_JSON_PATH" 2>/dev/null
}

# Read tmux_session_prefix from data.json (optional field)
get_tmux_session_prefix() {
    if [ ! -f "$DATA_JSON_PATH" ]; then
        echo ""
        return
    fi
    jq -r '.tmux_session_prefix // empty' "$DATA_JSON_PATH" 2>/dev/null
}

# Check if there are any running tmux sessions with the configured prefix.
# Returns 0 (true) if at least one session exists, 1 (false) if none exist.
# Also returns 0 (true / skip shutdown) if:
#   - No prefix is configured (can't check)
#   - No agent directories exist yet (host is still in initial setup)
#   - An agent directory was created recently (within grace period)
#
# Note: The host lock check in the main loop (checking HOST_LOCK_PATH) already
# prevents this function from being called during agent creation/provisioning,
# since create() holds the lock. The grace period below is an additional safety
# net for any edge cases outside the lock.
#
# Grace period (seconds) after the most recent agent directory creation.
# This prevents false positives when an agent dir exists but the tmux
# session hasn't been started yet (e.g., during provisioning).
AGENT_SESSION_GRACE_PERIOD=120

has_running_agent_sessions() {
    local prefix
    prefix=$(get_tmux_session_prefix)

    # If no prefix configured, skip this check (return true to avoid false positives)
    if [ -z "$prefix" ]; then
        return 0
    fi

    # If no agent directories exist yet, the host is still in its initial setup
    # phase (agents haven't been created yet). Skip the check to avoid shutting
    # down the host before any agent has had a chance to start.
    local agents_dir="$HOST_DATA_DIR/agents"
    if [ ! -d "$agents_dir" ]; then
        return 0
    fi
    local agent_dirs
    agent_dirs=$(find "$agents_dir" -mindepth 1 -maxdepth 1 -type d 2>/dev/null)
    if [ -z "$agent_dirs" ]; then
        return 0
    fi

    # Check if any agent directory was created recently (within grace period).
    # This protects against the window between agent dir creation and tmux
    # session start (e.g., during provisioning which can take minutes).
    local current_time
    current_time=$(date +%s)
    local newest_agent_mtime=0
    for agent_dir in $agent_dirs; do
        # Use stat directly since get_mtime only works on regular files
        local mtime
        mtime=$(stat -c %Y "$agent_dir" 2>/dev/null || stat -f %m "$agent_dir" 2>/dev/null || echo "")
        if [ -n "$mtime" ] && [ "$mtime" -gt "$newest_agent_mtime" ]; then
            newest_agent_mtime=$mtime
        fi
    done
    if [ "$newest_agent_mtime" -gt 0 ]; then
        local age=$((current_time - newest_agent_mtime))
        if [ "$age" -lt "$AGENT_SESSION_GRACE_PERIOD" ]; then
            return 0
        fi
    fi

    # Check how long since the last boot -- if less than the grace period, skip
    # the tmux check to avoid shutting down before agent sessions can start.
    # We use the boot activity file mtime rather than /proc/uptime because
    # Docker container uptime accumulates across start/stop cycles and does
    # not reset on container restart.
    local boot_mtime
    boot_mtime=$(get_mtime "$BOOT_ACTIVITY_PATH")
    if [ -n "$boot_mtime" ]; then
        local seconds_since_boot=$((current_time - boot_mtime))
        if [ "$seconds_since_boot" -lt "$AGENT_SESSION_GRACE_PERIOD" ]; then
            return 0
        fi
    fi

    # List tmux sessions and check if any match the prefix.
    # tmux list-sessions fails if no server is running, which means no sessions.
    local sessions
    sessions=$(tmux list-sessions -F '#{session_name}' 2>/dev/null) || return 1

    # Check if any session name starts with the prefix
    echo "$sessions" | grep -q "^${prefix}" 2>/dev/null
}

# Check if the host has exceeded its maximum age (hard timeout)
# Returns 0 (true) if the host should be shut down due to age, 1 (false) otherwise
check_max_host_age() {
    local max_host_age_seconds
    max_host_age_seconds=$(get_max_host_age)

    # If no max_host_age, no hard timeout applies
    if [ -z "$max_host_age_seconds" ]; then
        return 1
    fi

    # Get boot activity file mtime
    local boot_mtime
    boot_mtime=$(get_mtime "$BOOT_ACTIVITY_PATH")
    if [ -z "$boot_mtime" ]; then
        # No boot activity file yet, can't determine age
        return 1
    fi

    # Check if we've exceeded max age
    local current_time
    current_time=$(date +%s)
    local max_age_deadline=$((boot_mtime + max_host_age_seconds))

    if [ "$current_time" -ge "$max_age_deadline" ]; then
        echo "Host has exceeded maximum age (boot: $boot_mtime, max_age: $max_host_age_seconds, deadline: $max_age_deadline, now: $current_time)"
        return 0
    fi

    return 1
}

# Get the maximum mtime across all activity files for the configured sources
get_max_activity_mtime() {
    local max_mtime=0
    local activity_sources
    activity_sources=$(get_activity_sources)

    # If no activity sources configured (DISABLED mode), return 0
    if [ -z "$activity_sources" ]; then
        echo "0"
        return
    fi

    for source in $activity_sources; do
        if is_host_level_source "$source"; then
            # Host-level source: single file at <host_data_dir>/activity/<source>
            local file="$HOST_DATA_DIR/activity/$source"
            if [ -f "$file" ]; then
                local mtime
                mtime=$(get_mtime "$file")
                if [ -n "$mtime" ] && [ "$mtime" -gt "$max_mtime" ]; then
                    max_mtime=$mtime
                fi
            fi
        else
            # Agent-level source: glob pattern <host_data_dir>/agents/*/activity/<source>
            # shellcheck disable=SC2086
            for file in "$HOST_DATA_DIR"/agents/*/activity/$source; do
                if [ -f "$file" ]; then
                    local mtime
                    mtime=$(get_mtime "$file")
                    if [ -n "$mtime" ] && [ "$mtime" -gt "$max_mtime" ]; then
                        max_mtime=$mtime
                    fi
                fi
            done
        fi
    done

    echo "$max_mtime"
}

main() {
    log "Activity watcher starting for $HOST_DATA_DIR"
    log "Data JSON path: $DATA_JSON_PATH"
    log "Boot activity path: $BOOT_ACTIVITY_PATH"
    log "Shutdown script path: $SHUTDOWN_SCRIPT"
    log "Check interval: $CHECK_INTERVAL seconds"

    while true; do
        echo "--- Activity watcher check at $(date) ---"
        log_debug "Activity watcher check"

        # Log current state for debugging
        if [ -f "$DATA_JSON_PATH" ]; then
            echo "data.json exists"
            local max_host_age_val
            max_host_age_val=$(get_max_host_age)
            echo "max_host_age from data.json: $max_host_age_val"
        else
            echo "data.json NOT found at $DATA_JSON_PATH"
        fi

        if [ -f "$BOOT_ACTIVITY_PATH" ]; then
            local boot_mtime
            boot_mtime=$(get_mtime "$BOOT_ACTIVITY_PATH")
            echo "boot activity file exists, mtime: $boot_mtime"
        else
            echo "boot activity file NOT found at $BOOT_ACTIVITY_PATH"
        fi

        if [ -x "$SHUTDOWN_SCRIPT" ]; then
            echo "shutdown.sh exists and is executable"
        elif [ -f "$SHUTDOWN_SCRIPT" ]; then
            echo "shutdown.sh exists but is NOT executable"
        else
            echo "shutdown.sh NOT found at $SHUTDOWN_SCRIPT"
        fi

        # If the host is locked, don't shut down
        if [ -f "$HOST_LOCK_PATH" ]; then
            sleep "$CHECK_INTERVAL"
            continue
        fi

        # Check if host has exceeded maximum age (hard timeout)
        # This takes precedence over idle timeout to ensure clean shutdown before
        # external timeout (e.g., Modal sandbox timeout) kills the host
        if check_max_host_age; then
            # Call shutdown script if it exists
            if [ -x "$SHUTDOWN_SCRIPT" ]; then
                log "Calling shutdown script due to max host age: $SHUTDOWN_SCRIPT"
                "$SHUTDOWN_SCRIPT"
                # Exit after calling shutdown (the script should handle the actual shutdown)
                exit 0
            else
                echo "Shutdown script not found or not executable: $SHUTDOWN_SCRIPT"
                log_warn "Shutdown script not found or not executable"
                # Continue monitoring in case the script appears later
            fi
        fi

        # Check if all agent tmux sessions have exited.
        # If the prefix is configured and no sessions with that prefix exist,
        # the host should be stopped (not just paused) since all agents are gone.
        if ! has_running_agent_sessions; then
            log "No agent tmux sessions found with prefix '$(get_tmux_session_prefix)'"
            if [ -x "$SHUTDOWN_SCRIPT" ]; then
                log "Calling shutdown script with STOPPED (no agents running): $SHUTDOWN_SCRIPT"
                "$SHUTDOWN_SCRIPT" STOPPED
                exit 0
            else
                echo "Shutdown script not found or not executable: $SHUTDOWN_SCRIPT"
                log_warn "Shutdown script not found or not executable"
            fi
        fi

        # Check if data.json exists
        if [ ! -f "$DATA_JSON_PATH" ]; then
            sleep "$CHECK_INTERVAL"
            continue
        fi

        # Read idle timeout from data.json
        local idle_timeout_seconds
        idle_timeout_seconds=$(get_idle_timeout_seconds)
        if [ -z "$idle_timeout_seconds" ]; then
            sleep "$CHECK_INTERVAL"
            continue
        fi

        # Check if activity sources are configured (DISABLED mode has empty array)
        local activity_sources
        activity_sources=$(get_activity_sources)
        if [ -z "$activity_sources" ]; then
            sleep "$CHECK_INTERVAL"
            continue
        fi

        # Get the maximum activity time
        local max_mtime
        max_mtime=$(get_max_activity_mtime)

        # If no activity files found (max_mtime=0), wait for activity to be recorded
        if [ "$max_mtime" -eq 0 ]; then
            sleep "$CHECK_INTERVAL"
            continue
        fi

        # Calculate idle deadline
        local current_time
        current_time=$(date +%s)
        local idle_deadline=$((max_mtime + idle_timeout_seconds))

        # Check if we're past the idle deadline
        if [ "$current_time" -ge "$idle_deadline" ]; then
            log "Host is idle (last activity: $max_mtime, deadline: $idle_deadline, now: $current_time)"

            # Call shutdown script if it exists
            if [ -x "$SHUTDOWN_SCRIPT" ]; then
                log "Calling shutdown script: $SHUTDOWN_SCRIPT"
                "$SHUTDOWN_SCRIPT"
                # Exit after calling shutdown (the script should handle the actual shutdown)
                exit 0
            else
                echo "Shutdown script not found or not executable: $SHUTDOWN_SCRIPT"
                log_warn "Shutdown script not found or not executable"
                # Continue monitoring in case the script appears later
            fi
        fi

        sleep "$CHECK_INTERVAL"
    done
}

main
