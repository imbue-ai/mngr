#!/usr/bin/env bash
# Reset the mac-runner to a clean state for a verification run.
# Wipes Minds state and the installed .app; preserves Lima's base-image cache.
# Optional arg: a .zip URL to download and install as the fresh app.
set -euo pipefail

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

if command -v limactl >/dev/null 2>&1; then
  log "stopping and deleting Lima VM instances via limactl"
  limactl stop --all >/dev/null 2>&1 || true
  limactl delete --all >/dev/null 2>&1 || true
fi

# Belt and suspenders for the CI runner: rm any minds-e2e* dirs still
# under ~/.lima/. limactl on the GitHub Actions PATH isn't reliable and
# the `command -v` guard above silently skips when missing, leaving
# 6.4GB diffdisk + supporting files per past run pinned forever. On the
# self-hosted mac runner this accumulated to 70 zombie VMs / 446GB
# (verified 2026-06-06) before catching it. Direct rm -rf is safe --
# any minds-e2e* still present at reset time is by definition orphaned
# (the e2e's normal destroy-lifecycle handles in-flight VMs).
if [[ -d "$HOME/.lima" ]]; then
  zombie_count=$(find "$HOME/.lima" -maxdepth 1 -type d -name 'minds-e2e*' 2>/dev/null | wc -l | tr -d ' ')
  if [[ "$zombie_count" -gt 0 ]]; then
    log "wiping $zombie_count orphan minds-e2e* dir(s) under ~/.lima"
    find "$HOME/.lima" -maxdepth 1 -type d -name 'minds-e2e*' -exec rm -rf {} + 2>/dev/null || true
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
  curl -fSL --silent --show-error -o "$TMP/minds.zip" "$URL"
  unzip -q -d "$TMP" "$TMP/minds.zip"
  sudo mv "$TMP/minds.app" /Applications/minds.app
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
