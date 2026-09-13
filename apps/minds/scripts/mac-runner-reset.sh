#!/usr/bin/env bash
# Reset the mac-runner to a clean state for a verification run.
# Wipes Minds state and the installed .app; preserves Lima's base-image cache.
# Optional arg: a .zip URL to download and install as the fresh app.
#
# Deliberately NOT `set -e`: this is a best-effort cleanup of a non-ephemeral
# self-hosted runner, and every step must run even if an earlier one fails.
# Under `set -e` a single unguarded failure (e.g. a `df`/`find` pipe, a
# `defaults read`) aborts the script and SKIPS the remaining cleanup, leaking
# Lima VMs / disk. So instead: run every step best-effort, then VERIFY the end
# state (no surviving minds-host VMs / data disks, no ~/.minds, app removed) and exit
# non-zero if the runner is not actually clean -- otherwise a leaked VM rots
# the runner silently. Callers surface that exit code (the post-test cleanup
# step no longer swallows it with `|| true`). The install block fails loud too.
set -uo pipefail

log() { printf '[reset] %s\n' "$*" >&2; }

log "asking Minds to quit"
osascript -e 'tell application "Minds" to quit' 2>/dev/null || true
for _ in 1 2 3 4 5; do
  pids=$(pgrep -f '/Applications/minds.app/Contents/' || true)
  [[ -z "$pids" ]] && break
  sleep 1
done
pids=$(pgrep -f '/Applications/minds.app/Contents/' || true)
for pid in $pids; do
  log "force-kill straggler $pid"
  kill -9 "$pid" 2>/dev/null || true
done

BUNDLED_LIMACTL="/Applications/Minds.app/Contents/Resources/lima/bin/limactl"
LIMACTL=""
if [[ -x "$BUNDLED_LIMACTL" ]]; then
  LIMACTL="$BUNDLED_LIMACTL"
elif command -v limactl >/dev/null 2>&1; then
  LIMACTL="limactl"
fi
if [[ -n "$LIMACTL" ]]; then
  log "stopping and deleting Lima VM instances via $LIMACTL"
  "$LIMACTL" stop --all >/dev/null 2>&1 || true
  "$LIMACTL" delete --all >/dev/null 2>&1 || true
fi

# Belt and suspenders for the CI runner: rm the Lima state minds e2e runs
# leave under ~/.lima/. The pre-run reset installs the app only at the end,
# so here the bundled limactl is absent and the guard above skipped -- this
# fallback is then the ONLY cleanup that runs. It must match how minds names
# its Lima state: instances are `minds-host-<host_id>` (~/.lima/minds-host-*),
# and each mounts a data disk `mngr-<host_id>-data` (~/.lima/_disks/mngr-*-data).
# limactl delete does NOT reap the detached data disk, so the disks leak even
# when limactl runs; clean them here unconditionally. (Unchecked, this reached
# 3 zombie VMs + 226 orphan data disks / 170GB on the self-hosted mac runner.)
#
# limactl delete differs from rm -rf in that it stops the VM's hypervisor
# first. Preserve that: for each instance dir, if ha.pid names a live process,
# SIGTERM (then SIGKILL on a 2s grace) before rm -rf so we never orphan a
# running VM. Data disks are plain files -- no hypervisor to stop.
if [[ -d "$HOME/.lima" ]]; then
  instance_count=$(find "$HOME/.lima" -maxdepth 1 -type d -name 'minds-host-*' 2>/dev/null | wc -l | tr -d ' ')
  if [[ "$instance_count" -gt 0 ]]; then
    log "cleaning $instance_count minds-host-* instance dir(s) under ~/.lima"
    for vm_dir in "$HOME/.lima"/minds-host-*; do
      [[ -d "$vm_dir" ]] || continue
      pid_file="$vm_dir/ha.pid"
      if [[ -f "$pid_file" ]]; then
        pid=$(cat "$pid_file" 2>/dev/null || true)
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
          log "  $(basename "$vm_dir"): hypervisor pid=$pid alive, SIGTERM then SIGKILL"
          kill "$pid" 2>/dev/null || true
          for _ in 1 2 3 4; do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.5
          done
          kill -9 "$pid" 2>/dev/null || true
        fi
      fi
      rm -rf "$vm_dir" 2>/dev/null || true
    done
  fi
  disk_count=$(find "$HOME/.lima/_disks" -maxdepth 1 -type d -name 'mngr-*-data' 2>/dev/null | wc -l | tr -d ' ')
  if [[ "$disk_count" -gt 0 ]]; then
    log "cleaning $disk_count orphaned mngr-*-data disk(s) under ~/.lima/_disks"
    rm -rf "$HOME/.lima/_disks"/mngr-*-data 2>/dev/null || true
  fi
