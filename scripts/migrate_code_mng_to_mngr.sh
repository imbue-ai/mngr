#!/usr/bin/env bash
# Migrate code from mng -> mngr naming within the git checkout.
#
# Run from the repo root (or it will cd there automatically).
# This script is idempotent -- safe to run multiple times.
#
# For open MRs: run this script on your branch, then merge in the new main.
# After running, review changes and commit.
#
# What this does:
#   1. Renames .mng/ -> .mngr/ in the repo root
#   2. Renames lib directories (libs/mng -> libs/mngr, libs/mng_* -> libs/mngr_*)
#   3. Renames Python package directories within libs
#   4. Fixes symlinks with stale targets (before file renames, since dir renames break them)
#   5. Renames individual files with 'mng' in their basename
#   6. Replaces mng -> mngr in all tracked file contents
#   7. Fixes the main package PyPI name to imbue-mngr (not just mngr)
#
# What this does NOT do:
#   - Migrate external state (~/.mng, env vars, etc.) -- see migrate_state_mng_to_mngr.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

step() { echo -e "\n${BOLD}[$1] $2${NC}"; }
ok()   { echo -e "  ${GREEN}$*${NC}"; }
skip() { echo -e "  ${YELLOW}skip: $*${NC}"; }

echo -e "${BOLD}mng -> mngr code migration${NC}"

# ── 1. Rename .mng/ directory ─────────────────────────────────────

step "1/7" "Renaming .mng/ directory..."

if [ -d ".mng" ] && [ ! -d ".mngr" ]; then
    # Fix symlinks inside .mng/ BEFORE the directory rename, so git mv
    # preserves them correctly (otherwise they become broken and git
    # resolves them to regular files).
    for file in .mng/*; do
        [ -L "$file" ] || continue
        target=$(readlink "$file")
        if [[ "$target" == *mng* && "$target" != *mngr* ]]; then
            newtarget="${target//mng/mngr}"
            rm "$file"
            ln -s "$newtarget" "$file"
            git add "$file"
            ok "fixed symlink $file -> $newtarget"
        fi
    done
    git mv ".mng" ".mngr"
    ok ".mng -> .mngr"
elif [ -d ".mngr" ]; then
    skip ".mngr already exists"
else
    skip "no .mng directory found"
fi

# ── 2. Rename lib directories (top level) ─────────────────────────

step "2/7" "Renaming lib directories..."

for dir in libs/mng libs/mng_*; do
    [ -d "$dir" ] || continue
    base=$(basename "$dir")
    # Only process dirs starting with mng (not mngr)
    case "$base" in
        mng|mng_*) ;;
        *) continue ;;
    esac
    newbase="${base/mng/mngr}"
    newdir="libs/$newbase"
    if [ ! -d "$newdir" ]; then
        git mv "$dir" "$newdir"
        ok "$dir -> $newdir"
    else
        skip "$newdir already exists"
    fi
done

# ── 3. Rename Python package directories inside libs ──────────────

step "3/7" "Renaming Python package directories..."

# Main package: libs/mngr/imbue/mng -> libs/mngr/imbue/mngr
if [ -d "libs/mngr/imbue/mng" ] && [ ! -d "libs/mngr/imbue/mngr" ]; then
    git mv "libs/mngr/imbue/mng" "libs/mngr/imbue/mngr"
    ok "libs/mngr/imbue/mng -> libs/mngr/imbue/mngr"
elif [ -d "libs/mngr/imbue/mngr" ]; then
    skip "libs/mngr/imbue/mngr already exists"
fi

# Plugins: libs/mngr_X/imbue/mng_X -> libs/mngr_X/imbue/mngr_X
for dir in libs/mngr_*/imbue/mng_*; do
    [ -d "$dir" ] || continue
    base=$(basename "$dir")
    parent=$(dirname "$dir")
    newbase="${base/mng_/mngr_}"
    newdir="$parent/$newbase"
    if [ "$dir" != "$newdir" ] && [ ! -d "$newdir" ]; then
        git mv "$dir" "$newdir"
        ok "$dir -> $newdir"
    fi
done

