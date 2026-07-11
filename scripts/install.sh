#!/usr/bin/env bash
#
# mngr installer
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/imbue-ai/mngr/main/scripts/install.sh | bash
#
# What this script does:
#   1. Installs uv (https://docs.astral.sh/uv/) if not already present
#   2. Installs mngr via: uv tool install imbue-mngr
#           (pinned to the tested dependency versions via a fetched constraints file)
#   3. Runs: mngr dependencies --install interactive --scope core
#           (interactively install system deps; only warns if a *core* dep is missing)
#   4. Runs: mngr extras -i        (optional: plugins, shell completion,
#                                   Claude Code plugin, default agent type)
#   5. Runs: mngr config wizard    (optional: common user-scope config, e.g.
#                                   Claude config dir isolation)
#
# Steps 1-2 run automatically. Steps 3-5 prompt before changing anything.
# Safe to re-run: skips anything already installed or configured.
# Source: https://github.com/imbue-ai/mngr
#
set -euo pipefail

info()  { printf '\033[1m==> %s\033[0m\n' "$1"; }
warn()  { printf '\033[1mWARNING: %s\033[0m\n' "$1" >&2; }
error() { printf '\033[1mERROR: %s\033[0m\n' "$1" >&2; exit 1; }

# ── Step 1: Install uv (Python package manager) ──────────────────────────────

if command -v uv &>/dev/null; then
    info "uv is already installed ($(uv --version))"
else
    info "Installing uv..."
    # Source: https://github.com/astral-sh/uv
    if ! curl -LsSf https://astral.sh/uv/install.sh | sh; then
        error "Failed to install uv. Install manually: https://docs.astral.sh/uv/getting-started/installation/"
    fi
    # Source uv's env file so it's available without restarting the shell
    [ -f "$HOME/.local/bin/env" ] && . "$HOME/.local/bin/env"
    if ! command -v uv &>/dev/null; then
        error "uv was installed but is not on PATH. Restart your shell and re-run."
    fi
    info "uv installed ($(uv --version))"
fi

# ── Step 2: Install mngr ─────────────────────────────────────────────────────

# Pin the install to the versions this release was tested with. uv.lock and the
# `exclude-newer` cutoff only constrain resolution inside a checkout -- they are not
# consulted by `uv tool install` from PyPI -- so without this, a fresh resolution could
# pull an untested (potentially broken) dependency release. The lockfile-derived
# constraints file is served from the `release` branch, which CI fast-forwards to each
# mngr release commit, so it matches the published package. Required, not best-effort:
# a failed fetch aborts (set -euo pipefail + curl -f) rather than silently installing
# unpinned; the installer already depends on GitHub for this very script.
CONSTRAINTS_FILE="$(mktemp)"
info "Fetching dependency constraints..."
curl -fsSL "https://raw.githubusercontent.com/imbue-ai/mngr/release/libs/mngr/constraints.txt" -o "$CONSTRAINTS_FILE"

if uv tool list 2>/dev/null | grep -q '^imbue-mngr '; then
    info "Upgrading mngr..."
    # `uv tool upgrade` bumps mngr to the latest release and preserves installed plugins,
    # but does not accept constraints; re-pin the resolved dependencies with a constrained
    # `uv tool install` afterward (which also preserves plugins and keeps the upgraded version).
    uv tool upgrade imbue-mngr
    uv tool install imbue-mngr --constraints "$CONSTRAINTS_FILE"
else
    info "Installing mngr..."
    uv tool install imbue-mngr --constraints "$CONSTRAINTS_FILE"
fi

if ! command -v mngr &>/dev/null; then
    TOOL_BIN="$(uv tool dir --bin)"
    # Figure out which shell RC file to suggest
    case "${SHELL:-}" in
        */zsh)  SHELL_RC="~/.zshrc" ;;
        */bash) SHELL_RC="~/.bashrc" ;;
        *)      SHELL_RC="your shell's RC file" ;;
    esac
    error "mngr was installed to $TOOL_BIN but that directory is not on your PATH.

To fix, add this line to $SHELL_RC:

  export PATH=\"$TOOL_BIN:\$PATH\"

Then restart your shell and re-run this script."
fi

# ── Step 3: Check / install system dependencies ──────────────────────────────

# No stdin redirect needed: mngr commands read from /dev/tty directly
# when they need interactive input, so they work even when stdin is piped.
# --scope core: only treat a *core* dependency (git/tmux/jq) as a hard failure, so a
# missing optional dep (ssh/rsync/unison/claude) does not trigger the warning below.
mngr dependencies --install interactive --scope core || warn "Some dependencies could not be installed. Run 'mngr dependencies' to see what's missing."

# ── Step 4: Optional extras (plugins, shell completion, Claude Code plugin, default agent type) ──

mngr extras -i || warn "Some extras could not be installed. Run 'mngr extras' to see status."

# ── Step 5: Common user-scope configuration ──────────────────────────────────

# Walks through common config (e.g. whether to isolate the Claude config dir for
# local agents). Each step is skipped if already configured, so this is safe to
# re-run. Reads interactive input from /dev/tty, so it works even when stdin is piped.
mngr config wizard || warn "Configuration wizard did not complete. Run 'mngr config wizard' to finish."

# ── Done ──────────────────────────────────────────────────────────────────────

info "Get started with: mngr --help"
