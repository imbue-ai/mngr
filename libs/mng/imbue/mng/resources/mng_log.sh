#!/bin/bash
set -euo pipefail
# mng_log.sh -- Shared JSONL logging library for mng bash scripts.
#
# Source this file after setting the required variables:
#   _MNG_LOG_TYPE    - the event type (e.g. "event_watcher", "chat")
#   _MNG_LOG_SOURCE  - the event source (e.g. "event_watcher", "chat")
#   _MNG_LOG_FILE    - absolute path to the JSONL log file
#
# Provides:
#   _json_escape <string>         - escape a string for JSON embedding
#   _log_jsonl <level> <message>  - write a JSONL log line to $_MNG_LOG_FILE
#   log_info <message>            - log at INFO level
#   log_debug <message>           - log at DEBUG level
#   log_warn <message>            - log at WARNING level
#   log_error <message>           - log at ERROR level
#
# Level names match Python's loguru: DEBUG, INFO, WARNING, ERROR.
# Note: this file sets strict mode (set -euo pipefail) -- all sourcing
# scripts are expected to use strict mode as well.

_json_escape() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    s="${s//$'\n'/\\n}"
    s="${s//$'\r'/\\r}"
    s="${s//$'\t'/\\t}"
    printf '%s' "$s"
}

_log_jsonl() {
    local level="$1"
    local msg="$2"
    # GNU date supports %N (nanoseconds); macOS BSD date does not.
    # Fall back to zero-padded microseconds on macOS.
    local ts
    ts=$(date -u +"%Y-%m-%dT%H:%M:%S.%NZ" 2>/dev/null)
    if [[ "$ts" == *"%N"* ]]; then
        ts=$(date -u +"%Y-%m-%dT%H:%M:%S.000000000Z")
    fi
    local eid
    eid="evt-$(head -c 16 /dev/urandom | xxd -p)"
    local escaped_msg
    escaped_msg=$(_json_escape "$msg")
    mkdir -p "$(dirname "$_MNG_LOG_FILE")"
    printf '{"timestamp":"%s","type":"%s","event_id":"%s","source":"%s","level":"%s","message":"%s","pid":%s}\n' \
        "$ts" "$_MNG_LOG_TYPE" "$eid" "$_MNG_LOG_SOURCE" "$level" "$escaped_msg" "$$" >> "$_MNG_LOG_FILE"
}

log_info() {
    _log_jsonl "INFO" "$*"
}

log_debug() {
    _log_jsonl "DEBUG" "$*"
}

log_warn() {
    _log_jsonl "WARNING" "$*"
}

log_error() {
    _log_jsonl "ERROR" "$*"
}
