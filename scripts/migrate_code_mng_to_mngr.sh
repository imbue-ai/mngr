#!/usr/bin/env bash
# Migrate code from mng -> mngr naming within the git checkout.
#
# Run from the repo root (or it will cd there automatically).
# This script is idempotent -- safe to run multiple times.
#
# For open MRs:
#   1. Run this script on your branch
#   2. Commit the rename
#   3. Merge main -- since both sides now use mngr names, git can
#      three-way merge properly and only real conflicts remain
#   4. Resolve any real conflicts manually (as you normally would)
#   5. Commit the merge
#
# Usage:
#   scripts/migrate_code_mng_to_mngr.sh              # run migration
#   scripts/migrate_code_mng_to_mngr.sh --dry-run    # preview without changes
#
# What this does:
#   0. Cleans build artifacts (__pycache__, htmlcov, etc.)
#   1. Renames .mng/ -> .mngr/ in the repo root
#   2. Renames lib directories (libs/mng -> libs/mngr, libs/mng_* -> libs/mngr_*)
#   3. Renames Python package directories within libs
#   4. Moves orphaned files from old paths (post-merge stragglers)
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
    for pat in coverage.xml .coverage; do
        count=$(find "$REPO_ROOT" -name "$pat" 2>/dev/null | wc -l | tr -d ' ')
        [ "$count" -gt 0 ] && dry "would remove $count $pat files"
    done
else
    find "$REPO_ROOT" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    find "$REPO_ROOT" -type d -name htmlcov -exec rm -rf {} + 2>/dev/null || true
    find "$REPO_ROOT" -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
    find "$REPO_ROOT" -type d -name .test_output -exec rm -rf {} + 2>/dev/null || true
    find "$REPO_ROOT" -name coverage.xml -delete 2>/dev/null || true
    find "$REPO_ROOT" -name '.coverage' -delete 2>/dev/null || true
    find "$REPO_ROOT" -path '*/.reviewer/outputs' -exec rm -rf {} + 2>/dev/null || true
    find "$REPO_ROOT" -name '.stop_hook_consecutive_blocks' -delete 2>/dev/null || true
    ok "Cleaned build artifacts"
fi

# ── Helper: clean and remove old libs/mng* directories ────────────
cleanup_old_mng_dirs() {
    for d in libs/mng libs/mng_*; do
        [ -d "$d" ] || continue
        find "$d" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
        find "$d" -type d -name htmlcov -exec rm -rf {} + 2>/dev/null || true
        find "$d" -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
        find "$d" -type d -name .test_output -exec rm -rf {} + 2>/dev/null || true
        find "$d" -name coverage.xml -delete 2>/dev/null || true
        find "$d" -name '.coverage' -delete 2>/dev/null || true
        find "$d" -path '*/.reviewer/outputs' -exec rm -rf {} + 2>/dev/null || true
        find "$d" -name '.stop_hook_consecutive_blocks' -delete 2>/dev/null || true
        find "$d" -depth -type d -empty -delete 2>/dev/null || true
        if [ -d "$d" ]; then
            if find "$d" -type f | read -r; then
                echo -e "  ${YELLOW}WARNING: $d still has non-artifact files -- keeping it${NC}"
            else
                rm -rf "$d"
                ok "Removed $d"
            fi
        else
            ok "Removed $d"
        fi
    done
}

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

# ── Helper: perl script for imbue- prefix (TOML/config files only) ──
# These patterns match broadly on quoted "mngr" strings, so they're
# only safe for pyproject.toml, CI workflows, and config files.

