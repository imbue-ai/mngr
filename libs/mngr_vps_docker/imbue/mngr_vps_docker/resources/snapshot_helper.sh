#!/usr/bin/env bash
# Outer-side btrfs snapshot helper for vps-docker hosts.
#
# Watches /var/lib/mngr-snapshot/request.json (the outer-host view of the
# docker volume bind-mounted into the container at /mngr-snapshot/) and,
# whenever a new request appears, runs the requested btrfs operation
# against the per-host subvolume, then writes a result.json the inner
# host_backup script can read.
#
# Request file format:
#     {"request_id": "<uuid>", "operation": "snapshot" | "cleanup",
#      "timestamp_iso": "..."}
#
# Result file format (atomically renamed from result.json.tmp):
#     {"request_id": "<same uuid>", "operation": "...", "exit_code": int,
#      "stdout": "...", "stderr": "...", "snapshot_path": "..."}
#
# Environment (set by the systemd unit, parameterized at host-create time
# by the install template the mngr_vps_docker provider materializes):
#     MNGR_BTRFS_MOUNT_PATH -- e.g. /mngr-btrfs
#     MNGR_HOST_SUBVOLUME   -- e.g. /mngr-btrfs/<host_id_hex>
#     MNGR_TRIGGER_DIR      -- e.g. /var/lib/mngr-snapshot
set -euo pipefail

# --- config defaults (overridable via env) ----------------------------------
: "${MNGR_BTRFS_MOUNT_PATH:=/mngr-btrfs}"
: "${MNGR_HOST_SUBVOLUME:?MNGR_HOST_SUBVOLUME must be set}"
: "${MNGR_TRIGGER_DIR:=/var/lib/mngr-snapshot}"

SNAPSHOT_PATH="${MNGR_BTRFS_MOUNT_PATH}/snapshots/current"
REQUEST_PATH="${MNGR_TRIGGER_DIR}/request.json"
RESULT_PATH="${MNGR_TRIGGER_DIR}/result.json"
RESULT_TMP="${MNGR_TRIGGER_DIR}/result.json.tmp"

# --- helpers ----------------------------------------------------------------

# Emit a result.json. Args: request_id operation exit_code stdout stderr snapshot_path
emit_result() {
    local request_id="$1" operation="$2" exit_code="$3" stdout="$4" stderr="$5" snapshot_path="$6"
    # Use jq for safe JSON encoding (handles quoting/escaping of stdout/stderr).
    jq -n \
        --arg request_id "$request_id" \
        --arg operation "$operation" \
        --argjson exit_code "$exit_code" \
        --arg stdout "$stdout" \
        --arg stderr "$stderr" \
        --arg snapshot_path "$snapshot_path" \
        '{request_id: $request_id, operation: $operation, exit_code: $exit_code, stdout: $stdout, stderr: $stderr, snapshot_path: $snapshot_path}' \
        > "$RESULT_TMP"
    mv "$RESULT_TMP" "$RESULT_PATH"
}

do_snapshot() {
    local request_id="$1"
    local stdout stderr exit_code

    # Defensive cleanup: if a stale `current/` snapshot is present, delete
    # it first so `btrfs subvolume snapshot` doesn't fail with "exists".
    if [ -d "$SNAPSHOT_PATH" ]; then
        btrfs subvolume delete "$SNAPSHOT_PATH" >/dev/null 2>&1 || true
    fi
    mkdir -p "$(dirname "$SNAPSHOT_PATH")"

    local out_file err_file
    out_file=$(mktemp)
    err_file=$(mktemp)
    set +e
    btrfs subvolume snapshot -r "$MNGR_HOST_SUBVOLUME" "$SNAPSHOT_PATH" >"$out_file" 2>"$err_file"
    exit_code=$?
    set -e
    stdout=$(cat "$out_file"); rm -f "$out_file"
    stderr=$(cat "$err_file"); rm -f "$err_file"

    local effective_snapshot_path=""
    if [ "$exit_code" -eq 0 ]; then
        effective_snapshot_path="$SNAPSHOT_PATH"
    fi
    emit_result "$request_id" "snapshot" "$exit_code" "$stdout" "$stderr" "$effective_snapshot_path"
}

do_cleanup() {
    local request_id="$1"
    local stdout="" stderr="" exit_code=0

    if [ -d "$SNAPSHOT_PATH" ]; then
        local out_file err_file
        out_file=$(mktemp)
        err_file=$(mktemp)
        set +e
        btrfs subvolume delete "$SNAPSHOT_PATH" >"$out_file" 2>"$err_file"
        exit_code=$?
        set -e
        stdout=$(cat "$out_file"); rm -f "$out_file"
        stderr=$(cat "$err_file"); rm -f "$err_file"
    fi
    emit_result "$request_id" "cleanup" "$exit_code" "$stdout" "$stderr" ""
}

handle_request() {
    local payload request_id operation
    payload=$(cat "$REQUEST_PATH" 2>/dev/null || echo "{}")
    request_id=$(echo "$payload" | jq -r '.request_id // ""')
    operation=$(echo "$payload" | jq -r '.operation // ""')
    if [ -z "$request_id" ]; then
        echo "snapshot_helper: request missing request_id; skipping" >&2
        return
    fi
    case "$operation" in
        snapshot) do_snapshot "$request_id" ;;
        cleanup)  do_cleanup  "$request_id" ;;
        *)
            emit_result "$request_id" "$operation" 2 "" "unknown operation: $operation" ""
            ;;
    esac
}

# --- main loop --------------------------------------------------------------

mkdir -p "$MNGR_TRIGGER_DIR"

# Process any request that's already on disk at startup (covers the case
# where the helper restarted while the inner script was waiting for a result).
if [ -f "$REQUEST_PATH" ]; then
    handle_request
fi

# inotifywait blocks until the file is modified or renamed-into-place. We
# care about both because the inner script writes request.json.tmp then
# renames; the rename surfaces as MOVED_TO.
exec inotifywait -m -e close_write,moved_to --format '%f' "$MNGR_TRIGGER_DIR" |
    while read -r filename; do
        if [ "$filename" = "request.json" ]; then
            handle_request
        fi
    done
