#!/usr/bin/env bash
# Outer-side btrfs snapshot helper for mngr_vps hosts.
#
# Watches /var/lib/mngr-snapshot/request.json (the outer-host view of the
# docker volume bind-mounted into the container at /mngr-snapshot/) and,
# whenever a new request appears, runs the requested btrfs operation
# against the per-host subvolume, then writes a result.json the inner
# host_backup script can read.
#
# Snapshots are created at <btrfs-mount>/snapshots/<name>, where <name> is
# a unique, per-request value chosen by the inner script (a timestamp).
# We never reuse a single fixed path: under gVisor (runsc) the container
# reads the snapshot through the gofer, which caches a handle to the
# directory it first opened. Deleting and recreating one path leaves the
# container reading the stale (deleted) subvolume, so every snapshot after
# the first comes back empty. A fresh name per request avoids that, and
# the inner script garbage-collects old snapshots by name.
#
# Request file format:
#     {"request_id": "<id>", "operation": "snapshot" | "cleanup",
#      "timestamp_iso": "...", "target": "<name>"}
#   - For "snapshot", the snapshot is created at snapshots/<request_id>.
#   - For "cleanup", the snapshot named by "target" is deleted; "request_id"
#     is only used to correlate the result.
#
# Result file format (atomically renamed from result.json.tmp):
#     {"request_id": "<same id>", "operation": "...", "exit_code": int,
#      "stdout": "...", "stderr": "...", "snapshot_path": "..."}
#
# Environment (set by the systemd unit, parameterized at host-create time
# by the install template the mngr_vps provider materializes):
#     MNGR_BTRFS_MOUNT_PATH -- e.g. /mngr-btrfs
#     MNGR_HOST_SUBVOLUME   -- e.g. /mngr-btrfs/<host_id_hex>
#     MNGR_TRIGGER_DIR      -- e.g. /var/lib/mngr-snapshot
set -euo pipefail

# --- config defaults (overridable via env) ----------------------------------
: "${MNGR_BTRFS_MOUNT_PATH:=/mngr-btrfs}"
: "${MNGR_HOST_SUBVOLUME:?MNGR_HOST_SUBVOLUME must be set}"
: "${MNGR_TRIGGER_DIR:=/var/lib/mngr-snapshot}"

SNAPSHOTS_DIR="${MNGR_BTRFS_MOUNT_PATH}/snapshots"
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

# Return 0 iff `name` is a safe single path component (a child of the
# snapshots dir). Rejects empty, ".", "..", anything containing "/" or
# "..", so a malformed request can never escape the snapshots directory or
# target the live subvolume.
is_safe_name() {
    local name="$1"
    case "$name" in
        "" | . | ..) return 1 ;;
        */* | *..*) return 1 ;;
    esac
    return 0
}

do_snapshot() {
    local name="$1"
    local stdout stderr exit_code

    if ! is_safe_name "$name"; then
        emit_result "$name" "snapshot" 2 "" "invalid snapshot name: ${name}" ""
        return
    fi

    local target="${SNAPSHOTS_DIR}/${name}"
    mkdir -p "$SNAPSHOTS_DIR"

    # Names are unique per request, so a collision means a stale leftover at
    # this exact name. Fail rather than overwrite; the next request uses a
    # fresh name and recovers.
    if [ -e "$target" ]; then
        emit_result "$name" "snapshot" 1 "" "snapshot path already exists: ${name}" ""
        return
    fi

    local out_file err_file
    out_file=$(mktemp)
    err_file=$(mktemp)
    set +e
    btrfs subvolume snapshot -r "$MNGR_HOST_SUBVOLUME" "$target" >"$out_file" 2>"$err_file"
    exit_code=$?
    set -e
    stdout=$(cat "$out_file"); rm -f "$out_file"
    stderr=$(cat "$err_file"); rm -f "$err_file"

    local effective_snapshot_path=""
    if [ "$exit_code" -eq 0 ]; then
        effective_snapshot_path="$target"
    fi
    emit_result "$name" "snapshot" "$exit_code" "$stdout" "$stderr" "$effective_snapshot_path"
}

do_cleanup() {
    local request_id="$1" target="$2"
    local stdout="" stderr="" exit_code=0

    if ! is_safe_name "$target"; then
        emit_result "$request_id" "cleanup" 2 "" "invalid cleanup target: ${target}" ""
        return
    fi

    local path="${SNAPSHOTS_DIR}/${target}"
    # "Already gone" is success: cleanup is idempotent.
    if [ -e "$path" ]; then
        local out_file err_file
        out_file=$(mktemp)
        err_file=$(mktemp)
        set +e
        btrfs subvolume delete "$path" >"$out_file" 2>"$err_file"
        exit_code=$?
        set -e
        stdout=$(cat "$out_file"); rm -f "$out_file"
        stderr=$(cat "$err_file"); rm -f "$err_file"
    fi
    emit_result "$request_id" "cleanup" "$exit_code" "$stdout" "$stderr" ""
}

handle_request() {
    local payload request_id operation target last_result_request_id
    payload=$(cat "$REQUEST_PATH" 2>/dev/null || echo "{}")
    request_id=$(echo "$payload" | jq -r '.request_id // ""')
    operation=$(echo "$payload" | jq -r '.operation // ""')
    target=$(echo "$payload" | jq -r '.target // ""')
    if [ -z "$request_id" ]; then
        echo "snapshot_helper: request missing request_id; skipping" >&2
        return
    fi
    # Idempotency guard: skip a request we have already produced a result for.
    # request_ids are unique per request (a timestamp for snapshots, a uuid for
    # cleanups), so re-seeing one means we are re-reading an un-consumed
    # request.json -- e.g. on a helper restart, whose startup re-runs whatever
    # request is still on disk. Without this, re-running a snapshot whose path now
    # exists would overwrite a good result.json with a spurious "already exists"
    # failure (and re-running cleanup would needlessly churn result.json). The
    # requester uses a fresh request_id each time, so this never suppresses a real
    # new request; a genuinely-unserviced request (no matching result yet) still
    # runs via the startup path below.
    last_result_request_id=$(jq -r '.request_id // ""' "$RESULT_PATH" 2>/dev/null || echo "")
    if [ "$request_id" = "$last_result_request_id" ]; then
        return
    fi
    case "$operation" in
        # For a snapshot the request_id doubles as the snapshot name.
        snapshot) do_snapshot "$request_id" ;;
        cleanup)  do_cleanup  "$request_id" "$target" ;;
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