PYPI_TOML_PL=$(mktemp)
trap 'rm -f "$RENAME_PL" "$PYPI_TOML_PL" "$PYPI_CODE_PL"' EXIT
cat > "$PYPI_TOML_PL" << 'PERL_SCRIPT'
use strict;
use warnings;
my $dry_run = $ENV{MIGRATE_DRY_RUN} // 0;
my $count = 0;
for my $file (@ARGV) {
    open my $fh, '<', $file or next;
    my $content = do { local $/; <$fh> };
    close $fh;
    my $orig = $content;
    # Version specifiers: "mngr==", "mngr>=", etc
    $content =~ s/(?<!imbue-)"mngr(?=[=><~!])/"imbue-mngr/g;
    # Plugin dep names: "mngr-claude==", "mngr-modal", etc
    $content =~ s/(?<!imbue-)"mngr-(?=\w+)/"imbue-mngr-/g;
    # Standalone "mngr" in TOML arrays/values
    $content =~ s/(?<!imbue-)"mngr"/"imbue-mngr"/g;
    # uv sources keys at start of line
    $content =~ s/^mngr(?=-| = \{)/imbue-mngr/mg;
    # CI step names: Build mngr -> Build imbue-mngr
    $content =~ s/Build (?<!imbue-)mngr/Build imbue-mngr/g;
    # Fix false positives
    $content =~ s/dir_name="imbue-mngr"/dir_name="mngr"/g;
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
    print "${prefix} $count config files (imbue- prefix)\033[0m\n";
}
PERL_SCRIPT

# ── Helper: perl script for imbue- prefix (all source files) ─────
# These patterns are specific enough to be safe on any file.

PYPI_CODE_PL=$(mktemp)
cat > "$PYPI_CODE_PL" << 'PERL_SCRIPT'
use strict;
use warnings;
my $dry_run = $ENV{MIGRATE_DRY_RUN} // 0;
my $count = 0;
for my $file (@ARGV) {
    open my $fh, '<', $file or next;
    my $content = do { local $/; <$fh> };
    close $fh;
    my $orig = $content;
    # importlib.metadata.distribution("mngr...") lookups
    $content =~ s/distribution\("mngr/distribution("imbue-mngr/g;
    # startswith("mngr-") in package metadata checks
    $content =~ s/startswith\("mngr-"\)/startswith("imbue-mngr-")/g;
    # PyPI URL slugs
    $content =~ s|pypi/mngr/|pypi/imbue-mngr/|g;
    # uv tool commands
    $content =~ s/uv tool install (?<!imbue-)mngr/uv tool install imbue-mngr/g;
    $content =~ s/uv tool uninstall (?<!imbue-)mngr/uv tool uninstall imbue-mngr/g;
    $content =~ s/uv tool upgrade (?<!imbue-)mngr/uv tool upgrade imbue-mngr/g;
    $content =~ s/uv tool run --from (?<!imbue-)mngr /uv tool run --from imbue-mngr /g;
    # uvx mngr -> uvx --from imbue-mngr mngr
    $content =~ s/uvx (?<!imbue-)mngr\b/uvx --from imbue-mngr mngr/g;
    # Fix false positives: dir_name must stay "mngr"
    $content =~ s/dir_name="imbue-mngr"/dir_name="mngr"/g;
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
    print "${prefix} $count source files (imbue- prefix)\033[0m\n";
}
PERL_SCRIPT

# Export dry-run flag for perl scripts
if [ "$DRY_RUN" = true ]; then
    export MIGRATE_DRY_RUN=1
fi

# ── 1. Rename .mng/ directory ─────────────────────────────────────

step "1/9" "Renaming .mng/ directory..."

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
elif [ -d ".mngr" ] && [ -d ".mng" ]; then
    # Both exist (e.g. after merging main reintroduces .mng/).
    # Copy any untracked files (local config like settings.local.toml)
    # from .mng/ to .mngr/ before removing .mng/.
    if [ "$DRY_RUN" = true ]; then
        dry "would copy untracked files from .mng/ to .mngr/ and remove .mng/"
    else
        cp -a -n .mng/. .mngr/ 2>/dev/null || true
        git rm -rf .mng 2>/dev/null || true
        rm -rf .mng
        ok "Merged .mng/ into .mngr/ and removed .mng/"
    fi
elif [ -d ".mngr" ]; then
    ok "Already renamed"
fi

# ── 2. Rename lib directories (top level) ─────────────────────────

step "2/9" "Renaming lib directories..."

renamed_libs=0
for dir in libs/mng libs/mng_*; do
    [ -d "$dir" ] || continue
    base=$(basename "$dir")
    case "$base" in
        mng|mng_*) ;;
        *) continue ;;
    esac
    newbase="${base/mng/mngr}"
    newdir="libs/$newbase"
    # Remove newdir if it exists but has no git-tracked files (just artifacts)
    if [ -d "$newdir" ] && ! git ls-files --error-unmatch "$newdir" >/dev/null 2>&1; then
        rm -rf "$newdir"
    fi
    if [ ! -d "$newdir" ]; then
        if [ "$DRY_RUN" = true ]; then
            dry "would rename $dir -> $newdir"
        else
            git mv "$dir" "$newdir"
            ok "$dir -> $newdir"
        fi
        renamed_libs=$((renamed_libs + 1))
    elif [ -d "$dir" ] && [ -d "$newdir" ]; then
        # Both exist (after merging main reintroduces old paths).
        # Copy untracked files to new dir first, then remove old.
        if [ "$DRY_RUN" = true ]; then
            dry "would merge $dir into $newdir and remove $dir"
        else
            cp -a -n "$dir"/. "$newdir"/ 2>/dev/null || true
            git rm -rf "$dir" 2>/dev/null || true
            rm -rf "$dir"
            ok "Merged $dir into $newdir and removed"
        fi
        renamed_libs=$((renamed_libs + 1))
    fi
