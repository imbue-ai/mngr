#!/bin/bash
# Wrapper script that starts the Docker daemon (if not already running)
# and then exec's the given command with all arguments.
# Available in release images as a command prefix for scripts that need Docker.
set -euo pipefail

if ! docker info >/dev/null 2>&1 && [ -x /start-dockerd.sh ]; then
    # Capture combined output so we can surface it on failure. start-dockerd.sh
    # runs with `set -x`, which is noisy on success, so we only print the log
    # when the script fails (to aid debugging of opaque dockerd startup issues).
    dockerd_log=$(mktemp)
    # Capture the real exit code via `|| rc=$?`. Using `if ! cmd; then rc=$?`
    # would set rc=0 inside the then-branch (because $? holds the negated
    # pipeline's status), which would mask failures.
    rc=0
    /start-dockerd.sh >"$dockerd_log" 2>&1 || rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "start-dockerd.sh failed (exit $rc); output follows:" >&2
        cat "$dockerd_log" >&2
        exit "$rc"
    fi
fi

exec "$@"
