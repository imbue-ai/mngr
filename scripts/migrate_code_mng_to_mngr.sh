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
# Usage:
#   scripts/migrate_code_mng_to_mngr.sh              # run migration
#   scripts/migrate_code_mng_to_mngr.sh --dry-run    # preview without changes
#
# What this does:
#   0. Cleans build artifacts (__pycache__, htmlcov, etc.)
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

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
fi

step() { echo -e "\n${BOLD}[$1] $2${NC}"; }
ok()   { echo -e "  ${GREEN}$*${NC}"; }
skip() { echo -e "  ${YELLOW}skip: $*${NC}"; }
dry()  { echo -e "  ${CYAN}[dry-run] $*${NC}"; }

if [ "$DRY_RUN" = true ]; then
    echo -e "${BOLD}mng -> mngr code migration ${YELLOW}(DRY RUN)${NC}"
else
    echo -e "${BOLD}mng -> mngr code migration${NC}"
fi

# ── 0. Clean build artifacts ─────────────────────────────────────
# Stale .pyc files with old module paths prevent old directories
# from being removed and cause import errors.

echo -e "\n${BOLD}Cleaning build artifacts...${NC}"
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


# ── Helper: perl script for content replacement ───────────────────
# Written to a temp file to avoid shell escaping issues with negative
# lookahead (zsh eats ! in command-line perl -e).
# Respects MIGRATE_DRY_RUN env var: prints changed files without writing.

RENAME_PL=$(mktemp)
trap 'rm -f "$RENAME_PL"' EXIT
cat > "$RENAME_PL" << 'PERL_SCRIPT'
use strict;
use warnings;
my $dry_run = $ENV{MIGRATE_DRY_RUN} // 0;
my $count = 0;
for my $file (@ARGV) {
    open my $fh, '<', $file or next;
    my $content = do { local $/; <$fh> };
    close $fh;
    my $orig = $content;
    $content =~ s/MNG(?!R)/MNGR/g;
    $content =~ s/Mng(?!r)/Mngr/g;
    $content =~ s/mng(?!r)/mngr/g;
    if ($content ne $orig) {
        $count++;
        unless ($dry_run) {
            open my $out, '>', $file or next;
            print $out $content;
            close $out;
        }
    }
}
if ($count > 0) {
    my $prefix = $dry_run ? "  \033[0;36m[dry-run] would modify" : "  \033[0;32mOK\033[0m Modified";
    print "${prefix} $count files\033[0m\n";
}
PERL_SCRIPT

# ── Helper: perl script for imbue- prefix on PyPI names ──────────
# Idempotent: won't double-prefix imbue-mngr -> imbue-imbue-mngr.
# Respects MIGRATE_DRY_RUN env var.

PYPI_PL=$(mktemp)
trap 'rm -f "$RENAME_PL" "$PYPI_PL"' EXIT
cat > "$PYPI_PL" << 'PERL_SCRIPT'
use strict;
use warnings;
my $dry_run = $ENV{MIGRATE_DRY_RUN} // 0;
my $count = 0;
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
        $count++;
        unless ($dry_run) {
            open my $out, '>', $file or next;
            print $out $content;
            close $out;
        }
    }
}
if ($count > 0) {
    my $prefix = $dry_run ? "  \033[0;36m[dry-run] would modify" : "  \033[0;32mOK\033[0m Modified";
    print "${prefix} $count files (imbue- prefix)\033[0m\n";
}
PERL_SCRIPT

# Export dry-run flag for perl scripts
if [ "$DRY_RUN" = true ]; then
    export MIGRATE_DRY_RUN=1
fi

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
    count=$(find "$old_root" -type f | wc -l | tr -d ' ')
    if [ "$DRY_RUN" = true ]; then
        dry "would move $count files from $old_root -> $new_root"
    else
        find "$old_root" -type f | while IFS= read -r f; do
            rel="${f#"$old_root"/}"
            newf="$new_root/$rel"
            mkdir -p "$(dirname "$newf")"
            mv "$f" "$newf"
            git add "$newf" 2>/dev/null || true
            git rm --cached "$f" 2>/dev/null || true
        done
    fi
    moved=$((moved + count))
done
# Clean up empty old directories
if [ "$DRY_RUN" = false ]; then
    for d in libs/mng libs/mng_*; do
        [ -d "$d" ] && find "$d" -depth -type d -empty -delete 2>/dev/null
        [ -d "$d" ] && rmdir "$d" 2>/dev/null || true
    done
fi
if [ "$moved" -gt 0 ] && [ "$DRY_RUN" = false ]; then
    ok "Moved $moved files from old paths"
elif [ "$moved" -eq 0 ]; then
    ok "No orphaned files at old paths"
fi

# Remove leftover libs/mng* directories.
# After artifact cleanup, orphaned file moves, and git mv renames,
# these should be empty (or contain only gitignored artifacts).
for d in libs/mng libs/mng_*; do
    [ -d "$d" ] || continue
    if [ "$DRY_RUN" = true ]; then
        dry "would remove $d (after previous steps clean it out)"
    elif find "$d" -type f | read -r; then
        # In real mode, if still non-empty after all cleanup, warn
        echo -e "  ${YELLOW}WARNING: $d is not empty after cleanup -- keeping it${NC}"
    else
        rm -rf "$d"
        ok "Removed $d"
    fi
done

# ── 2. Rename .mng/ directory ─────────────────────────────────────

step "2/9" "Renaming .mng/ directory..."