done
if [ "$renamed_libs" -eq 0 ]; then
    ok "All lib directories already renamed"
fi

# ── 3. Rename Python package directories inside libs ──────────────

step "3/9" "Renaming Python package directories..."

renamed_pkgs=0
if [ -d "libs/mngr/imbue/mng" ] && [ ! -d "libs/mngr/imbue/mngr" ]; then
    if [ "$DRY_RUN" = true ]; then
        dry "would rename libs/mngr/imbue/mng -> libs/mngr/imbue/mngr"
    else
        git mv "libs/mngr/imbue/mng" "libs/mngr/imbue/mngr"
        ok "libs/mngr/imbue/mng -> libs/mngr/imbue/mngr"
    fi
    renamed_pkgs=$((renamed_pkgs + 1))
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
        renamed_pkgs=$((renamed_pkgs + 1))
    fi
done
if [ "$renamed_pkgs" -eq 0 ]; then
    ok "All package directories already renamed"
fi

# ── 4. Move orphaned files from old paths ─────────────────────────
# After merging main, git may leave new files at old paths like
# libs/mng/imbue/mng/... with "file location" conflict suggestions.
# This runs AFTER directory renames so git mv handles the bulk.

step "4/9" "Moving orphaned files from old paths..."

moved=0
for old_root in libs/mng/imbue/mng libs/mng_*/imbue/mng_*; do
    [ -d "$old_root" ] || continue
    new_root=$(echo "$old_root" | sed 's/mng_/mngr_/g; s|/mng/|/mngr/|g; s|^libs/mng/|libs/mngr/|')
    [ -d "$new_root" ] || continue
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
            git rm --cached --quiet "$f" 2>/dev/null || true
        done
    fi
    moved=$((moved + count))
done
if [ "$moved" -gt 0 ] && [ "$DRY_RUN" = false ]; then
    ok "Moved $moved files from old paths"
elif [ "$moved" -eq 0 ]; then
    ok "No orphaned files"
fi

# Clean up old dirs (first pass -- second pass runs after uv lock)
cleanup_old_mng_dirs

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

# Also remove any broken symlinks with mng in the name (untracked,
# left behind after merge brings in renamed versions)
find "$REPO_ROOT" -maxdepth 2 -type l -name '*mng*' ! -name '*mngr*' | while IFS= read -r link; do
    if [ ! -e "$link" ]; then
        if [ "$DRY_RUN" = true ]; then
            dry "would remove broken symlink $link"
        else
            rm "$link"
            ok "Removed broken symlink $link"
        fi
        fixed_links=$((fixed_links + 1))
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

