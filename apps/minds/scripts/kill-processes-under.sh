#!/usr/bin/env bash
# SIGKILL every process that has a file under any of the given directories (its
# executable, a mapped library, its cwd, or an open file) and wait for each one
# to exit, rescanning until none is left. Exits non-zero if that is not reached.
#
# Usage: kill-processes-under.sh DIR...
set -euo pipefail

readonly MAX_ROUNDS=5
readonly EXIT_POLLS=100
readonly EXIT_POLL_INTERVAL_SECONDS=0.05

log() { printf '[kill-processes-under] %s\n' "$*" >&2; }

if [[ $# -eq 0 ]]; then
  log "usage: kill-processes-under.sh DIR..."
  exit 2
fi

# lsof reports physical paths, so each existing root is resolved to its own.
roots=()
for root in "$@"; do
  if [[ "$root" != /?* ]]; then
    log "ERROR: '$root' is not an absolute path below /"
    exit 2
  fi
  if [[ -d "$root" ]]; then
    roots+=("$(cd "$root" && pwd -P)")
  else
    roots+=("$root")
  fi
done
ROOTS_NEWLINE_SEPARATED="$(printf '%s\n' "${roots[@]}")"
export ROOTS_NEWLINE_SEPARATED

pids_with_files_under_roots() {
  lsof -n -P -w -Fpn | awk '
    BEGIN { root_count = split(ENVIRON["ROOTS_NEWLINE_SEPARATED"], root, "\n") }
    /^p/ { pid = substr($0, 2); next }
    /^n/ {
      path = substr($0, 2)
      for (i = 1; i <= root_count; i++) {
        if (path == root[i] || index(path, root[i] "/") == 1) {
          print pid
          next
        }
      }
    }
  ' | sort -u
}

# A zombie (state Z) has already closed its files and runs no code.
is_running() {
  local state
  state=$(ps -o stat= -p "$1") || return 1
  [[ "$state" != *Z* ]]
}

wait_for_exit() {
  local pid=$1
  for ((poll = 0; poll < EXIT_POLLS; poll++)); do
    is_running "$pid" || return 0
    sleep "$EXIT_POLL_INTERVAL_SECONDS"
  done
  return 1
}

for ((round = 1; round <= MAX_ROUNDS; round++)); do
  pids=$(pids_with_files_under_roots)
  if [[ -z "$pids" ]]; then
    exit 0
  fi
  for pid in $pids; do
    log "SIGKILL $pid ($(ps -o ucomm= -p "$pid" || true))"
    kill -9 "$pid" 2>/dev/null || true
  done
  for pid in $pids; do
    if ! wait_for_exit "$pid"; then
      log "ERROR: $pid is still running after SIGKILL"
      exit 1
    fi
  done
done
log "ERROR: processes still have files under the given directories after $MAX_ROUNDS rounds"
exit 1
