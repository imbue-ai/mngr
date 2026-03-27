#!/usr/bin/env bash
# Migrate external state from mng -> mngr naming.
#
# This handles user-local state: directories, env vars, agent data, Claude data, etc.
# Run this AFTER the code migration (migrate_code_mng_to_mngr.sh) and AFTER
# all active Claude Code sessions on this repo are closed (except the one
# running this script).
#
# Usage:
#   scripts/migrate_state_mng_to_mngr.sh [--dry-run] [checkout_dir ...]
#
# Pass one or more additional checkout directories to migrate their .mng/ dirs.
# The script automatically migrates ~/.mng and the project it lives in.

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

DRY_RUN=false
EXTRA_DIRS=()
for arg in "$@"; do
    if [ "$arg" = "--dry-run" ]; then
        DRY_RUN=true
    else
        EXTRA_DIRS+=("$arg")
    fi
done

info()  { echo -e "  ${CYAN}>${NC} $*"; }
ok()    { echo -e "  ${GREEN}OK${NC} $*"; }
warn()  { echo -e "  ${YELLOW}!!${NC} $*"; }
err()   { echo -e "  ${RED}ERROR${NC} $*"; }
step()  { echo -e "\n${BOLD}${CYAN}[$1/${TOTAL_STEPS}]${NC} ${BOLD}$2${NC}"; }
skip()  { echo -e "  ${CYAN}skip${NC} $*"; }
dry()   { echo -e "  ${CYAN}[dry-run] $*${NC}"; }

TOTAL_STEPS=10

# ── Helper: perl rename script ───────────────────────────────────
# Written to temp file to avoid zsh history expansion eating ! in lookaheads.

RENAME_PL=$(mktemp)
trap 'rm -f "$RENAME_PL"' EXIT
cat > "$RENAME_PL" << 'PERL_SCRIPT'
use strict;
use warnings;
for my $file (@ARGV) {
    open my $fh, '<', $file or next;
    my $content = do { local $/; <$fh> };
    close $fh;
    my $orig = $content;
    $content =~ s/MNG(?!R)/MNGR/g;
    $content =~ s/Mng(?!r)/Mngr/g;
    $content =~ s/mng(?!r)/mngr/g;
    # uv tool paths: uv/tools/mngr/ -> uv/tools/imbue-mngr/
    # (the basic rename produces mngr but the PyPI tool name is imbue-mngr)
    $content =~ s|uv/tools/mngr/|uv/tools/imbue-mngr/|g;
    if ($content ne $orig) {
        open my $out, '>', $file or next;
        print $out $content;
        close $out;
        print "  \033[0;32mOK\033[0m Fixed mng -> mngr in $file\n";
    }
}
PERL_SCRIPT

# Fix a file in-place with backup: rename mng -> mngr.
# Creates a .bak backup before modifying. Resolves symlinks.
fix_file() {
    local file="$1"
    [ -f "$file" ] || return 0
    # Resolve symlinks so we edit the real file
    local real_file
    real_file=$(perl -e 'use Cwd "abs_path"; print abs_path($ARGV[0])' "$file")
    if [ "$DRY_RUN" = true ]; then
        if perl -ne 'if (/mng(?!r)/i) { $f=1; last } END { exit($f ? 0 : 1) }' "$real_file" 2>/dev/null; then
            dry "would fix mng -> mngr in $file (backup: ${real_file}.bak)"
        fi
    else
        if perl -ne 'if (/mng(?!r)/i) { $f=1; last } END { exit($f ? 0 : 1) }' "$real_file" 2>/dev/null; then
            cp "$real_file" "${real_file}.bak"
            perl "$RENAME_PL" "$real_file"
        fi
    fi
}

# Fix a file in-place with backup: specific Claude data replacements.
fix_claude_json() {
    local file="$1"
    [ -f "$file" ] || return 0
    local real_file
    real_file=$(perl -e 'use Cwd "abs_path"; print abs_path($ARGV[0])' "$file")
    if [ "$DRY_RUN" = true ]; then
        local hits
        hits=$(grep -cE '\.mng|_mngCreated|_mngSourcePath' "$real_file" 2>/dev/null || true)
        if [ "$hits" -gt 0 ]; then
            dry "would fix ~$hits stale references in $file (backup: ${real_file}.bak)"
        fi
    else
        cp "$real_file" "${real_file}.bak"
        perl -pi -e 's/\.mng(?!r)/.mngr/g; s/_mngCreated/_mngrCreated/g; s/_mngSourcePath/_mngrSourcePath/g' "$real_file"
        ok "Fixed stale references in $file (backup: ${real_file}.bak)"
    fi
}

