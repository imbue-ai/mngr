#!/usr/bin/env bash
# Print the current branch name, robust to jj (jujutsu) colocated repos.
#
# `git rev-parse --abbrev-ref HEAD` returns the literal "HEAD" when the
# checkout is in detached-HEAD state -- the normal resting state of a jj
# colocated repo, which tracks the "current branch" as a bookmark and leaves
# git's HEAD detached. In that case fall back to jj's nearest bookmark to the
# working copy (@). Tested against jj 0.28 and documented through 0.37.
#
# Usage:
#   branch="$(scripts/current_branch.sh)"                # checkout in cwd
#   branch="$(scripts/current_branch.sh /path/to/repo)"  # a specific checkout
#
# On success prints the branch and exits 0. When the branch can't be
# determined (detached HEAD with no jj bookmark, or not a git repo) prints
# nothing and exits 1 -- the caller chooses its own fallback / error policy.
set -ueo pipefail

repo_dir="${1:-.}"

branch="$(git -C "$repo_dir" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
if [ -z "$branch" ] || [ "$branch" = "HEAD" ]; then
    branch=""
    if command -v jj >/dev/null 2>&1 && jj -R "$repo_dir" root >/dev/null 2>&1; then
        # `--ignore-working-copy` keeps this read-only and fast (no snapshot);
        # bookmark resolution doesn't depend on uncommitted file changes.
        branch="$(jj -R "$repo_dir" --ignore-working-copy log --no-graph --color=never \
            -r 'heads(::@ & bookmarks())' \
            -T 'local_bookmarks.map(|b| b.name()).join("\n") ++ "\n"' 2>/dev/null || true)"
        # Take the first name if @ has several bookmarked heads (e.g. a merge).
        branch="${branch%%$'\n'*}"
    fi
fi

if [ -z "$branch" ] || [ "$branch" = "HEAD" ]; then
    exit 1
fi
printf '%s\n' "$branch"
