#!/usr/bin/env bash
# Migrate code from mng -> mngr naming within the git checkout.
#
# Run from the repo root (or it will cd there automatically).
# This script is idempotent -- safe to run multiple times, including
# after merging main into a rename branch (to fix incoming code).
#
# For open MRs: run this script on your branch, then merge in the new main.
# After a merge with main: just run this script again to rename incoming code.
#
# What this does:
#   1. Moves orphaned files from old paths (libs/mng/*) to new paths (libs/mngr/*)
#   2. Renames .mng/ -> .mngr/ in the repo root
#   3. Renames lib directories (libs/mng -> libs/mngr, libs/mng_* -> libs/mngr_*)
#   4. Renames Python package directories within libs
#   5. Fixes symlinks with stale targets
#   6. Renames individual files with 'mng' in their basename
#   7. Replaces mng -> mngr in all tracked file contents
#   8. Adds imbue- prefix to all PyPI package names
#   9. Regenerates uv.lock
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

# ── Helper: perl script for content replacement ───────────────────
# Written to a temp file to avoid shell escaping issues with negative
# lookahead (zsh eats ! in command-line perl -e).

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
    if ($content ne $orig) {
        open my $out, '>', $file or next;
        print $out $content;
        close $out;
    }
}
PERL_SCRIPT

# ── Helper: perl script for imbue- prefix on PyPI names ──────────
# Idempotent: won't double-prefix imbue-mngr -> imbue-imbue-mngr.

