#!/usr/bin/env bash
# Fetch the Tailwind Play CDN JS bundle into desktop_client/static/tailwind.js,
# verifying a pinned SHA-256 so the version is genuinely locked (the URL is
# version-pinned, but the SHA check catches the case where the CDN ever
# serves different bytes for the same tag).
#
# Called two ways:
#   - automatically on `pnpm install` via the `postinstall` package.json script
#   - manually via `just minds-tailwind`
#
# Idempotent: if the file already exists AND matches the expected hash,
# exits silently. If the file exists but has the wrong hash (e.g. stale
# from an earlier version), re-downloads.

set -euo pipefail

TAILWIND_VERSION="3.4.17"
TAILWIND_URL="https://cdn.tailwindcss.com/${TAILWIND_VERSION}"
TAILWIND_SHA256="176e894661aa9cdc9a5cba6c720044cbbf7b8bd80d1c9a142a7c24b1b6c50d15"

# Script lives at apps/minds/scripts/fetch_tailwind.sh; dest is relative to
# apps/minds/ so it works regardless of where the caller invoked us from.
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
app_dir="$(cd "${script_dir}/.." && pwd)"
dest="${app_dir}/imbue/minds/desktop_client/static/tailwind.js"

sha256_of() {
  # Handle both Linux (sha256sum) and macOS (shasum -a 256).
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    echo "ERROR: neither sha256sum nor shasum available" >&2
    exit 1
  fi
}

if [ -f "${dest}" ]; then
  actual="$(sha256_of "${dest}")"
  if [ "${actual}" = "${TAILWIND_SHA256}" ]; then
    # Already fetched and verified; nothing to do.
    exit 0
  fi
  echo "tailwind.js exists but SHA-256 does not match ${TAILWIND_VERSION} -- re-downloading"
  rm -f "${dest}"
fi

mkdir -p "$(dirname "${dest}")"
echo "Fetching Tailwind Play CDN ${TAILWIND_VERSION} -> ${dest}"
curl -fsSL "${TAILWIND_URL}" -o "${dest}"

actual="$(sha256_of "${dest}")"
if [ "${actual}" != "${TAILWIND_SHA256}" ]; then
  echo "ERROR: downloaded file SHA-256 ${actual} does not match expected ${TAILWIND_SHA256}" >&2
  rm -f "${dest}"
  exit 1
fi

echo "Fetched $(wc -c < "${dest}") bytes, SHA-256 verified."
