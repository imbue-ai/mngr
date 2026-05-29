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
  log "stopping and deleting Lima VM instances"
  limactl stop --all >/dev/null 2>&1 || true
  limactl delete --all >/dev/null 2>&1 || true
fi

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