# ── 4. Fix symlinks with stale targets ───────────────────────────
#
# Directory renames (steps 1-3) break symlinks whose targets referenced
# old paths. Fix these BEFORE file renames (step 5), because broken
# symlinks cause -f tests to fail and skip the rename.

step "4/7" "Fixing symlinks..."

# Collect symlinks first to avoid modifying the list while iterating
mapfile -t symlinks < <(git ls-files | while IFS= read -r file; do
    [ -L "$file" ] && echo "$file"
done)

fixed_links=0
for file in "${symlinks[@]+"${symlinks[@]}"}"; do
    target=$(readlink "$file")
    if [[ "$target" == *mng* && "$target" != *mngr* ]]; then
        newtarget="${target//mng/mngr}"
        newbase=$(basename "$file")
        newbase="${newbase//mng/mngr}"
        newfile="$(dirname "$file")/$newbase"
        # Remove old symlink
        rm "$file"
        git rm --cached "$file" 2>/dev/null || true
        # Create new symlink (possibly with renamed basename)
        ln -s "$newtarget" "$newfile"
        git add "$newfile"
        ok "$file -> $newfile (target: $newtarget)"
        fixed_links=$((fixed_links + 1))
    elif [[ "$(basename "$file")" == *mng* && "$(basename "$file")" != *mngr* ]]; then
        # Symlink target is fine but filename needs renaming
        newbase=$(basename "$file")
        newbase="${newbase//mng/mngr}"
        newfile="$(dirname "$file")/$newbase"
        if [ "$file" != "$newfile" ]; then
            git mv "$file" "$newfile"
            ok "$file -> $newfile"
            fixed_links=$((fixed_links + 1))
        fi
    fi
done

if [ "$fixed_links" -eq 0 ]; then
    ok "No symlinks needed fixing"
fi

# ── 5. Rename individual files with mng in their basename ─────────

step "5/7" "Renaming files with 'mng' in their name..."

# Collect files first (can't rename while iterating)
mapfile -t files_to_rename < <(
    git ls-files | while IFS= read -r file; do
        base=$(basename "$file")
        # Only process basenames containing mng but not mngr
        # Skip migration scripts (they intentionally contain mng)
        # Skip symlinks (already handled in step 4)
        if [[ "$base" == *mng* && "$base" != *mngr* && "$file" != scripts/migrate_* ]] && [ ! -L "$file" ]; then
            echo "$file"
        fi
    done
)

for file in "${files_to_rename[@]+"${files_to_rename[@]}"}"; do
    [ -e "$file" ] || continue
    base=$(basename "$file")
    dir=$(dirname "$file")
    newbase="${base//mng/mngr}"
    newfile="$dir/$newbase"
    if [ "$file" != "$newfile" ] && [ ! -e "$newfile" ]; then
        git mv "$file" "$newfile"
        ok "$file -> $newfile"
    fi
done

# ── 6. Replace file contents: mng -> mngr ────────────────────────

step "6/7" "Replacing mng -> mngr in file contents..."

modified=0
while IFS= read -r -d '' file; do
    # Skip migration scripts (they contain mng patterns intentionally)
    case "$file" in
        scripts/migrate_*) continue ;;
    esac
    # Skip symlinks (modify the target, not the link)
    [ -L "$file" ] && continue
    # Skip binary files
    mime=$(file --brief --mime-encoding "$file" 2>/dev/null || echo "unknown")
    case "$mime" in
        binary|unknown) continue ;;
    esac
    # Check if file contains mng not followed by r (any case)
    if perl -ne 'exit 0 if /mng(?!r)/i' "$file" 2>/dev/null; then
        perl -pi -e '
            s/MNG(?!R)/MNGR/g;
            s/Mng(?!r)/Mngr/g;
            s/mng(?!r)/mngr/g;
        ' "$file"
        modified=$((modified + 1))
    fi
done < <(git ls-files -z)

ok "Modified $modified files"

# ── 7. Fix all PyPI names to use imbue- prefix ────────────────────
#
# After the general mng->mngr rename, PyPI names are "mngr", "mngr-claude", etc.
# They should all be "imbue-mngr", "imbue-mngr-claude", etc.
# The CLI binary stays "mngr". Directory/module names stay "mngr"/"mngr_*".

step "7/7" "Adding imbue- prefix to all PyPI package names..."

