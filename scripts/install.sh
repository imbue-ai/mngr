#!/usr/bin/env bash
#
# mngr installer
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/imbue-ai/mngr/main/scripts/install.sh | bash
#
# This script:
#   1. Checks for prerequisites (curl, ssh)
#   2. Prompts to install system dependencies:
#      - Core: uv, git, tmux, jq
#      - Optional: claude (agent type), rsync (push/pull), unison (pair)
#   3. Installs mngr via uv tool install
#   4. Offers to enable shell completion
#
set -euo pipefail

BOLD='\033[1m'
DIM='\033[2m'
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

# ── Detect OS ──────────────────────────────────────────────────────────────────

detect_os() {
    case "$(uname -s)" in
        Darwin) echo "macos" ;;
        Linux)  echo "linux" ;;
        *)      error "Unsupported operating system: $(uname -s). mngr supports macOS and Linux." ;;
    esac
}

OS="$(detect_os)"

# ── Check prerequisites ───────────────────────────────────────────────────────

for prereq in curl ssh; do
    if ! command -v "$prereq" &>/dev/null; then
        error "$prereq is required but not found. Please install it and re-run this script."
    fi
done

# ── Install system dependencies ────────────────────────────────────────────────

# uv and claude have their own installers (not brew/apt), so we track them
# separately from BREW_APT_* lists. They show up in the missing deps prompt
# but are installed via their own mechanisms.
CORE_DEPS=(uv git tmux jq)
BREW_APT_CORE_DEPS=(git tmux jq)
OPTIONAL_DEPS=(claude rsync unison)
BREW_APT_OPTIONAL_DEPS=(rsync unison)
ALL_DEPS=("${CORE_DEPS[@]}" "${OPTIONAL_DEPS[@]}")

find_missing() {
    local deps=("$@")
    local missing=()
    for dep in "${deps[@]}"; do
        if ! command -v "$dep" &>/dev/null; then
            missing+=("$dep")
        fi
    done
    # ${missing[*]+...} avoids "unbound variable" on bash 3.2 (macOS) with set -u
    echo "${missing[*]+${missing[*]}}"
}

