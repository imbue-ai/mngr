#!/usr/bin/env bash
# Rebuild the vendored ttyd web client (resources/ttyd_index.html).
#
# WHY THIS EXISTS
# ---------------
# The released ttyd binary (1.7.7, the version mngr_ttyd installs) ships an
# xterm.js build with NO OSC 52 handler, so a tmux copy inside the browser
# terminal never reaches the system clipboard. ttyd's unreleased `main` branch
# adds @xterm/addon-clipboard (OSC 52 support), but even that addon's
# BrowserClipboardProvider only honors the explicit `c` selection target, while
# tmux emits OSC 52 with an EMPTY target -- so copies are still dropped.
#
# This script builds ttyd's `main` client with a one-class patch
# (ttyd_clipboard_provider.patch) that treats tmux's empty target as the system
# clipboard, producing a self-contained index.html. mngr_ttyd serves that file
# to the stock 1.7.7 binary via `ttyd -I` (the client/server wire protocol is
# unchanged between 1.7.7 and main, so the old binary + new client interoperate).
#
# USAGE
# -----
#   libs/mngr_ttyd/scripts/build_patched_ttyd_client.sh
#
# Requires: git, node, and corepack (ships with node) for the pinned yarn.
# Produces: libs/mngr_ttyd/imbue/mngr_ttyd/resources/ttyd_index.html.gz

set -euo pipefail

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_RESOURCES_DIR="$_SCRIPT_DIR/../imbue/mngr_ttyd/resources"
_PATCH="$_SCRIPT_DIR/ttyd_clipboard_provider.patch"
_BUILD_DIR="$(mktemp -d -t ttyd_client_build.XXXXXX)"
trap 'rm -rf "$_BUILD_DIR"' EXIT

# Pin to the ttyd commit the patch was authored against so the build is
# reproducible (and the patch applies cleanly).
_TTYD_REF="647d55a"

echo "Cloning ttyd into $_BUILD_DIR ..."
git clone https://github.com/tsl0922/ttyd.git "$_BUILD_DIR/ttyd"
git -C "$_BUILD_DIR/ttyd" checkout "$_TTYD_REF"

echo "Applying clipboard provider patch ..."
git -C "$_BUILD_DIR/ttyd" apply "$_PATCH"

echo "Building the html client ..."
corepack enable
(cd "$_BUILD_DIR/ttyd/html" && yarn install && yarn build)

_BUILT="$_BUILD_DIR/ttyd/html/dist/inline.html"
if ! grep -q "isSystemSelection" "$_BUILT"; then
    echo "error: built client is missing the clipboard patch" >&2
    exit 1
fi

# Ship it gzip-compressed (the plugin decompresses on install); this also keeps
# the vendored artifact under the repo's added-large-file limit.
gzip -c -9 "$_BUILT" > "$_RESOURCES_DIR/ttyd_index.html.gz"
echo "Updated $_RESOURCES_DIR/ttyd_index.html.gz"
