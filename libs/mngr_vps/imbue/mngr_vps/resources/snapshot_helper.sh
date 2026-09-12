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
# Result file format (atomically renamed into place from a random-named
# temp file in the same directory):
#     {"request_id": "<same id>", "operation": "...", "exit_code": int,
#      "stdout": "...", "stderr": "...", "snapshot_path": "..."}
#
# Trust boundary: the trigger directory is a plain bind mount shared
# read-write with the workspace container, and no user namespace separates
# the two, so root inside the container is root on this directory and can
# plant anything in it -- symlinks above all -- at any moment. This script
# runs as VM root and must therefore never let a name in that directory
# decide which inode it reads or writes:
#   - bash's `>` redirection, `cat`, `[ -f ]` and `[ -e ]` all follow
#     symlinks, so a planted `request.json -> /etc/shadow` would make us read
#     an arbitrary VM file, and a symlink planted at a predictable staging
#     name would make us truncate and overwrite a VM file with
#     request-derived content;
#   - checking `[ -L ]` before acting is a race (the container can swap the
#     entry between the check and the open), so every open below is done
#     with O_NOFOLLOW and the type is checked on the OPEN descriptor, which
#     bash cannot express -- hence the perl one-liners (perl ships on every
#     Debian image we run this on);
#   - results are written to a random-named temp file created with
#     O_CREAT|O_EXCL in the SAME directory and then rename(2)d over
#     result.json: O_EXCL cannot be satisfied by a pre-planted entry, the
#     create and the write share one descriptor so nothing can be swapped in
#     between, and rename(2) replaces whatever sits at the destination
#     (a symlink included) without following it. A temp file elsewhere
#     (e.g. /tmp) would be wrong: `mv` across filesystems copies into the
#     destination path and follows a symlink there again.
# Only the trigger directory is hostile in this way; the snapshots
# directory is exposed to the container read-only and the request payload's
# names are confined to it by is_safe_name.
#
# Environment (set by the systemd unit, parameterized at host-create time
# by the install template the mngr_vps provider materializes):
#     MNGR_BTRFS_MOUNT_PATH -- e.g. /mngr-btrfs
#     MNGR_HOST_SUBVOLUME   -- e.g. /mngr-btrfs/<host_id_hex>
#     MNGR_TRIGGER_DIR      -- e.g. /var/lib/mngr-snapshot
#     MNGR_MOUNT_POLL_SECONDS -- seconds between mount probes while deferring
#                                a request (default 5; tests override)
set -euo pipefail

# --- config defaults (overridable via env) ----------------------------------
: "${MNGR_BTRFS_MOUNT_PATH:=/mngr-btrfs}"
: "${MNGR_HOST_SUBVOLUME:?MNGR_HOST_SUBVOLUME must be set}"
: "${MNGR_TRIGGER_DIR:=/var/lib/mngr-snapshot}"
: "${MNGR_MOUNT_POLL_SECONDS:=5}"

SNAPSHOTS_DIR="${MNGR_BTRFS_MOUNT_PATH}/snapshots"
REQUEST_PATH="${MNGR_TRIGGER_DIR}/request.json"
RESULT_PATH="${MNGR_TRIGGER_DIR}/result.json"

# --- helpers ----------------------------------------------------------------

# Print the content of the regular file at $1 without following a symlink
# there; exit non-zero (printing nothing) when the entry is absent, is a
# symlink, or is anything but a regular file. See the trust-boundary note in
# the header for why this is perl and why the type check is on the open
# descriptor. O_NONBLOCK keeps a planted FIFO from parking us forever on
# the open (a FIFO fails the regular-file check right after).
read_regular_file() {
    perl -MFcntl -e '
        my $path = shift;
        sysopen(my $fh, $path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK) or exit 1;
        -f $fh or exit 1;
        local $/;
        my $content = <$fh>;
        print $content if defined $content;
    ' "$1"
}

