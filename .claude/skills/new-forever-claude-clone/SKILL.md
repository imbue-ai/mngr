---
name: new-forever-claude-clone
argument-hint: <repo-name> [parent-dir]
description: Create a new PRIVATE GitHub repo that is a full-history copy of imbue-ai/forever-claude-template's current main branch, clone it to <parent-dir>/<repo-name> (default $HOME/project), and push. Use when the user asks to "spin up a new forever-claude clone", "fork the forever-claude template as a private repo", "make me a new private copy of forever-claude-template", or similar.
---

# Create a new private copy of forever-claude-template

This skill is a thin wrapper around the `create-new-mind-repo` recipe in `~/project/mngr/justfile`. The recipe owns all the real logic (preflight checks, cloning, repo creation, push, PAT URL printing). Your job is to parse the user's args, invoke the recipe, and relay its output.

## Input parsing

The Skill tool passes the entire args string as `$1`. You need:

- `REPO` -- the new repo name. Required.
- `PARENT_DIR` -- the directory the clone will live under. Optional; recipe defaults to `$HOME/project`.

Common input shapes:
- Bare repo name: `story-recommender` -> `REPO=story-recommender`, omit `PARENT_DIR`.
- Repo name + parent: `story-recommender /some/dir` -> `REPO=story-recommender`, `PARENT_DIR=/some/dir`.
- `<owner>/<repo>` form (legacy): split on `/` and take the right side as `REPO`. The recipe always creates under whichever account `gh` is authenticated as, so the owner half is informational only -- if it doesn't match `gh api user --jq .login`, stop and ask the user to confirm rather than silently ignoring it.

If anything is ambiguous, ask before running.

## Run the recipe

From `~/project/mngr` (or any worktree of it):

```bash
just create-new-mind-repo "$REPO"            # default parent
just create-new-mind-repo "$REPO" "$PARENT_DIR"  # custom parent
```

The recipe handles every preflight check (gh auth, FCT presence, target dir free, target repo not yet on GitHub), the clone, the remote rewire, `gh repo create --private --push`, and prints a pre-filled fine-grained PAT URL at the end.

## Report

Relay the recipe's output to the user. Specifically:

- Web URL, clone URL, local path, and commit count (the recipe prints these).
- The fine-grained PAT URL (the recipe prints this too).
- The one-line reminder that GitHub does not accept repository selection via URL params, so the user still has to click "Only select repositories" and add the new repo manually after opening the PAT URL.

## Things not to do

- Do not reimplement the steps inline. If the recipe is missing or broken, fix the recipe (`~/project/mngr/justfile`) rather than working around it from the skill.
- Do not push to or modify `imbue-ai/forever-claude-template` -- the recipe only reads from it.
- Do not create the repo under an owner the user did not name. The recipe always uses the gh-authenticated user; if the user wanted an org repo, stop and ask -- this skill currently only handles personal repos.
- Do not try to mint a PAT via any API. Just relay the URL the recipe prints.
