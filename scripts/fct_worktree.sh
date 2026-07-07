#!/usr/bin/env bash
# Create an independent forever-claude-template ("fct") checkout nested in the
# current mngr checkout at .external_worktrees/forever-claude-template. Errors if
# one is already there (remove it to recreate) or if the branch already exists on fct.
#
#   just fct-worktree                 # fct on the SAME branch as this mngr checkout
#   just fct-worktree wz/other        # override branch
#   just fct-worktree wz/other main   # override base ref (default origin/main)
#
# The checkout is a full, independent clone (its own .git), so it survives deletion
# of any other clone or cache -- unlike a `git worktree` hung off a base repo, which
# breaks the moment that base is removed. Destination is derived from the current
# checkout's root, so it lands correctly whether run from an operator clone or an
# agent's ~/.mngr/worktrees/<agent>/ checkout, with no path baked in.
#
# FCT_DIR (from a gitignored apps/minds/.env or the shell) is an OPTIONAL speed hint:
# a local fct clone whose objects are borrowed via `git clone --reference-if-able`.
# `--dissociate` copies them in, so the result keeps no dependency on FCT_DIR.
set -ueo pipefail

FCT_REMOTE="https://github.com/imbue-ai/forever-claude-template.git"

repo_root="$(git rev-parse --show-toplevel)"
dest="$repo_root/.external_worktrees/forever-claude-template"
branch="${1:-$(git -C "$repo_root" rev-parse --abbrev-ref HEAD)}"
base="${2:-origin/main}"

# Reject rather than silently reuse a stale checkout from an earlier task.
if [ -e "$dest" ]; then
    echo "error: $dest already exists" >&2
    if [ -e "$dest/.git" ]; then
        echo "       (fct checkout on branch $(git -C "$dest" branch --show-current))" >&2
    fi
    echo "       remove it to recreate:  rm -rf $dest" >&2
    exit 1
fi

# Optional speed hint: borrow objects from a local fct clone if one is configured.
if [ -z "${FCT_DIR:-}" ] && [ -f "$repo_root/apps/minds/.env" ]; then
    set -a; . "$repo_root/apps/minds/.env"; set +a
fi
ref_args=()
if [ -n "${FCT_DIR:-}" ] && git -C "$FCT_DIR" rev-parse --git-dir >/dev/null 2>&1; then
    ref_args=(--reference-if-able "$FCT_DIR" --dissociate)
fi

# GH_TOKEN (agents have it) authenticates the private clone; operators without it
# fall back to git's configured credential helper. The token is never persisted.
url="$FCT_REMOTE"
if [ -n "${GH_TOKEN:-}" ]; then
    url="https://x-access-token:${GH_TOKEN}@github.com/imbue-ai/forever-claude-template.git"
fi

# Reject a name that already exists on fct rather than silently tracking that old
# branch -- a reused name (e.g. a year later) would otherwise inherit stale history
# instead of forking off $base. Checked before the clone so no work is wasted.
if git ls-remote --exit-code --heads "$url" "$branch" >/dev/null 2>&1; then
    echo "error: branch '$branch' already exists on fct" >&2
    echo "       delete it:  git push $FCT_REMOTE --delete $branch" >&2
    echo "       or pick a new name:  just fct-worktree <name>" >&2
    exit 1
fi

mkdir -p "$repo_root/.external_worktrees"
git clone ${ref_args[@]+"${ref_args[@]}"} "$url" "$dest"
git -C "$dest" remote set-url origin "$FCT_REMOTE"
git -C "$dest" checkout -q -b "$branch" "$base"
echo "fct checkout ready: $dest on $branch"