# Main package pyproject.toml: name field
if [ -f "libs/mngr/pyproject.toml" ]; then
    perl -pi -e 's/^name = "mngr"/name = "imbue-mngr"/' libs/mngr/pyproject.toml
    ok "libs/mngr/pyproject.toml: name = imbue-mngr"
fi

# All pyproject.toml: "mngr" and "mngr-*" in quoted strings (names, deps)
# This handles both name = "mngr-claude" and "mngr-claude==0.1.0" and "mngr==0.1.8"
for f in libs/*/pyproject.toml apps/*/pyproject.toml; do
    [ -f "$f" ] || continue
    perl -pi -e 's/"mngr(?=[-=])/"imbue-mngr/g; s/"mngr"/"imbue-mngr"/g' "$f"
    # Fix uv sources keys at start of line: mngr = {, mngr-claude = {
    perl -pi -e 's/^mngr(?=-| = )/imbue-mngr/' "$f"
done
ok "pyproject.toml: all PyPI names prefixed"

# Fix [project.scripts]: the CLI binary should stay "mngr", not "imbue-mngr"
if [ -f "libs/mngr/pyproject.toml" ]; then
    perl -pi -e 's/^imbue-mngr = "imbue\.mngr\./mngr = "imbue.mngr./' libs/mngr/pyproject.toml
    ok "libs/mngr/pyproject.toml: restored CLI name mngr"
fi

# Release scripts: replace "mngr" and "mngr-*" PyPI names with imbue- prefix
for f in scripts/release.py scripts/verify_publish.py scripts/utils.py; do
    [ -f "$f" ] || continue
    perl -pi -e 's/"mngr(?=-)"/"imbue-mngr/g; s/"mngr"/"imbue-mngr"/g' "$f"
done
ok "Release scripts: PyPI names prefixed"

# Fix false positive: dir_name must be the actual directory name, not the PyPI name
if [ -f "scripts/utils.py" ]; then
    perl -pi -e 's/dir_name="imbue-mngr"/dir_name="mngr"/' scripts/utils.py
    ok "scripts/utils.py: restored dir_name=\"mngr\""
fi

# Python source files: fix importlib.metadata lookups
for f in $(grep -rl 'distribution("mngr' libs/ apps/ 2>/dev/null); do
    perl -pi -e 's/distribution\("mngr/distribution("imbue-mngr/g' "$f"
    ok "$f: distribution lookup"
done

# Package metadata name checks in provisioning.py and uv_tool.py
for f in libs/mngr_recursive/imbue/mngr_recursive/provisioning.py libs/mngr/imbue/mngr/uv_tool.py; do
    [ -f "$f" ] || continue
    perl -pi -e '
        s/name == "mngr"/name == "imbue-mngr"/g;
        s/name != "mngr"/name != "imbue-mngr"/g;
        s/name="mngr"/name="imbue-mngr"/g;
        s/startswith\("mngr-"\)/startswith("imbue-mngr-")/g;
    ' "$f"
    ok "$f: package name references"
done

# release.py: PyPI URL slug
if [ -f "scripts/release.py" ]; then
    perl -pi -e 's|pypi/mngr/|pypi/imbue-mngr/|g' scripts/release.py
    ok "scripts/release.py: PyPI URL"
fi

# install.sh: uv tool install/run references
if [ -f "scripts/install.sh" ]; then
    perl -pi -e 's/uv tool install mngr$/uv tool install imbue-mngr/' scripts/install.sh
    perl -pi -e 's/uv tool run --from mngr /uv tool run --from imbue-mngr /' scripts/install.sh
    ok "scripts/install.sh: tool references"
fi

# ── Regenerate uv.lock ────────────────────────────────────────────

echo -e "\n${BOLD}Regenerating uv.lock...${NC}"
if command -v uv &>/dev/null; then
    uv lock
    ok "uv.lock regenerated"
else
    skip "uv not found, skipping lock regeneration"
fi

echo -e "\n${GREEN}${BOLD}Code migration complete.${NC}"
echo -e "Next steps:"
echo -e "  1. Review changes: git diff --stat"
echo -e "  2. Commit: git add -A && git commit -m 'Rename mng -> mngr'"
