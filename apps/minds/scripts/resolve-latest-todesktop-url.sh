#!/usr/bin/env bash
# Resolve the latest released arm64 macOS .zip URL for a ToDesktop app
# from its electron-updater channel feed (latest-mac.yml).
# Prints the URL to stdout, suitable for piping into mac-runner-reset.sh.
set -euo pipefail

APP_ID="${1:-26032588hqdzk}"
FEED="https://download.todesktop.com/${APP_ID}/latest-mac.yml"

fname=$(curl -fsSL --max-time 30 "$FEED" \
  | awk '/arm64-mac\.zip/{sub(/^[ -]*url: */,""); print; exit}')

if [[ -z "$fname" ]]; then
  echo "ERROR: no arm64-mac.zip entry found in $FEED" >&2
  exit 1
fi

encoded=$(printf '%s' "$fname" | python3 -c \
  'import sys,urllib.parse;print(urllib.parse.quote(sys.stdin.read().strip()))')

echo "https://download.todesktop.com/${APP_ID}/${encoded}"
