#!/usr/bin/env bash
# Migrate developer-local state for the forever-claude-template ->
# default-workspace-template rename.
#
# Run from your mngr checkout AFTER the GitHub repo rename and the code rename
# have landed. Idempotent -- safe to rerun; anything it is unsure about is
# reported but not touched.
#
# Usage:
#   scripts/migrate_state_fct_to_default_workspace_template.sh --dry-run
#   scripts/migrate_state_fct_to_default_workspace_template.sh
#   scripts/migrate_state_fct_to_default_workspace_template.sh [checkout ...]
#
# Positional args are additional template checkout paths. The template
# checkout is otherwise discovered from DEFAULT_WORKSPACE_TEMPLATE_DIR /
# FCT_DIR (shell env or gitignored apps/minds/.env).
#
# What this does:
#   1. Removes this checkout's clean .external_worktrees/forever-claude-template
#      (a fresh one materializes under the new name on next use)
#   2. Renames each template checkout dir forever-claude-template -> default-workspace-template
#   3. Points each template checkout's git remotes at the new URL
#   4. Renames FCT_DIR -> DEFAULT_WORKSPACE_TEMPLATE_DIR in apps/minds/.env
#   5. Sweeps stale __pycache__ in this checkout and reports leftover FCT_* env vars

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

OLD_NAME="forever-claude-template"
NEW_NAME="default-workspace-template"

DRY_RUN=false
EXTRA_DIRS=()
for arg in "$@"; do
    if [ "$arg" = "--dry-run" ]; then
        DRY_RUN=true
    else
        EXTRA_DIRS+=("$arg")
    fi
done

ok()   { echo "  OK $*"; }
skip() { echo "  skip: $*"; }
warn() { echo "  !! $*"; }
dry()  { echo "  [dry-run] $*"; }
step() { echo; echo "[$1/5] $2"; }

if [ "$DRY_RUN" = true ]; then
    echo "$OLD_NAME -> $NEW_NAME state migration (DRY RUN)"
else
    echo "$OLD_NAME -> $NEW_NAME state migration"
fi

# ── 1. Stale external worktree in this mngr checkout ───────────────
step 1 "Removing stale .external_worktrees/$OLD_NAME..."

OLD_WORKTREE="$REPO_ROOT/.external_worktrees/$OLD_NAME"
if [ ! -e "$OLD_WORKTREE" ]; then
    skip "no $OLD_WORKTREE"
elif [ -n "$(git -C "$OLD_WORKTREE" status --porcelain 2>/dev/null)" ]; then
    warn "$OLD_WORKTREE has uncommitted changes -- commit/stash them, then rerun."
else
    # The worktree's .git file names its parent repo; detach it there so the
    # parent's worktree bookkeeping stays consistent.
    parent_gitdir="$(sed -n 's/^gitdir: //p' "$OLD_WORKTREE/.git" 2>/dev/null || true)"
    if [ "$DRY_RUN" = true ]; then
        dry "would remove clean worktree $OLD_WORKTREE (a new-name one materializes on next use)"
    else
        rm -rf "$OLD_WORKTREE"
        if [ -n "$parent_gitdir" ] && [ -d "$parent_gitdir" ]; then
            git -C "$(dirname "$(dirname "$(dirname "$parent_gitdir")")")" worktree prune 2>/dev/null || true
        fi
        ok "removed $OLD_WORKTREE"
    fi
fi

# ── 2. Discover and rename template checkouts ──────────────────────
step 2 "Renaming template checkout dirs..."

# Discovery: explicit args + DEFAULT_WORKSPACE_TEMPLATE_DIR / FCT_DIR from the
# shell env or the gitignored apps/minds/.env.
CANDIDATE_DIRS=("${EXTRA_DIRS[@]+"${EXTRA_DIRS[@]}"}")
if [ -f apps/minds/.env ]; then
    env_dir="$(sed -n 's/^\(DEFAULT_WORKSPACE_TEMPLATE_DIR\|FCT_DIR\)=//p' apps/minds/.env | tail -1)"
    [ -n "$env_dir" ] && CANDIDATE_DIRS+=("$env_dir")
fi
[ -n "${DEFAULT_WORKSPACE_TEMPLATE_DIR:-}" ] && CANDIDATE_DIRS+=("$DEFAULT_WORKSPACE_TEMPLATE_DIR")
[ -n "${FCT_DIR:-}" ] && CANDIDATE_DIRS+=("$FCT_DIR")

