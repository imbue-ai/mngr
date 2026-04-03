#!/usr/bin/env bash
#
# mngr installer
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/imbue-ai/mngr/main/scripts/install.sh | bash
#
# This script:
#   1. Installs uv (Python package manager) if not present
#   2. Installs mngr via uv tool install
#   3. Runs mngr dependencies to check/install system dependencies
#   4. Runs mngr extras for optional setup (plugins, shell completion, etc.)
#
set -euo pipefail

BOLD='\033[1m'
RESET='\033[0m'

info() {
    printf "${BOLD}==> %s${RESET}\n" "$1"
}

warn() {
    printf "${BOLD}WARNING: %s${RESET}\n" "$1" >&2
}

error() {
    printf "${BOLD}ERROR: %s${RESET}\n" "$1" >&2
    exit 1
}

# ── Install uv ───────────────────────────────────────────────────────────────

install_uv() {
    if command -v uv &>/dev/null; then
        info "uv is already installed ($(uv --version))"
        return
    fi
    info "Installing uv..."
    if ! curl -LsSf https://astral.sh/uv/install.sh | sh; then
        error "Failed to install uv. Install it manually: https://docs.astral.sh/uv/getting-started/installation/"
    fi

    # The uv installer creates an env file that adds its bin dir to PATH.
    # Source it so uv is available in this script without restarting the shell.
    [ -f "$HOME/.local/bin/env" ] && . "$HOME/.local/bin/env"

    if ! command -v uv &>/dev/null; then
        error "uv was installed but is not on PATH. Restart your shell and run this script again."
    fi

    info "uv installed ($(uv --version))"
}

# ── Main ──────────────────────────────────────────────────────────────────────

install_uv

# ── Install mngr ──────────────────────────────────────────────────────────────

info "Installing mngr..."
uv tool install imbue-mngr

MNGR_BIN="$(uv tool dir --bin)/mngr"

DEFERRED_WARNINGS=""
if ! command -v mngr &>/dev/null; then
    DEFERRED_WARNINGS="${DEFERRED_WARNINGS}mngr was installed but is not on PATH.\nYou may need to add ~/.local/bin to your PATH:\n  export PATH=\"\$HOME/.local/bin:\$PATH\"\n"
fi

# ── Check system dependencies ─────────────────────────────────────────────────

# Redirect stdin from /dev/tty for interactive commands. When this script is
# invoked via `curl ... | bash`, stdin is the pipe from curl, not the terminal.
# Interactive prompts and the plugin install wizard's urwid TUI need a real TTY.
"$MNGR_BIN" dependencies -i < /dev/tty || warn "Some dependencies could not be installed. Run 'mngr dependencies' to see what's missing."

# ── Optional extras (plugins, shell completion, Claude Code plugin) ───────────

"$MNGR_BIN" extras -i < /dev/tty || warn "Some extras could not be installed. Run 'mngr extras' to see status."

# ── Done ──────────────────────────────────────────────────────────────────────

info "Get started with: mngr --help"

if [ -n "$DEFERRED_WARNINGS" ]; then
    printf "\n"
    printf "%b" "$DEFERRED_WARNINGS" | while IFS= read -r line; do
        warn "$line"
    done
fi
