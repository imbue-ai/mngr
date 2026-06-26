#!/usr/bin/env bash
# Reset the mac-runner to a clean state for a verification run.
# Wipes Minds state and the installed .app; preserves Lima's base-image cache.
# Optional arg: a .zip URL to download and install as the fresh app.
#
# Deliberately NOT `set -e`: this is a best-effort cleanup of a non-ephemeral
# self-hosted runner, and every step must run even if an earlier one fails.
# Under `set -e` a single unguarded failure (e.g. a `df`/`find` pipe, a
# `defaults read`) aborts the script and SKIPS the remaining cleanup, leaking
# Lima VMs / disk -- and the workflow wraps this in `|| true`, so the skipped
# cleanup is silent. The optional install block at the end fails loud on its
# own (it must, so a run never proceeds against a stale app).
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

# Belt and suspenders for the CI runner: rm any minds-e2e* dirs still
# under ~/.lima/. limactl on the GitHub Actions PATH isn't reliable and
# the `command -v` guard above silently skips when missing, leaving
# 6.4GB diffdisk + supporting files per past run pinned forever. On the
# self-hosted mac runner this accumulated to 70 zombie VMs / 446GB
# (verified 2026-06-06) before catching it.
#
# limactl delete differs from rm -rf in two ways: (1) it stops the VM's
# hypervisor process first, (2) it deregisters from limactl's index.
# The index is rebuilt by scanning ~/.lima/ each invocation, so (2) is
# bookkeeping. The hypervisor stop matters for a LIVE VM. Preserve that
# semantic without depending on limactl: for each minds-e2e* dir, check
# ha.pid -- if the hypervisor is still alive, SIGTERM (then SIGKILL on
# 2s grace) before rm -rf so we never orphan a running VM.
if [[ -d "$HOME/.lima" ]]; then
  zombie_count=$(find "$HOME/.lima" -maxdepth 1 -type d -name 'minds-e2e*' 2>/dev/null | wc -l | tr -d ' ')
  if [[ "$zombie_count" -gt 0 ]]; then
    log "cleaning $zombie_count minds-e2e* dir(s) under ~/.lima"
    for vm_dir in "$HOME/.lima"/minds-e2e*; do
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
