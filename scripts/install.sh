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
#   3. Runs: mngr dependencies -i  (interactively install system deps)
#   4. Runs: mngr extras -i        (optional: plugins, shell completion, etc.)
#
# Steps 1-2 run automatically. Steps 3-4 prompt before installing anything.
# Safe to re-run: skips anything already installed.
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

info "Installing mngr..."
uv tool install imbue-mngr

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

# When invoked via `curl | bash`, stdin is the pipe, not the terminal.
# Redirect from /dev/tty so interactive prompts work.
if [ -e /dev/tty ]; then
    mngr dependencies -i < /dev/tty || warn "Some dependencies could not be installed. Run 'mngr dependencies' to see what's missing."
else
    mngr dependencies || warn "Run 'mngr dependencies -i' to install missing dependencies."
fi

# ── Step 4: Optional extras (plugins, shell completion, Claude Code plugin) ──

if [ -e /dev/tty ]; then
    mngr extras -i < /dev/tty || warn "Some extras could not be installed. Run 'mngr extras' to see status."
else
    mngr extras || true
fi

# ── Done ──────────────────────────────────────────────────────────────────────

info "Get started with: mngr --help"
