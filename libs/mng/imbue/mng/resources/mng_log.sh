#!/usr/bin/env bash
set -euo pipefail
# mng_log.sh -- Shared JSONL logging library and timestamp utilities for mng
# bash scripts.
#
# Source this file after setting the required variables:
#   _MNG_LOG_TYPE    - the event type (e.g. "event_watcher", "chat")
#   _MNG_LOG_SOURCE  - the event source (e.g. "event_watcher", "chat")
#   _MNG_LOG_FILE    - absolute path to the JSONL log file
#
# Provides:
#   mng_timestamp                    - print ISO 8601 UTC timestamp with best
#                                      available sub-second precision
#   _json_escape <string>            - escape a string for JSON embedding
#   _log_jsonl <level> <message>     - write a JSONL log line to $_MNG_LOG_FILE
#   log_info <message>               - log at INFO level
#   log_debug <message>              - log at DEBUG level
#   log_warn <message>               - log at WARNING level
#   log_error <message>              - log at ERROR level
#
# Level names match Python's loguru: DEBUG, INFO, WARNING, ERROR.
# Note: this file sets strict mode (set -euo pipefail) -- all sourcing
# scripts are expected to use strict mode as well.

# ---------------------------------------------------------------------------
# Timestamp generation
# ---------------------------------------------------------------------------
# Detect the best available method for generating high-precision timestamps.
# This runs once when the file is sourced so that every subsequent call to
# mng_timestamp is fast.
#
# Methods (in preference order):
#   gnu   - GNU coreutils date with nanosecond %N support (Linux)
#   perl  - perl with Time::HiRes for microsecond precision (macOS with perl)
#   basic - BSD/macOS date without sub-second precision (zero-padded to 9 digits)
_MNG_TIMESTAMP_METHOD=""

_mng_detect_timestamp_method() {
    local test_ts
    test_ts=$(date -u +"%Y-%m-%dT%H:%M:%S.%NZ" 2>/dev/null) || true
    if [[ "$test_ts" != *"%N"* ]]; then
        _MNG_TIMESTAMP_METHOD="gnu"
        return
    fi
    if perl -MTime::HiRes=gettimeofday -e '1' 2>/dev/null; then
        _MNG_TIMESTAMP_METHOD="perl"
        return
    fi
    _MNG_TIMESTAMP_METHOD="basic"
}

_mng_detect_timestamp_method

mng_timestamp() {
    case "$_MNG_TIMESTAMP_METHOD" in
        gnu)
            date -u +"%Y-%m-%dT%H:%M:%S.%NZ"
            ;;
        perl)
            perl -MTime::HiRes=gettimeofday -MPOSIX=strftime \
                -e '($s,$us)=gettimeofday();printf "%s.%09dZ\n",strftime("%Y-%m-%dT%H:%M:%S",gmtime($s)),$us*1000'
            ;;
        basic)
            date -u +"%Y-%m-%dT%H:%M:%S.000000000Z"
            ;;
    esac
}

# ---------------------------------------------------------------------------
# JSON escaping
# ---------------------------------------------------------------------------

_json_escape() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    s="${s//$'\n'/\\n}"
    s="${s//$'\r'/\\r}"
    s="${s//$'\t'/\\t}"
    printf '%s' "$s"
}

# ---------------------------------------------------------------------------
# JSONL logging
# ---------------------------------------------------------------------------

_log_jsonl() {
    local level="$1"
    local msg="$2"
    local ts
    ts=$(mng_timestamp)
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