# TOML/config patterns (broad "mngr" matching, only safe for config files)
mapfile -t toml_files < <(
    for f in libs/*/pyproject.toml apps/*/pyproject.toml; do [ -f "$f" ] && echo "$f"; done
    for f in .github/workflows/*.yml; do [ -f "$f" ] && echo "$f"; done
)
mapfile -t toml_files < <(printf '%s\n' "${toml_files[@]}" | sort -u)
perl "$PYPI_TOML_PL" "${toml_files[@]+"${toml_files[@]}"}"

# Code patterns (specific enough for all source files)
mapfile -t code_files < <(
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
perl "$PYPI_CODE_PL" "${code_files[@]+"${code_files[@]}"}"

# Targeted fixes for specific files where "mngr" means the PyPI name
# (not vendor name, profile name, etc.)
#
# Split into two scripts:
#   TARGETED_PL      -- base "mngr" patterns (safe for all targeted files)
#   TARGETED_DASH_PL -- "mngr-xxx" plugin/dep name patterns (only for files
#                       where mngr-xxx strings are PyPI names, NOT package
#                       metadata names like ToolRequirement(name="mngr-opencode"))
TARGETED_PL=$(mktemp)
TARGETED_DASH_PL=$(mktemp)
trap 'rm -f "$RENAME_PL" "$PYPI_TOML_PL" "$PYPI_CODE_PL" "$TARGETED_PL" "$TARGETED_DASH_PL"' EXIT

# -- Base patterns: "mngr" (the core package) as a PyPI name --
cat > "$TARGETED_PL" << 'PERL_SCRIPT'
use strict;
use warnings;
my $dry_run = $ENV{MIGRATE_DRY_RUN} // 0;
my $count = 0;
for my $file (@ARGV) {
    open my $fh, '<', $file or next;
    my $content = do { local $/; <$fh> };
    close $fh;
    my $orig = $content;
    # Comparisons: name == "mngr", name != "mngr"
    $content =~ s/name == "mngr"/name == "imbue-mngr"/g;
    $content =~ s/name != "mngr"/name != "imbue-mngr"/g;
    # Keyword arg: name="mngr"
    $content =~ s/name="mngr"/name="imbue-mngr"/g;
    # TOML-style: name = "mngr" (with spaces, used in test TOML strings)
    $content =~ s/name = "mngr"/name = "imbue-mngr"/g;
    # Dict key access: ["mngr"]
    $content =~ s/\["mngr"\]/["imbue-mngr"]/g;
    # Dict/set membership: "mngr" not in / "mngr" in
    $content =~ s/"mngr" not in/"imbue-mngr" not in/g;
    $content =~ s/"mngr" in /"imbue-mngr" in /g;
    # Assertions: assert "mngr" in (for package name checks)
    $content =~ s/assert "mngr" in/assert "imbue-mngr" in/g;
    # Tuple element: ("mngr", -- for package tuples like ("mngr", "0.1.4")
    $content =~ s/\("mngr",/("imbue-mngr",/g;
    # Version specifiers in docstrings: "mngr>=0.1.0" etc
    $content =~ s/(?<!imbue-)"mngr(?=[><=!])/"imbue-mngr/g;
    # Standalone quoted "mngr" (docstrings, error messages, TOML values)
    $content =~ s/(?<!imbue-)"mngr"/"imbue-mngr"/g;
    # Fix false positive: dir_name must stay "mngr"
    $content =~ s/dir_name="imbue-mngr"/dir_name="mngr"/g;
    # Fix false positive: path segments (/ "mngr") must stay "mngr"
    $content =~ s|/ "imbue-mngr"|/ "mngr"|g;
    if ($content ne $orig) {
        $count++;
        unless ($dry_run) {
            open my $out, '>', $file or next;
            print $out $content;
            close $out;
        }
    }
}
PERL_SCRIPT

# -- Dash patterns: "mngr-xxx" as PyPI names --
# Only for files where "mngr-xxx" strings are actual PyPI package names,
# NOT for uv_tool.py/uv_tool_test.py where they are package metadata names.
cat > "$TARGETED_DASH_PL" << 'PERL_SCRIPT'
use strict;
use warnings;
my $dry_run = $ENV{MIGRATE_DRY_RUN} // 0;
my $count = 0;
for my $file (@ARGV) {
    open my $fh, '<', $file or next;
    my $content = do { local $/; <$fh> };
    close $fh;
    my $orig = $content;
    # pypi_name= with mngr or mngr-xxx (utils.py PackageInfo declarations)
    $content =~ s/pypi_name="mngr-(\w+)"/pypi_name="imbue-mngr-$1"/g;
    $content =~ s/pypi_name="mngr"/pypi_name="imbue-mngr"/g;
    # internal_deps: "mngr-xxx", "other" and "mngr", "other"
    $content =~ s/(?<!imbue-)"mngr-(\w+)",\s*"/"imbue-mngr-$1", "/g;
    # Tuple element: ("mngr-xxx", for package tuples like ("mngr-pair", "0.1.0")
    $content =~ s/\("mngr-(\w+)",/("imbue-mngr-$1",/g;
    # Quoted plugin names as function args: "mngr-schedule" etc
    $content =~ s/(?<!imbue-)"mngr-(\w+)"/"imbue-mngr-$1"/g;
    # Unquoted in error messages: mngr-schedule package
    $content =~ s/(?<!imbue-)mngr-schedule package/imbue-mngr-schedule package/g;
    # Also handle --with imbue-mngr-xxx in assertion strings (already correct
    # from PYPI_CODE_PL, but the version-pinned form may be missed)
    $content =~ s/--with mngr-(\w+)==/--with imbue-mngr-$1==/g;
    # Fix false positive: dir_name must stay "mngr"
    $content =~ s/dir_name="imbue-mngr"/dir_name="mngr"/g;
    # NOTE: resolve_mngr_install_mode takes a PyPI distribution name (used by
    # importlib.metadata.distribution()), so it SHOULD have the imbue- prefix.
    # A previous version of this script incorrectly treated this as a false positive.
    # Fix false positive: path segments (/ "mngr-xxx") must stay "mngr-xxx"
    $content =~ s|/ "imbue-mngr-|/ "mngr-|g;
    if ($content ne $orig) {
        $count++;
        unless ($dry_run) {
            open my $out, '>', $file or next;
            print $out $content;
            close $out;
        }
    }
}
PERL_SCRIPT

# Apply base patterns to all targeted files
for f in \
    libs/mngr_recursive/imbue/mngr_recursive/provisioning.py \
    libs/mngr_recursive/imbue/mngr_recursive/provisioning_test.py \
    libs/mngr/imbue/mngr/uv_tool.py \
    libs/mngr/imbue/mngr/uv_tool_test.py \
    libs/mngr_schedule/imbue/mngr_schedule/implementations/modal/deploy.py \
    scripts/utils.py \
    scripts/release.py \
    scripts/verify_publish.py; do
    [ -f "$f" ] && perl "$TARGETED_PL" "$f"
done

# Apply dash patterns only to files where "mngr-xxx" means a PyPI name
for f in \
    libs/mngr_recursive/imbue/mngr_recursive/provisioning.py \
    libs/mngr_recursive/imbue/mngr_recursive/provisioning_test.py \
    libs/mngr_schedule/imbue/mngr_schedule/implementations/modal/deploy.py \
    scripts/utils.py; do
    [ -f "$f" ] && perl "$TARGETED_DASH_PL" "$f"
done

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

# Final cleanup: re-run after uv lock which can recreate __pycache__
cleanup_old_mng_dirs

echo -e "\n${GREEN}${BOLD}Code migration complete.${NC}"
if [ "$DRY_RUN" = true ]; then
    echo -e "${YELLOW}This was a dry run. No changes were made.${NC}"
else
    echo -e "Next steps:"
    echo -e "  1. Review changes: git diff --stat"
    echo -e "  2. Commit: git add -A && git commit -m 'Rename mng -> mngr'"
fi