copy_missing_files() {
    local src="$1"
    local dst="$2"
    if [ "$DRY_RUN" = true ]; then
        local missing_items=()
        for item in "$src"/*; do
            [ -e "$item" ] || continue
            local base
            base=$(basename "$item")
            if [ ! -e "$dst/$base" ]; then
                missing_items+=("$base")
            fi
        done
        if [ ${#missing_items[@]} -gt 0 ]; then
            dry "would copy from $src to $dst:"
            for item in "${missing_items[@]}"; do
                dry "  $item"
            done
        else
            dry "nothing to copy from $src (all items already in $dst)"
        fi
    else
        # macOS cp returns exit code 1 when -n skips files, so we ignore it.
        cp -a -n "$src"/. "$dst"/ 2>/dev/null || true
    fi
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
        if [ "$DRY_RUN" = true ]; then
            dry "would rename $label/.mng/ -> $label/.mngr/"
        else
            mv "$mng_dir" "$mngr_dir"
            ok "Renamed $label/.mng/ -> $label/.mngr/"
        fi
        return
    fi

    # Both exist -- copy all missing files from old to new
    copy_missing_files "$mng_dir" "$mngr_dir"

    # Check if they're now identical
    if [ "$DRY_RUN" = true ]; then
        if diff -rq "$mng_dir" "$mngr_dir" > /dev/null 2>&1; then
            dry "would remove $label/.mng/ (identical to .mngr/)"
        else
            dry "$label/.mng/ and $label/.mngr/ differ -- would keep both"
        fi
    else
        if diff -rq "$mng_dir" "$mngr_dir" > /dev/null 2>&1; then
            rm -rf "$mng_dir"
            ok "Removed $label/.mng/ (identical to .mngr/)"
        else
            warn "$label/.mng/ and $label/.mngr/ differ after copying. Keeping both."
            info "Inspect diff: ${CYAN}diff -r $mng_dir $mngr_dir${NC}"
        fi
    fi
}

if [ "$DRY_RUN" = true ]; then
    echo -e "${BOLD}mng -> mngr state migration ${YELLOW}(DRY RUN)${NC}"
else
    echo -e "${BOLD}mng -> mngr state migration${NC}"
fi

# ── 1. Clean build artifacts ─────────────────────────────────────

step 1 "Cleaning build artifacts..."
if [ "$DRY_RUN" = true ]; then
    for pat in __pycache__ htmlcov .pytest_cache .test_output; do
        count=$(find "$REPO_ROOT" -type d -name "$pat" 2>/dev/null | wc -l | tr -d ' ')
        [ "$count" -gt 0 ] && dry "would remove $count $pat directories"
    done
    count=$(find "$REPO_ROOT" -name coverage.xml 2>/dev/null | wc -l | tr -d ' ')
    [ "$count" -gt 0 ] && dry "would remove $count coverage.xml files"
else
    find "$REPO_ROOT" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    find "$REPO_ROOT" -type d -name htmlcov -exec rm -rf {} + 2>/dev/null || true
    find "$REPO_ROOT" -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
    find "$REPO_ROOT" -type d -name .test_output -exec rm -rf {} + 2>/dev/null || true
    find "$REPO_ROOT" -name coverage.xml -delete 2>/dev/null || true
    ok "Cleaned __pycache__, htmlcov, .pytest_cache, .test_output, coverage.xml"
fi

# Remove empty leftover directories from the old mng names
for d in "$REPO_ROOT"/libs/mng "$REPO_ROOT"/libs/mng_*; do
    [ -d "$d" ] || continue
    if find "$d" -type f | read -r; then
        warn "$(basename "$d") is not empty and may need manual cleanup"
    elif [ "$DRY_RUN" = true ]; then
        dry "would remove empty $(basename "$d")"
    else
        rm -rf "$d"
        ok "Removed empty $(basename "$d")"
    fi
done

# ── 2. Remove mng binary ──────────────────────────────────────────

step 2 "Removing mng binary..."
mng_bin=$(command -v mng 2>/dev/null || true)
if [ -n "$mng_bin" ]; then
    if [ "$DRY_RUN" = true ]; then
        dry "would remove $mng_bin"
    else
        rm "$mng_bin"
        ok "Removed $mng_bin"
    fi
else
    ok "No mng binary found in PATH (already removed)."
fi

if uv tool list 2>/dev/null | grep -q '^mng '; then
    if [ "$DRY_RUN" = true ]; then
        dry "would uninstall mng uv tool"
    else
        uv tool uninstall mng
        ok "Uninstalled mng uv tool"
    fi
fi

echo ""

# ── 3. Migrate data directories ─────────────────────────────────

step 3 "Migrating data directories..."
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
for repo_dir in "${EXTRA_DIRS[@]+"${EXTRA_DIRS[@]}"}"; do
    migrate_dir "$repo_dir" "$repo_dir"

    if [ -d "$repo_dir/.mngr/worktrees" ]; then
        for wt in "$repo_dir/.mngr/worktrees"/*/; do
            [ -d "$wt/.mng" ] && migrate_dir "${wt%/}" "${wt%/}"
        done
    fi

    [ -f "$repo_dir/.envrc" ] && fix_file "$repo_dir/.envrc"

    echo ""
done

# ── 4. Fix tmux session environment variables ─────────────────────
# Running tmux sessions have env vars pointing to ~/.mng/. Update the
# session-level environment so new panes get the right values.
# (Existing panes keep their old env until restarted.)

step 4 "Fixing tmux session environment variables..."

if command -v tmux &>/dev/null && tmux ls &>/dev/null 2>&1; then
    tmux_fixed=0
    while IFS= read -r session; do
        session_name="${session%%:*}"
        while IFS='=' read -r var val; do
            [ -z "$var" ] && continue
            new_val=$(echo "$val" | sed 's/\.mng/.mngr/g; s/MNG_/MNGR_/g')
            if [ "$val" != "$new_val" ]; then
                if [ "$DRY_RUN" = true ]; then
                    dry "would update $var in session $session_name"
                else
                    tmux set-environment -t "$session_name" "$var" "$new_val"
                fi
                tmux_fixed=$((tmux_fixed + 1))
            fi
        done < <(tmux show-environment -t "$session_name" 2>/dev/null | grep '\.mng\|MNG_' | grep -v 'mngr\|MNGR_')
    done < <(tmux ls 2>/dev/null)
    if [ "$tmux_fixed" -gt 0 ]; then
        ok "Updated $tmux_fixed tmux environment variables"
    else
        ok "No tmux environment variables need updating"
    fi
else
    ok "No tmux sessions running"
fi

# ── 5. Fix shell configs ────────────────────────────────────────

step 5 "Fixing shell configs..."

fixed_any=false
for rc in "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.profile" "$HOME/.zshrc" "$HOME/.zprofile" "$HOME/.zshenv" "$HOME/.config/fish/config.fish" "$HOME/.envrc"; do
    [ -f "$rc" ] || continue
    if perl -ne 'if (/mng(?!r)/i) { $f=1; last } END { exit($f ? 0 : 1) }' "$rc" 2>/dev/null; then
        fix_file "$rc"
        fixed_any=true
    fi
done
if [ "$fixed_any" = false ]; then
    ok "No stale references found in shell configs."
fi

# ── 5. Check current env vars for stale references ─────────────────

step 6 "Checking environment variables..."

stale_vars=$(env | grep -i '_mng_\|^mng_\|\.mng' | grep -iv 'mngr' || true)
if [ -n "$stale_vars" ]; then
    warn "Found environment variables containing 'mng' (not 'mngr'):"
    echo "$stale_vars" | sed 's/^/  /'
    info "These will be correct after restarting your shell / agents."
    echo ""
else
    ok "No stale environment variables found."
fi

# ── 6. Fix agent data.json files ────────────────────────────────────

step 7 "Fixing agent data.json files..."

agent_fixed=0
for f in "$HOME/.mngr/agents"/*/data.json; do
    [ -f "$f" ] || continue
    if perl -ne 'if (/MNG(?!R)_|\.mng(?!r)/) { $f=1; last } END { exit($f ? 0 : 1) }' "$f" 2>/dev/null; then
        if [ "$DRY_RUN" = true ]; then
            dry "would fix stale references in $f"
        else
            perl -pi -e 's/MNG(?!R)/MNGR/g; s/\.mng(?!r)/.mngr/g' "$f"
        fi
        agent_fixed=$((agent_fixed + 1))
    fi
done
if [ "$agent_fixed" -gt 0 ]; then
    ok "Fixed $agent_fixed agent data.json files"
else
    ok "No agent data.json files need fixing."
fi

# Host data.json
host_fixed=0
for f in "$HOME/.mngr/data.json" "$HOME/.mngr/hosts"/*/data.json; do
    [ -f "$f" ] || continue
    if perl -ne 'if (/\.mng(?!r)|MNG(?!R)_/) { $f=1; last } END { exit($f ? 0 : 1) }' "$f" 2>/dev/null; then
        if [ "$DRY_RUN" = true ]; then
            dry "would fix stale references in $f"
        else
            perl -pi -e 's/MNG(?!R)/MNGR/g; s/\.mng(?!r)/.mngr/g' "$f"
            ok "Fixed stale references in $f"
        fi
        host_fixed=$((host_fixed + 1))
    fi
done
if [ "$host_fixed" -eq 0 ]; then
    ok "No host data.json files need fixing."
fi

# ── 7. Fix Claude data ─────────────────────────────────────────────

step 8 "Fixing Claude data..."

fix_claude_json "$HOME/.claude.json"

# ~/.claude/projects/: rename dirs that encode .mng/ paths
renamed_count=0
if [ -d "$HOME/.claude/projects" ]; then
    for dir in "$HOME/.claude/projects"/*mng*; do
        [ -d "$dir" ] || continue
        case "$(basename "$dir")" in
            *mngr*) continue ;;
        esac
        newdir=$(echo "$dir" | sed 's/--mng-/--mngr-/g; s/\.mng/.mngr/g')
        if [ "$dir" != "$newdir" ] && [ ! -e "$newdir" ]; then
            if [ "$DRY_RUN" = true ]; then
                dry "would rename $(basename "$dir") -> $(basename "$newdir")"
            else
                mv "$dir" "$newdir"
            fi
            renamed_count=$((renamed_count + 1))
        fi
    done
fi
if [ "$renamed_count" -gt 0 ]; then
    ok "Renamed $renamed_count Claude project dirs (.mng -> .mngr)"
else
    ok "No Claude project dirs need renaming."
fi

# Agent-internal Claude project dirs (inside each agent's plugin data).
# These encode the worktree path in the directory name.
agent_claude_renamed=0
for projects_dir in "$HOME/.mngr/agents"/*/plugin/claude/anthropic/projects; do
    [ -d "$projects_dir" ] || continue
    for dir in "$projects_dir"/*mng*; do
        [ -d "$dir" ] || continue
        case "$(basename "$dir")" in
            *mngr*) continue ;;
        esac
        newdir=$(echo "$dir" | sed 's/--mng-/--mngr-/g; s/-mng-/-mngr-/g')
        if [ "$dir" != "$newdir" ] && [ ! -e "$newdir" ]; then
            if [ "$DRY_RUN" = true ]; then
                dry "would rename agent project dir: $(basename "$dir")"
            else
                mv "$dir" "$newdir"
            fi
            agent_claude_renamed=$((agent_claude_renamed + 1))
        fi
    done
done
if [ "$agent_claude_renamed" -gt 0 ]; then
    ok "Renamed $agent_claude_renamed agent Claude project dirs"
elif [ "$DRY_RUN" = false ]; then
    ok "No agent Claude project dirs need renaming."
fi

# ── 8. Rename ~/.config/mng ────────────────────────────────────────

step 9 "Renaming ~/.config/mng..."

if [ -d "$HOME/.config/mng" ] && [ ! -d "$HOME/.config/mngr" ]; then
    if [ "$DRY_RUN" = true ]; then
        dry "would rename ~/.config/mng -> ~/.config/mngr"
    else
        mv "$HOME/.config/mng" "$HOME/.config/mngr"
        ok "Renamed ~/.config/mng -> ~/.config/mngr"
    fi
elif [ -d "$HOME/.config/mng" ]; then
    warn "Both ~/.config/mng and ~/.config/mngr exist. Leaving both."
else
    ok "No ~/.config/mng found."
fi

# ── 9. Sync packages ──────────────────────────────────────────────

step 10 "Syncing packages..."
if [ "$DRY_RUN" = true ]; then
    dry "would run uv sync --all-packages"
else
    if command -v uv &>/dev/null; then
        uv sync --all-packages
        ok "Packages synced"
    else
        skip "uv not found"
    fi
fi

echo ""
if [ "$DRY_RUN" = true ]; then
    echo -e "${YELLOW}This was a dry run. No changes were made.${NC}"
else
    echo -e "${GREEN}Done.${NC}"
    echo ""
    echo -e "${BOLD}Note on existing resources:${NC}"
    echo -e "  The resource prefix changed from ${CYAN}mng-${NC} to ${CYAN}mngr-${NC}."
    echo -e "  Existing Modal environments and tmux sessions still use the old"
    echo -e "  prefix. Until all your existing Modal agents have finished, use:"
    echo ""
    echo -e "    ${CYAN}MNGR_PREFIX=mng- mngr <command>${NC}"
    echo ""
    echo -e "  Once they're done, you can drop the prefix override and new"
    echo -e "  resources will use ${CYAN}mngr-${NC} going forward."
fi
