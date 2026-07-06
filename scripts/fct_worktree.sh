#!/usr/bin/env bash
# Stand up (or reuse) an independent forever-claude-template ("fct") checkout
# nested in the current mngr checkout at .external_worktrees/forever-claude-template.
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

if [ -e "$dest/.git" ]; then
    echo "reusing $dest (branch: $(git -C "$dest" branch --show-current))"
    exit 0
fi

# Optional speed hint: borrow objects from a local fct clone if one is configured.
if [ -z "${FCT_DIR:-}" ] && [ -f "$repo_root/apps/minds/.env" ]; then
    set -a; . "$repo_root/apps/minds/.env"; set +a
fi
ref_args=()
if [ -n "${FCT_DIR:-}" ] && [ -d "$FCT_DIR/.git" ]; then
    ref_args=(--reference-if-able "$FCT_DIR/.git" --dissociate)
fi

# GH_TOKEN (agents have it) authenticates the private clone; operators without it
# fall back to git's configured credential helper. The token is never persisted.
url="$FCT_REMOTE"
if [ -n "${GH_TOKEN:-}" ]; then
    url="https://x-access-token:${GH_TOKEN}@github.com/imbue-ai/forever-claude-template.git"
fi

mkdir -p "$repo_root/.external_worktrees"
git clone ${ref_args[@]+"${ref_args[@]}"} "$url" "$dest"
git -C "$dest" remote set-url origin "$FCT_REMOTE"

if git -C "$dest" show-ref --verify --quiet "refs/remotes/origin/$branch"; then
    git -C "$dest" checkout -q -B "$branch" "origin/$branch"
else
    git -C "$dest" checkout -q -B "$branch" "$base"
fi
echo "fct checkout ready: $dest on $branch"