RENAMED_DIRS=()
seen=""
for dir in "${CANDIDATE_DIRS[@]+"${CANDIDATE_DIRS[@]}"}"; do
    dir="${dir%/}"
    case " $seen " in *" $dir "*) continue ;; esac
    seen="$seen $dir"
    if [ ! -d "$dir" ]; then
        skip "$dir does not exist"
        continue
    fi
    base="$(basename "$dir")"
    if [ "$base" = "$NEW_NAME" ]; then
        RENAMED_DIRS+=("$dir")
        skip "$dir already renamed"
        continue
    fi
    if [ "$base" != "$OLD_NAME" ]; then
        RENAMED_DIRS+=("$dir")
        skip "$dir keeps its custom name (only remotes/.env are updated)"
        continue
    fi
    target="$(dirname "$dir")/$NEW_NAME"
    if [ -e "$target" ]; then
        warn "$target already exists -- resolve manually, keeping $dir"
        continue
    fi
    if [ -n "$(git -C "$dir" worktree list --porcelain 2>/dev/null | grep -c ^worktree || true)" ] && [ "$(git -C "$dir" worktree list 2>/dev/null | wc -l)" -gt 1 ]; then
        warn "$dir has linked worktrees; their .git pointers break on rename. Remove them first (git -C $dir worktree list), then rerun."
        continue
    fi
    if [ "$DRY_RUN" = true ]; then
        dry "would mv $dir -> $target"
        RENAMED_DIRS+=("$dir")
    else
        mv "$dir" "$target"
        ok "mv $dir -> $target"
        RENAMED_DIRS+=("$target")
    fi
done
[ -z "${RENAMED_DIRS[0]:-}" ] && skip "no template checkouts found (pass paths as arguments if you have one elsewhere)"

# ── 3. Point remotes at the new URL ────────────────────────────────
step 3 "Updating git remotes..."

for dir in "${RENAMED_DIRS[@]+"${RENAMED_DIRS[@]}"}"; do
    [ -d "$dir/.git" ] || [ -f "$dir/.git" ] || continue
    while read -r remote url _; do
        case "$url" in
            *"$OLD_NAME"*)
                new_url="${url//$OLD_NAME/$NEW_NAME}"
                if [ "$DRY_RUN" = true ]; then
                    dry "would set $dir remote $remote -> $new_url"
                else
                    git -C "$dir" remote set-url "$remote" "$new_url"
                    ok "$dir remote $remote -> $new_url"
                fi
                ;;
        esac
    done < <(git -C "$dir" remote -v | awk '$3 == "(fetch)" {print $1, $2}')
done

# ── 4. apps/minds/.env variable rename ─────────────────────────────
step 4 "Renaming FCT_DIR in apps/minds/.env..."

ENV_FILE="apps/minds/.env"
if [ ! -f "$ENV_FILE" ]; then
    skip "no $ENV_FILE"
elif ! grep -qE "^FCT_DIR=|$OLD_NAME" "$ENV_FILE"; then
    skip "$ENV_FILE already clean"
elif [ "$DRY_RUN" = true ]; then
    dry "would rewrite FCT_DIR -> DEFAULT_WORKSPACE_TEMPLATE_DIR and $OLD_NAME -> $NEW_NAME in $ENV_FILE"
else
    sed -i.bak -e "s/^FCT_DIR=/DEFAULT_WORKSPACE_TEMPLATE_DIR=/" -e "s|$OLD_NAME|$NEW_NAME|g" "$ENV_FILE"
    rm -f "$ENV_FILE.bak"
    ok "rewrote $ENV_FILE"
fi

# ── 5. Stale caches and leftover env vars ──────────────────────────
step 5 "Sweeping stale __pycache__ and checking env..."

pycache_count="$(find "$REPO_ROOT" -type d -name __pycache__ -not -path "*/.git/*" 2>/dev/null | wc -l | tr -d ' ')"
if [ "$pycache_count" -gt 0 ]; then
    if [ "$DRY_RUN" = true ]; then
        dry "would remove $pycache_count __pycache__ dirs"
    else
        find "$REPO_ROOT" -type d -name __pycache__ -not -path "*/.git/*" -exec rm -rf {} + 2>/dev/null || true
        ok "removed $pycache_count __pycache__ dirs"
    fi
else
    skip "no __pycache__ dirs"
fi

leftover_vars="$(env | grep -E "^FCT_" | cut -d= -f1 || true)"
if [ -n "$leftover_vars" ]; then
    warn "shell still exports: $(echo "$leftover_vars" | tr '\n' ' ')-- rename these in your shell rc (FCT_DIR -> DEFAULT_WORKSPACE_TEMPLATE_DIR)."
fi

echo
if [ "$DRY_RUN" = true ]; then
    echo "Dry run complete. Rerun without --dry-run to apply."
else
    echo "State migration complete. Rerun any time; it will re-report anything it skipped."
fi