install_deps() {
    local deps=("$@")
    if [ ${#deps[@]} -eq 0 ]; then
        return
    fi
    if [ "$OS" = "macos" ]; then
        if ! command -v brew &>/dev/null; then
            error "Missing dependencies: ${deps[*]}. Install them manually, or install Homebrew (https://brew.sh) and re-run this script."
        fi
        info "Installing system dependencies: ${deps[*]}"
        brew install "${deps[@]}"
    elif [ "$OS" = "linux" ]; then
        if ! command -v apt-get &>/dev/null; then
            error "apt-get not found. On non-Debian systems, manually install: ${deps[*]}"
        fi
        info "Installing system dependencies: ${deps[*]}"
        sudo apt-get update -qq
        sudo apt-get install -y -qq "${deps[@]}"
    fi
}

install_claude() {
    if command -v claude &>/dev/null; then
        return
    fi
    info "Installing Claude Code..."
    if ! curl -fsSL https://claude.ai/install.sh | bash; then
        warn "Failed to install Claude Code. Install it manually: https://docs.anthropic.com/en/docs/claude-code/getting-started"
    fi
}

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

info "Detected OS: ${OS}"

# macOS ships /bin/bash 3.2 which lacks features mngr scripts need.
# `command -v bash` always succeeds, so find_missing won't detect this. We check
# the version explicitly and force-add bash to the missing arrays if too old.
_NEED_MODERN_BASH=false
_PATH_BASH_VER="$(bash -c 'echo ${BASH_VERSINFO[0]}' 2>/dev/null || echo 0)"
if [ "$_PATH_BASH_VER" -lt 4 ] 2>/dev/null; then
    _NEED_MODERN_BASH=true
fi

SHOULD_INSTALL_DEPS=true

# shellcheck disable=SC2207
missing_all=($(find_missing "${ALL_DEPS[@]}"))
if [ "$_NEED_MODERN_BASH" = true ]; then
    missing_all+=("bash(4+)")
fi

if [ ${#missing_all[@]} -eq 0 ]; then
    info "All system dependencies already installed"
else
    printf "\n"
    printf "mngr needs these system dependencies: ${BOLD}${missing_all[*]}${RESET}\n"
    printf "  claude, rsync, and unison are optional (needed for the claude agent type, push/pull, and pair).\n"
    printf "\n"
    printf "  [a] Install all (%s)\n" "${missing_all[*]}"
    # shellcheck disable=SC2207
    missing_core=($(find_missing "${CORE_DEPS[@]}"))
    if [ "$_NEED_MODERN_BASH" = true ]; then
        missing_core+=("bash(4+)")
    fi
    if [ ${#missing_core[@]} -gt 0 ]; then
        printf "  [c] Install core only (%s)\n" "${missing_core[*]}"
    fi
    printf "  [n] Skip -- I'll install them myself\n"
    printf "\n"
    printf "Choice [a/c/n]: "
    # Read from /dev/tty since stdin may be piped
    read -r choice < /dev/tty

    # Filter out uv and claude from brew/apt install lists (they have their own installers)
    # shellcheck disable=SC2207
    brew_apt_missing_all=($(find_missing "${BREW_APT_CORE_DEPS[@]}" "${BREW_APT_OPTIONAL_DEPS[@]}"))
    # shellcheck disable=SC2207
    brew_apt_missing_core=($(find_missing "${BREW_APT_CORE_DEPS[@]}"))
    # Force-add bash if the PATH-resolved version is too old (find_missing can't detect this)
    if [ "$_NEED_MODERN_BASH" = true ]; then
        brew_apt_missing_all+=("bash")
        brew_apt_missing_core+=("bash")
    fi

    case "$choice" in
        a|A|y|Y|"")
            install_uv
            install_claude
            if [ ${#brew_apt_missing_all[@]} -gt 0 ]; then
                install_deps "${brew_apt_missing_all[@]}"
            fi
            ;;
        c|C)
            install_uv
            if [ ${#brew_apt_missing_core[@]} -gt 0 ]; then
                install_deps "${brew_apt_missing_core[@]}"
            else
                info "Core dependencies already installed"
            fi
            ;;
        n|N)
            SHOULD_INSTALL_DEPS=false
            info "Skipping system dependency installation"
            ;;
        *)
            SHOULD_INSTALL_DEPS=false
            info "Skipping system dependency installation"
            ;;
    esac
fi

# ── Verify bash 4+ is on PATH (post-install) ─────────────────────────────────
# Re-check after deps were installed. Collect warnings for printing at the end
# (see DEFERRED_WARNINGS below).

DEFERRED_WARNINGS=""

if [ "$_NEED_MODERN_BASH" = true ]; then
    _POST_BASH_VER="$(bash -c 'echo ${BASH_VERSINFO[0]}' 2>/dev/null || echo 0)"
    if [ "$_POST_BASH_VER" -lt 4 ] 2>/dev/null; then
        if [ "$OS" = "macos" ]; then
            DEFERRED_WARNINGS="${DEFERRED_WARNINGS}PATH-resolved bash is still version $_POST_BASH_VER after install.\nEnsure /opt/homebrew/bin (Apple Silicon) or /usr/local/bin (Intel) is before /bin in your PATH.\n"
        else
            DEFERRED_WARNINGS="${DEFERRED_WARNINGS}PATH-resolved bash is still version $_POST_BASH_VER after install.\nEnsure the newly installed bash is before the old one in your PATH.\n"
        fi
    fi
fi

# ── Verify uv is available ─────────────────────────────────────────────────────

if ! command -v uv &>/dev/null; then
    if [ "$SHOULD_INSTALL_DEPS" = true ]; then
        # All deps were already installed but uv was somehow missed
        install_uv
    else
        error "uv is required but not installed. Install it with 'curl -LsSf https://astral.sh/uv/install.sh | sh' and re-run this script."
    fi
fi

# ── Install mngr ──────────────────────────────────────────────────────────────

info "Installing mngr..."
uv tool install imbue-mngr

MNGR_BIN="$(uv tool dir --bin)/mngr"

if ! command -v mngr &>/dev/null; then
    DEFERRED_WARNINGS="${DEFERRED_WARNINGS}mngr was installed but is not on PATH.\nYou may need to add ~/.local/bin to your PATH:\n  export PATH=\"\$HOME/.local/bin:\$PATH\"\n"
fi

# ── Plugin install wizard ─────────────────────────────────────────────────────

"$MNGR_BIN" plugin install-wizard || warn "Plugin install wizard failed. You can run 'mngr plugin install-wizard' later."

# ── Shell completion ───────────────────────────────────────────────────────────

if [ "$OS" = "macos" ]; then
    SHELL_RC="$HOME/.zshrc"
    SHELL_TYPE="zsh"
else
    SHELL_RC="$HOME/.bashrc"
    SHELL_TYPE="bash"
fi

ALREADY_CONFIGURED=false
if grep -qF '_mngr_complete' "$SHELL_RC" 2>/dev/null; then
    ALREADY_CONFIGURED=true
fi

if [ "$ALREADY_CONFIGURED" = true ]; then
    info "Shell completion already configured in $SHELL_RC"
else
    printf "\n"
    printf "Enable shell completion? This will add a line to ${BOLD}%s${RESET}\n" "$SHELL_RC"
    printf "  [y] Yes\n"
    printf "  [n] No\n"
    printf "\n"
    printf "Choice [y/n]: "
    read -r completion_choice < /dev/tty

    case "$completion_choice" in
        y|Y|"")
            COMPLETION_SCRIPT="$(uv tool run --from imbue-mngr python3 -m imbue.mngr.cli.complete --script "$SHELL_TYPE" 2>/dev/null)"
            if [ -n "$COMPLETION_SCRIPT" ]; then
                printf "\n%s\n" "$COMPLETION_SCRIPT" >> "$SHELL_RC"
                info "Shell completion enabled in $SHELL_RC"
            else
                warn "Could not generate completion script."
                warn "You can set it up manually later -- see: https://github.com/imbue-ai/mngr#shell-completion"
            fi
            ;;
        *)
            info "Skipping shell completion"
            ;;
    esac
fi

# ── Claude Code plugins ───────────────────────────────────────────────────────

if command -v claude &>/dev/null; then
    printf "\n"
    printf "mngr provides a Claude Code plugin for automated code review enforcement.\n"
    printf "To install it, run:\n"
    printf "\n"
    printf "  ${BOLD}claude plugin marketplace add imbue-ai/code-guardian && claude plugin install imbue-code-guardian@imbue-code-guardian${RESET}\n"
    printf "\n"
fi

info "Get started with: mngr --help"

# IMPORTANT: Instructions that require user action after installation (e.g.
# adding something to PATH) must always be printed last, so they remain visible
# when the script exits.
if [ -n "$DEFERRED_WARNINGS" ]; then
    printf "\n"
    printf "%b" "$DEFERRED_WARNINGS" | while IFS= read -r line; do
        warn "$line"
    done
fi
