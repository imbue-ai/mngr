#!/usr/bin/env bash
# Migrate external state from mng -> mngr naming.
#
# This handles user-local state: directories, env vars, agent data, Claude data, etc.
# Run this AFTER the code migration (migrate_code_mng_to_mngr.sh) and AFTER
# all active Claude Code sessions on this repo are closed (except the one
# running this script).
#
# Usage:
#   scripts/migrate_state_mng_to_mngr.sh [checkout_dir ...]
#
# If no checkout_dir is given, only ~/.mng and shell configs are migrated.
# Pass one or more checkout directories to also migrate repo-local .mng/ dirs.

set -euo pipefail

# Auto-detect the repo root this script lives in
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "  ${CYAN}>${NC} $*"; }
ok()    { echo -e "  ${GREEN}OK${NC} $*"; }
warn()  { echo -e "  ${YELLOW}!!${NC} $*"; }
err()   { echo -e "  ${RED}ERROR${NC} $*"; }
step()  { echo -e "\n${BOLD}${CYAN}[$1/${TOTAL_STEPS}]${NC} ${BOLD}$2${NC}"; }
skip()  { echo -e "  ${CYAN}skip${NC} $*"; }

TOTAL_STEPS=8

copy_missing_files() {
    local src="$1"
    local dst="$2"
    # Copy everything from src to dst that doesn't already exist in dst.
    # Uses cp -a to preserve permissions/symlinks and -n to skip existing.
    # macOS cp returns exit code 1 when -n skips files, so we ignore it.
    cp -a -n "$src"/. "$dst"/ 2>/dev/null || true
}

migrate_dir() {
    local mng_dir="$1/.mng"
    local mngr_dir="$1/.mngr"
    local label="$2"

    if [ ! -d "$mng_dir" ]; then
        skip "No $label/.mng/ found (already migrated or never existed)."
        return
    fi

    if [ ! -d "$mngr_dir" ]; then
        mv "$mng_dir" "$mngr_dir"
        ok "Renamed $label/.mng/ -> $label/.mngr/"
        return
    fi

    # Both exist -- copy all missing files from old to new
    copy_missing_files "$mng_dir" "$mngr_dir"

    # Check if they're now identical
    if diff -rq "$mng_dir" "$mngr_dir" > /dev/null 2>&1; then
        rm -rf "$mng_dir"
        ok "Removed $label/.mng/ (identical to .mngr/)"
    else
        warn "$label/.mng/ and $label/.mngr/ differ after copying. Keeping both."
        info "Inspect diff: ${CYAN}diff -r $mng_dir $mngr_dir${NC}"
    fi
}

# ── 1. Remove mng binary ──────────────────────────────────────────

step 1 "Removing mng binary..."
mng_bin=$(command -v mng 2>/dev/null || true)
if [ -n "$mng_bin" ]; then
    rm "$mng_bin"
    ok "Removed $mng_bin"
else
    ok "No mng binary found in PATH (already removed)."
fi

if uv tool list 2>/dev/null | grep -q '^mng '; then
    uv tool uninstall mng
    ok "Uninstalled mng uv tool"
fi

echo ""

# ── 2. Migrate ~/.mng/ -> ~/.mngr/ ─────────────────────────────────

step 2 "Migrating data directories..."
migrate_dir "$HOME" "~"

# Migrate .mng dirs inside worktrees (these are gitignored local state)
for wt in "$HOME/.mngr/worktrees"/*/; do
    [ -d "$wt/.mng" ] && migrate_dir "${wt%/}" "${wt%/}"
done

# Migrate .mng in the project this script lives in
migrate_dir "$REPO_ROOT" "$REPO_ROOT"

# Also migrate .mng dirs inside worktrees under this checkout
if [ -d "$REPO_ROOT/.mngr/worktrees" ]; then
    for wt in "$REPO_ROOT/.mngr/worktrees"/*/; do
        [ -d "$wt/.mng" ] && migrate_dir "${wt%/}" "${wt%/}"
    done
fi

echo ""

