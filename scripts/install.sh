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
#   5. Suggests a `mngr config set commands.create.type ...` command to set
#      the default agent type for `mngr create` (does NOT write it for you)
#
# Steps 1-2 run automatically. Steps 3-4 prompt before installing anything.
# Step 5 only prints a suggested command -- nothing is written without your
# action. Safe to re-run: skips anything already installed or configured.
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

if uv tool list 2>/dev/null | grep -q '^imbue-mngr '; then
    info "Upgrading mngr..."
    uv tool upgrade imbue-mngr
else
    info "Installing mngr..."
    uv tool install imbue-mngr
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
mngr dependencies -i || warn "Some dependencies could not be installed. Run 'mngr dependencies' to see what's missing."

# ── Step 4: Optional extras (plugins, shell completion, Claude Code plugin) ──

mngr extras -i || warn "Some extras could not be installed. Run 'mngr extras' to see status."

# ── Step 5: Default agent type for `mngr create` ─────────────────────────────

# `mngr create` requires an agent type (via positional, --type, or
# [commands.create] type in user settings). If the user has not set one
# yet, suggest the right `mngr config set` command and list the
# installed agent-type plugins they can pick from.
if ! mngr config get commands.create.type --scope user >/dev/null 2>&1; then
    info "To set a default agent type for 'mngr create', run:"
    info "    mngr config set commands.create.type <name> --scope user"
    # Print the "Available agent types:" header only on success so that the
    # failure path doesn't leave a dangling header above the warning.
    if AGENT_TYPES_OUTPUT="$(mngr plugin list --kind agent-type --active --format '{name}' 2>/dev/null)"; then
        info "Available agent types:"
        printf '%s\n' "$AGENT_TYPES_OUTPUT" | sed 's/^/    /'
    else
        warn "Could not list agent-type plugins. Run 'mngr plugin list --kind agent-type' to investigate."
    fi
fi

# ── Done ──────────────────────────────────────────────────────────────────────

info "Get started with: mngr --help"
