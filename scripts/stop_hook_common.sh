#!/bin/bash
set -euo pipefail
#
# stop_hook_common.sh
#
# Shared function definitions for stop hook scripts. Source this file to get
# logging helpers and retry_command. Sources mng_log.sh for JSONL logging.

# Colors for output (disabled if not a terminal)
if [[ -t 2 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[0;33m'
    NC='\033[0m'
else
    RED=''
    GREEN=''
    YELLOW=''
    NC=''
fi

# File logging: all log functions write to $STOP_HOOK_LOG if set.
# Each sourcing script should set STOP_HOOK_LOG before calling log functions.
# Format: JSONL with standard envelope
STOP_HOOK_LOG="${STOP_HOOK_LOG:-}"
STOP_HOOK_SCRIPT_NAME="${STOP_HOOK_SCRIPT_NAME:-unknown}"

# Source the shared logging library for _json_escape and _log_jsonl.
# Configure the library variables so _log_to_file can delegate to _log_jsonl.
_MNG_LOG_TYPE="stop_hook"
_MNG_LOG_SOURCE="logs/stop_hook"
_MNG_LOG_FILE="${STOP_HOOK_LOG:-/dev/null}"
# shellcheck source=mng_log.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/mng_log.sh"

_log_to_file() {
    local level="$1"
    local msg="$2"
    if [[ -n "$STOP_HOOK_LOG" ]]; then
        _MNG_LOG_FILE="$STOP_HOOK_LOG"
        _log_jsonl "$level" "$msg"
    fi
}

log_error() {
    echo -e "${RED}ERROR: $1${NC}" >&2
    _log_to_file "ERROR" "$1"
}

log_warn() {
    echo -e "${YELLOW}WARN: $1${NC}" >&2
    _log_to_file "WARNING" "$1"
}

log_info() {
    echo -e "${GREEN}$1${NC}"
    _log_to_file "INFO" "$1"
}

log_debug() {
    _log_to_file "DEBUG" "$1"
}

# Retry a command with exponential backoff
# Usage: retry_command <max_retries> <command...>
retry_command() {
    local max_retries=$1
    shift
    local attempt=1
    local wait_time=1

    while [[ $attempt -le $max_retries ]]; do
        if "$@"; then
            return 0
        fi

        if [[ $attempt -lt $max_retries ]]; then
            log_warn "Command failed (attempt $attempt/$max_retries), retrying in ${wait_time}s..."
            sleep "$wait_time"
            wait_time=$((wait_time * 2))
        fi
        attempt=$((attempt + 1))
    done

    log_error "Command failed after $max_retries attempts: $*"
    return 1
}
