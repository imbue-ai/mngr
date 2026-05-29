#!/usr/bin/env bash
# Reset the mac-runner to a clean state for a verification run.
# Wipes Minds state and the installed .app; preserves Lima's base-image cache.
# Optional arg: a .zip URL to download and install as the fresh app.
set -euo pipefail

log() { printf '[reset] %s\n' "$*" >&2; }

log "killing Minds processes"
pids=$(pgrep -f '/Applications/minds.app/Contents/' || true)
for pid in $pids; do kill "$pid" 2>/dev/null || true; done
sleep 2
pids=$(pgrep -f '/Applications/minds.app/Contents/' || true)
for pid in $pids; do kill -9 "$pid" 2>/dev/null || true; done

if command -v limactl >/dev/null 2>&1; then
  log "stopping and deleting Lima VM instances"
  limactl stop --all >/dev/null 2>&1 || true
  limactl delete --all >/dev/null 2>&1 || true
fi

log "removing ~/.minds and /Applications/minds.app"
rm -rf "$HOME/.minds"
sudo rm -rf /Applications/minds.app

URL="${1:-}"
if [[ -n "$URL" ]]; then
  log "downloading fresh app from $URL"
  TMP=$(mktemp -d)
  trap 'rm -rf "$TMP"' EXIT
  curl -fSL --silent --show-error -o "$TMP/minds.zip" "$URL"
  unzip -q -d "$TMP" "$TMP/minds.zip"
  sudo mv "$TMP/minds.app" /Applications/minds.app
  sudo xattr -dr com.apple.quarantine /Applications/minds.app
  version=$(defaults read /Applications/minds.app/Contents/Info.plist CFBundleShortVersionString)
  build=$(defaults read /Applications/minds.app/Contents/Info.plist CFBundleVersion)
  log "installed $version ($build)"
fi

log "done"