PYPI_PL=$(mktemp)
trap 'rm -f "$RENAME_PL" "$PYPI_PL"' EXIT
cat > "$PYPI_PL" << 'PERL_SCRIPT'
use strict;
use warnings;
for my $file (@ARGV) {
    open my $fh, '<', $file or next;
    my $content = do { local $/; <$fh> };
    close $fh;
    my $orig = $content;
    # "mngr==" or "mngr-X==" in dep strings (but not already "imbue-mngr")
    $content =~ s/(?<!imbue-)"mngr(?=[-=])/"imbue-mngr/g;
    # "mngr" as a standalone quoted name (but not already "imbue-mngr")
    $content =~ s/(?<!imbue-)"mngr"/"imbue-mngr"/g;
    # uv sources keys at start of line: mngr = {, mngr-X = {
    $content =~ s/^mngr(?=-| = \{)/imbue-mngr/mg;
    # importlib.metadata.distribution("mngr...") lookups
    $content =~ s/distribution\("mngr/distribution("imbue-mngr/g;
    # Package metadata name comparisons
    $content =~ s/name == "mngr"/name == "imbue-mngr"/g;
    $content =~ s/name != "mngr"/name != "imbue-mngr"/g;
    $content =~ s/name="mngr"/name="imbue-mngr"/g;
    $content =~ s/startswith\("mngr-"\)/startswith("imbue-mngr-")/g;
    # PyPI URL slugs
    $content =~ s|pypi/mngr/|pypi/imbue-mngr/|g;
    # uv tool install/run (but not already imbue-mngr)
    $content =~ s/uv tool install mngr$/uv tool install imbue-mngr/mg;
    $content =~ s/uv tool run --from mngr /uv tool run --from imbue-mngr /g;
    # Fix false positives: dir_name must stay as "mngr", not "imbue-mngr"
    $content =~ s/dir_name="imbue-mngr"/dir_name="mngr"/g;
    # CLI binary entry point must stay "mngr", not "imbue-mngr"
    $content =~ s/^imbue-mngr = "imbue\.mngr\./mngr = "imbue.mngr./mg;
    if ($content ne $orig) {
        open my $out, '>', $file or next;
        print $out $content;
        close $out;
    }
}
PERL_SCRIPT

# ── 1. Move orphaned files from old paths ─────────────────────────
# After merging main, git may leave new files at old paths like
# libs/mng/imbue/mng/... with "file location" conflict suggestions.

step "1/9" "Moving orphaned files from old paths..."

moved=0
for old_root in libs/mng/imbue/mng libs/mng_*/imbue/mng_*; do
    [ -d "$old_root" ] || continue
    # Compute the new root by replacing mng with mngr
    new_root="${old_root//mng_/mngr_}"
    new_root="${new_root//\/mng\//\/mngr\/}"
    new_root="${new_root//libs\/mng\//libs\/mngr\/}"
    find "$old_root" -type f | while IFS= read -r f; do
        rel="${f#"$old_root"/}"
        newf="$new_root/$rel"
        mkdir -p "$(dirname "$newf")"
        mv "$f" "$newf"
        git add "$newf" 2>/dev/null || true
        git rm --cached "$f" 2>/dev/null || true
        moved=$((moved + 1))
    done
done
# Clean up empty old directories
for d in libs/mng libs/mng_*; do
    [ -d "$d" ] && find "$d" -depth -type d -empty -delete 2>/dev/null
    [ -d "$d" ] && rmdir "$d" 2>/dev/null || true
done
if [ "$moved" -gt 0 ]; then
    ok "Moved $moved files from old paths"
else
    ok "No orphaned files at old paths"
fi

# ── 2. Rename .mng/ directory ─────────────────────────────────────

step "2/9" "Renaming .mng/ directory..."

if [ -d ".mng" ] && [ ! -d ".mngr" ]; then
    # Fix symlinks inside .mng/ BEFORE the directory rename
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

# ── 3. Rename lib directories (top level) ─────────────────────────

step "3/9" "Renaming lib directories..."

for dir in libs/mng libs/mng_*; do
    [ -d "$dir" ] || continue
    base=$(basename "$dir")
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

# ── 4. Rename Python package directories inside libs ──────────────

step "4/9" "Renaming Python package directories..."

if [ -d "libs/mngr/imbue/mng" ] && [ ! -d "libs/mngr/imbue/mngr" ]; then
    git mv "libs/mngr/imbue/mng" "libs/mngr/imbue/mngr"
    ok "libs/mngr/imbue/mng -> libs/mngr/imbue/mngr"
elif [ -d "libs/mngr/imbue/mngr" ]; then
    skip "libs/mngr/imbue/mngr already exists"
fi

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

# ── 5. Fix symlinks with stale targets ───────────────────────────

step "5/9" "Fixing symlinks..."

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
        rm "$file"
        git rm --cached "$file" 2>/dev/null || true
        ln -s "$newtarget" "$newfile"
        git add "$newfile"
        ok "$file -> $newfile (target: $newtarget)"
        fixed_links=$((fixed_links + 1))
    elif [[ "$(basename "$file")" == *mng* && "$(basename "$file")" != *mngr* ]]; then
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

# ── 6. Rename individual files with mng in their basename ─────────

step "6/9" "Renaming files with 'mng' in their name..."

mapfile -t files_to_rename < <(
    git ls-files | while IFS= read -r file; do
        base=$(basename "$file")
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

# ── 7. Replace file contents: mng -> mngr ────────────────────────

step "7/9" "Replacing mng -> mngr in file contents..."

modified=0
while IFS= read -r -d '' file; do
    case "$file" in
        scripts/migrate_*|test_meta_ratchets.py) continue ;;
    esac
    [ -L "$file" ] && continue
    mime=$(file --brief --mime-encoding "$file" 2>/dev/null || echo "unknown")
    case "$mime" in
        binary|unknown) continue ;;
    esac
    perl "$RENAME_PL" "$file" && modified=$((modified + 1))
done < <(git ls-files -z)

ok "Processed $modified files"

# ── 8. Add imbue- prefix to all PyPI package names ────────────────

step "8/9" "Adding imbue- prefix to PyPI package names..."

# pyproject.toml files
for f in libs/*/pyproject.toml apps/*/pyproject.toml; do
    [ -f "$f" ] || continue
    perl "$PYPI_PL" "$f"
done
ok "pyproject.toml files"

# Release scripts
for f in scripts/release.py scripts/verify_publish.py scripts/utils.py; do
    [ -f "$f" ] || continue
    perl "$PYPI_PL" "$f"
done
ok "Release scripts"

# install.sh
[ -f "scripts/install.sh" ] && perl "$PYPI_PL" scripts/install.sh
ok "install.sh"

# Python source files with importlib.metadata or package name checks
for f in $(grep -rl 'distribution("mngr' libs/ apps/ 2>/dev/null || true); do
    perl "$PYPI_PL" "$f"
done
for f in libs/mngr_recursive/imbue/mngr_recursive/provisioning.py libs/mngr/imbue/mngr/uv_tool.py; do
    [ -f "$f" ] && perl "$PYPI_PL" "$f"
done
ok "Python source files"

# README docs
for f in README.md libs/mngr/README.md; do
    [ -f "$f" ] && perl "$PYPI_PL" "$f"
done
ok "Documentation"

# ── 9. Regenerate uv.lock ────────────────────────────────────────

step "9/9" "Regenerating uv.lock..."
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