# Write stdin to the regular file at $1, atomically and without following a
# planted symlink at (or on the way to) the destination: create a
# random-named temp file in the same directory with O_CREAT|O_EXCL|O_NOFOLLOW,
# write through that same descriptor, then rename(2) it over $1. See the
# trust-boundary note in the header.
write_regular_file_atomically() {
    perl -MFcntl -e '
        my $path = shift;
        my ($dir, $name) = $path =~ m{^(.*)/([^/]+)$} ? ($1, $2) : (".", $path);
        my ($fh, $tmp);
        for (1 .. 100) {
            my $suffix = join "", map { ("a" .. "z", "A" .. "Z", 0 .. 9)[rand 62] } 1 .. 12;
            $tmp = "$dir/.$name.$suffix";
            last if sysopen($fh, $tmp, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0644);
            undef $fh;
        }
        defined $fh or die "could not create a temp file next to $path: $!\n";
        local $/;
        my $content = <STDIN>;
        (print {$fh} $content) && close($fh) or do { unlink $tmp; die "could not write $tmp: $!\n" };
        rename($tmp, $path) or do { unlink $tmp; die "could not rename $tmp over $path: $!\n" };
    ' "$1"
}

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
        '{request_id: $request_id, operation: $operation, exit_code: $exit_code, stdout: $stdout, stderr: $stderr, snapshot_path: $snapshot_path}' |
        write_regular_file_atomically "$RESULT_PATH"
}

# Block until the data volume is actually mounted. On slice VMs the btrfs
# disk is mounted by the guest's lima provisioning at a highly variable point
# late in boot (well after local-fs.target, which this unit orders on), and
# the trigger dir lives on the root fs, so a request can be waiting before
# the volume exists -- e.g. this script's own startup replay of a request
# left over from before a reboot. Servicing it early is doubly wrong: the
# mkdir/btrfs calls write shadow debris onto the root fs beneath the
# unmounted mountpoint (which once defeated the workspace autostart trigger;
# see default-workspace-template#381), and the request fails when it only
# needed to wait a few seconds -- and then never retries, because the
# result-id guard retires it. Deferring is always correct: the inner
# requester enforces its own result timeout, and a deferred request serviced
# post-mount produces the result it would have produced on a healthy boot.
wait_until_data_volume_mounted() {
    local waited=0
    until mountpoint -q "$(readlink -f "$MNGR_BTRFS_MOUNT_PATH")"; do
        if [ "$((waited % 60))" -eq 0 ]; then
            echo "snapshot_helper: waiting for the data volume at ${MNGR_BTRFS_MOUNT_PATH} to be mounted (waited ${waited}s)" >&2
        fi
        sleep "$MNGR_MOUNT_POLL_SECONDS"
        waited=$((waited + MNGR_MOUNT_POLL_SECONDS))
    done
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
    # Defer (never fail) requests that arrive before the data volume is up.
    # Waiting BEFORE reading the payload matters: the deferral can last
    # minutes, and a request superseded during it should never be serviced --
    # once the volume appears we read (and answer) the newest request on disk.
    wait_until_data_volume_mounted
    # A request that is not a regular file (a planted symlink, FIFO, ...) is
    # treated exactly like an absent one: nothing is read through it.
    payload=$(read_regular_file "$REQUEST_PATH" 2>/dev/null || echo "{}")
    request_id=$(echo "$payload" | jq -r '.request_id // ""')
    operation=$(echo "$payload" | jq -r '.operation // ""')
    target=$(echo "$payload" | jq -r '.target // ""')
    if [ -z "$request_id" ]; then
        echo "snapshot_helper: request missing request_id (or request.json is not a regular file); skipping" >&2
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
    last_result_request_id=$(read_regular_file "$RESULT_PATH" 2>/dev/null | jq -r '.request_id // ""' 2>/dev/null || echo "")
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
# The probe is the same no-follow open as the read itself (`[ -f ]` would
# follow a planted symlink and report whatever it points at).
if read_regular_file "$REQUEST_PATH" >/dev/null 2>&1; then
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