fi

# Local Time Machine snapshots hold deleted-file blocks until purged.
# After a sequence of CI runs that each create+destroy a Lima VM (whose
# diffdisk is up to 100GB), those snapshots can pin ~50-100GB even after
# limactl delete --all reclaims the user-visible files. Free them so the
# next Lima diffdisk conversion ("no space left on device" -- run
# 27060995662) has room.
log "freeing local Time Machine snapshots (best effort)"
sudo tmutil deletelocalsnapshots / 2>/dev/null || true

log "disk usage after cleanup:"
df -h "$HOME" / 2>&1 | sed 's/^/[reset]   /' >&2

log "wiping leftover /tmp diagnostic artifacts from prior runs"
# Only /tmp/minds-electron.log persists across runs (re-written each
# launch); the deleted first-message-* artifacts were produced by
# scripts that no longer exist.
rm -f /tmp/minds-electron.log 2>/dev/null || true

log "removing ~/.minds and /Applications/minds.app"
# `rm -rf` can race against a not-yet-fully-dead Minds backend process that
# is still writing to ~/.minds/Cache or ~/.minds/Code Cache. Retry a few
# times with a short backoff before giving up.
for attempt in 1 2 3 4 5; do
  if rm -rf "$HOME/.minds" 2>/dev/null; then
    break
  fi
  log "  rm ~/.minds attempt $attempt failed (likely still being written); waiting 2s"
  sleep 2
  if [[ $attempt -eq 5 ]]; then
    log "  forcing one more pass with verbose errors"
    rm -rf "$HOME/.minds" || true
  fi
done
sudo rm -rf /Applications/minds.app

URL="${1:-}"

# Verify the cleanup actually reached a clean state. The steps above are
# best-effort and swallow their own failures, so without this check a pinned
# Lima VM or a busy ~/.minds would leak silently and rot this non-ephemeral
# runner. Assert the post-conditions and exit non-zero so the caller's job
# goes red. A pure cleanup (no install URL) also expects the app to be gone;
# when a URL is given the install below puts a fresh one back.
cleanup_failed=0
surviving_vms=$(find "$HOME/.lima" -maxdepth 1 -type d -name 'minds-host-*' 2>/dev/null | wc -l | tr -d ' ')
if [[ "$surviving_vms" -gt 0 ]]; then
  log "ERROR: $surviving_vms minds-host-* VM dir(s) survived cleanup under ~/.lima"
  cleanup_failed=1
fi
surviving_disks=$(find "$HOME/.lima/_disks" -maxdepth 1 -type d -name 'mngr-*-data' 2>/dev/null | wc -l | tr -d ' ')
if [[ "$surviving_disks" -gt 0 ]]; then
  log "ERROR: $surviving_disks mngr-*-data disk(s) survived cleanup under ~/.lima/_disks"
  cleanup_failed=1
fi
if [[ -e "$HOME/.minds" ]]; then
  log "ERROR: ~/.minds survived cleanup"
  cleanup_failed=1
fi
if [[ -z "$URL" && -e /Applications/minds.app ]]; then
  log "ERROR: /Applications/minds.app survived cleanup"
  cleanup_failed=1
fi
if [[ "$cleanup_failed" -ne 0 ]]; then
  log "cleanup did not reach a clean state; failing so the dirty runner is visible"
  exit 1
fi

if [[ -n "$URL" ]]; then
  log "downloading fresh app from $URL"
  TMP=$(mktemp -d)
  trap 'rm -rf "$TMP"' EXIT
  # Install must fail loud: a run must never proceed against a stale app.
  curl -fSL --silent --show-error -o "$TMP/minds.zip" "$URL" || { log "ERROR: app download failed"; exit 1; }
  unzip -q -d "$TMP" "$TMP/minds.zip" || { log "ERROR: app unzip failed"; exit 1; }
  sudo mv "$TMP/minds.app" /Applications/minds.app || { log "ERROR: app install (mv) failed"; exit 1; }
  # xattr -dr returns non-zero when some signed-bundle internals refuse the
  # delete with "Operation not permitted"; we only care about the top-level
  # quarantine bit so Gatekeeper lets the app launch. Per-file failures
  # inside signed frameworks are harmless.
  sudo xattr -dr com.apple.quarantine /Applications/minds.app 2>/dev/null || true
  sudo xattr -d com.apple.quarantine /Applications/minds.app 2>/dev/null || true
  version=$(defaults read /Applications/minds.app/Contents/Info.plist CFBundleShortVersionString)
  build=$(defaults read /Applications/minds.app/Contents/Info.plist CFBundleVersion)
  log "installed $version ($build)"
fi

log "done"