# Migrate any additional checkout dirs passed as arguments
for repo_dir in "$@"; do
    migrate_dir "$repo_dir" "$repo_dir"

    if [ -d "$repo_dir/.mngr/worktrees" ]; then
        for wt in "$repo_dir/.mngr/worktrees"/*/; do
            [ -d "$wt/.mng" ] && migrate_dir "${wt%/}" "${wt%/}"
        done
    fi

    if [ -f "$repo_dir/.envrc" ] && grep -qi 'mng' "$repo_dir/.envrc" 2>/dev/null; then
        echo ""
        echo -e "${RED}Found 'mng' references in $repo_dir/.envrc:${NC}"
        grep -ni 'mng' "$repo_dir/.envrc" | grep -vi 'mngr' | sed 's/^/  /'
        echo -e "  To fix: ${CYAN}sed -i'.bak' -e 's/MNG/MNGR/g; s/Mng/Mngr/g; s/mng/mngr/g' $repo_dir/.envrc${NC}"
    fi

    echo ""
done

# ── 3. Check shell configs for stale references ────────────────────

step 3 "Checking shell configs for stale references..."

found_any=false
for rc in "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.profile" "$HOME/.zshrc" "$HOME/.zprofile" "$HOME/.zshenv" "$HOME/.config/fish/config.fish" "$HOME/.envrc"; do
    if [ -f "$rc" ] && grep -qi 'mng' "$rc" 2>/dev/null && grep -qiv 'mngr' "$rc" 2>/dev/null; then
        if [ "$found_any" = false ]; then
            found_any=true
        fi
        echo -e "${RED}Found 'mng' references in $rc:${NC}"
        grep -ni 'mng' "$rc" | grep -vi 'mngr' | sed 's/^/  /'
        echo -e "  To fix: ${CYAN}sed -i'.bak' -e 's/MNG_/MNGR_/g; s/\\.mng/.mngr/g; s/\\/mng/\\/mngr/g' $rc${NC}"
        echo ""
    fi
done
if [ "$found_any" = false ]; then
    ok "No stale references found in shell configs."
fi

# ── 4. Check current env vars for stale references ─────────────────

step 4 "Checking environment variables..."

stale_vars=$(env | grep -i '_mng_\|^mng_\|\.mng' | grep -iv 'mngr' || true)
if [ -n "$stale_vars" ]; then
    warn "Found environment variables containing 'mng' (not 'mngr'):"
    echo "$stale_vars" | sed 's/^/  /'
    echo ""
else
    ok "No stale environment variables found."
fi

# ── 5. Fix agent data.json files ────────────────────────────────────

step 5 "Fixing agent data.json files..."

# Agent connect commands have baked-in MNG_ env var names and .mng paths
agent_fixed=0
for f in "$HOME/.mngr/agents"/*/data.json; do
    [ -f "$f" ] || continue
    if grep -qP 'MNG(?!R)_|\.mng(?!r)' "$f" 2>/dev/null; then
        perl -pi -e 's/MNG(?!R)/MNGR/g; s/\.mng(?!r)/.mngr/g' "$f"
        agent_fixed=$((agent_fixed + 1))
    fi
done
if [ "$agent_fixed" -gt 0 ]; then
    ok "Fixed $agent_fixed agent data.json files (MNG_ env vars and .mng paths)"
else
    ok "No agent data.json files need fixing."
fi

# Host data.json has .mng worktree paths and MNG_ env vars
host_fixed=0
for f in "$HOME/.mngr/data.json" "$HOME/.mngr/hosts"/*/data.json; do
    [ -f "$f" ] || continue
    if grep -qP '\.mng(?!r)|MNG(?!R)_' "$f" 2>/dev/null; then
        perl -pi -e 's/MNG(?!R)/MNGR/g; s/\.mng(?!r)/.mngr/g' "$f"
        ok "Fixed stale references in $f"
        host_fixed=$((host_fixed + 1))
    fi
done
if [ "$host_fixed" -eq 0 ]; then
    ok "No host data.json files need fixing."
fi

# ── 6. Fix Claude data ─────────────────────────────────────────────

step 6 "Checking Claude data for stale mng references..."

real_hits=0
# ~/.claude.json: contains .mng/ paths as project keys, _mngCreated/_mngSourcePath properties
if [ -f "$HOME/.claude.json" ]; then
    real_hits=$(grep -cE '\.mng|_mngCreated|_mngSourcePath' "$HOME/.claude.json" 2>/dev/null || true)
    if [ "$real_hits" -gt 0 ]; then
        echo -e "${YELLOW}Found ~$real_hits stale 'mng' references in ~/.claude.json${NC}"
        echo -e "  (paths like .mng/, properties like _mngCreated, _mngSourcePath)"
        echo -e "  To fix: ${CYAN}sed -i'.bak' -e 's/\\.mng/.mngr/g; s/_mngCreated/_mngrCreated/g; s/_mngSourcePath/_mngrSourcePath/g' ~/.claude.json${NC}"
        echo ""
    fi
fi

# ~/.claude/projects/: rename dirs that encode .mng/ paths
renamed_count=0
if [ -d "$HOME/.claude/projects" ]; then
    for dir in "$HOME/.claude/projects"/*mng*; do
        [ -d "$dir" ] || continue
        # Only rename dirs containing mng but not mngr
        case "$(basename "$dir")" in
            *mngr*) continue ;;
        esac
        newdir=$(echo "$dir" | sed 's/--mng-/--mngr-/g; s/\.mng/.mngr/g')
        if [ "$dir" != "$newdir" ] && [ ! -e "$newdir" ]; then
            mv "$dir" "$newdir"
            renamed_count=$((renamed_count + 1))
        fi
    done
fi
if [ "$renamed_count" -gt 0 ]; then
    ok "Renamed $renamed_count Claude project dirs (.mng -> .mngr)"
fi

if [ "$real_hits" -eq 0 ] && [ "$renamed_count" -eq 0 ]; then
    ok "No stale mng references found in Claude data."
fi

# ── 7. Rename ~/.config/mng ────────────────────────────────────────

step 7 "Renaming ~/.config/mng..."

if [ -d "$HOME/.config/mng" ] && [ ! -d "$HOME/.config/mngr" ]; then
    mv "$HOME/.config/mng" "$HOME/.config/mngr"
    ok "Renamed ~/.config/mng -> ~/.config/mngr"
elif [ -d "$HOME/.config/mng" ]; then
    warn "Both ~/.config/mng and ~/.config/mngr exist. Leaving both."
else
    ok "No ~/.config/mng found."
fi

# ── 8. Clean uv cache ──────────────────────────────────────────────

step 8 "Cleaning uv cache..."
uv cache clean 2>/dev/null && ok "uv cache cleaned" || true

echo ""
echo -e "${CYAN}Note: References to the GitHub repo URL will need updating"
echo -e "separately if/when the repo is renamed.${NC}"
echo ""
echo -e "${GREEN}Done.${NC} Run ${BOLD}uv sync --all-packages${NC} in your checkout."