if [ -d ".mng" ] && [ ! -d ".mngr" ]; then
    # Fix symlinks inside .mng/ BEFORE the directory rename
    for file in .mng/*; do
        [ -L "$file" ] || continue
        target=$(readlink "$file")
        if [[ "$target" == *mng* && "$target" != *mngr* ]]; then
            newtarget="${target//mng/mngr}"
            if [ "$DRY_RUN" = true ]; then
                dry "would fix symlink $file -> $newtarget"
            else
                rm "$file"
                ln -s "$newtarget" "$file"
                git add "$file"
                ok "fixed symlink $file -> $newtarget"
            fi
        fi
    done
    if [ "$DRY_RUN" = true ]; then
        dry "would rename .mng -> .mngr"
    else
        git mv ".mng" ".mngr"
        ok ".mng -> .mngr"
    fi
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
        if [ "$DRY_RUN" = true ]; then
            dry "would rename $dir -> $newdir"
        else
            git mv "$dir" "$newdir"
            ok "$dir -> $newdir"
        fi
    else
        skip "$newdir already exists"
    fi
done

# ── 4. Rename Python package directories inside libs ──────────────

step "4/9" "Renaming Python package directories..."

if [ -d "libs/mngr/imbue/mng" ] && [ ! -d "libs/mngr/imbue/mngr" ]; then
    if [ "$DRY_RUN" = true ]; then
        dry "would rename libs/mngr/imbue/mng -> libs/mngr/imbue/mngr"
    else
        git mv "libs/mngr/imbue/mng" "libs/mngr/imbue/mngr"
        ok "libs/mngr/imbue/mng -> libs/mngr/imbue/mngr"
    fi
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
        if [ "$DRY_RUN" = true ]; then
            dry "would rename $dir -> $newdir"
        else
            git mv "$dir" "$newdir"
            ok "$dir -> $newdir"
        fi
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
        if [ "$DRY_RUN" = true ]; then
            dry "would fix symlink $file -> $newfile (target: $newtarget)"
        else
            rm "$file"
            git rm --cached "$file" 2>/dev/null || true
            ln -s "$newtarget" "$newfile"
            git add "$newfile"
            ok "$file -> $newfile (target: $newtarget)"
        fi
        fixed_links=$((fixed_links + 1))
    elif [[ "$(basename "$file")" == *mng* && "$(basename "$file")" != *mngr* ]]; then
        newbase=$(basename "$file")
        newbase="${newbase//mng/mngr}"
        newfile="$(dirname "$file")/$newbase"
        if [ "$file" != "$newfile" ]; then
            if [ "$DRY_RUN" = true ]; then
                dry "would rename $file -> $newfile"
            else
                git mv "$file" "$newfile"
                ok "$file -> $newfile"
            fi
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
        if [ "$DRY_RUN" = true ]; then
            dry "would rename $file -> $newfile"
        else
            git mv "$file" "$newfile"
            ok "$file -> $newfile"
        fi
    fi
done

# ── 7. Replace file contents: mng -> mngr ────────────────────────

step "7/9" "Replacing mng -> mngr in file contents..."

# Collect eligible files, then pass them all to perl in one call
# so it can count and summarize.
mapfile -t content_files < <(
    git ls-files -z | while IFS= read -r -d '' file; do
        case "$file" in
            scripts/migrate_*|test_meta_ratchets.py) continue ;;
        esac
        [ -L "$file" ] && continue
        mime=$(file --brief --mime-encoding "$file" 2>/dev/null || echo "unknown")
        case "$mime" in
            binary|unknown) continue ;;
        esac
        echo "$file"
    done
)
perl "$RENAME_PL" "${content_files[@]+"${content_files[@]}"}"

# ── 8. Add imbue- prefix to all PyPI package names ────────────────

step "8/9" "Adding imbue- prefix to PyPI package names..."

# Collect all files that may need imbue- prefix fixes
mapfile -t pypi_files < <(
    for f in libs/*/pyproject.toml apps/*/pyproject.toml; do [ -f "$f" ] && echo "$f"; done
    for f in scripts/release.py scripts/verify_publish.py scripts/utils.py scripts/install.sh; do [ -f "$f" ] && echo "$f"; done
    grep -rl 'distribution("mngr' libs/ apps/ 2>/dev/null || true
    for f in libs/mngr_recursive/imbue/mngr_recursive/provisioning.py libs/mngr/imbue/mngr/uv_tool.py README.md libs/mngr/README.md; do [ -f "$f" ] && echo "$f"; done
)
# Deduplicate
mapfile -t pypi_files < <(printf '%s\n' "${pypi_files[@]}" | sort -u)
perl "$PYPI_PL" "${pypi_files[@]+"${pypi_files[@]}"}"

# ── 9. Regenerate uv.lock ────────────────────────────────────────

step "9/9" "Regenerating uv.lock..."
if [ "$DRY_RUN" = true ]; then
    dry "would regenerate uv.lock"
elif command -v uv &>/dev/null; then
    uv lock
    ok "uv.lock regenerated"
else
    skip "uv not found, skipping lock regeneration"
fi

echo -e "\n${GREEN}${BOLD}Code migration complete.${NC}"
if [ "$DRY_RUN" = true ]; then
    echo -e "${YELLOW}This was a dry run. No changes were made.${NC}"
else
    echo -e "Next steps:"
    echo -e "  1. Review changes: git diff --stat"
    echo -e "  2. Commit: git add -A && git commit -m 'Rename mng -> mngr'"
fi
