---
name: new-forever-claude-clone
argument-hint: <new-repo-name> [owner]
description: Create a new PRIVATE GitHub repo that is a full-history copy of imbue-ai/forever-claude-template's current main branch, clone it to ~/project/<new-repo-name>, and push. Use when the user asks to "spin up a new forever-claude clone", "fork the forever-claude template as a private repo", "make me a new private copy of forever-claude-template", or similar.
---

# Create a new private copy of forever-claude-template

This skill stands up a brand-new private GitHub repository that contains the full git history of `imbue-ai/forever-claude-template`'s current `main` branch. It clones the result to `~/project/<new-repo-name>` so the user can start using it immediately.

## Inputs

- `$1` (required): name for the new repo (e.g. `minds-prod-v2`). Used as both the GitHub repo name and the local directory name under `~/project/`.
- `$2` (optional): owner (GitHub username or org) for the new repo. If omitted, ask the user -- common choices are the user's personal account (`joshalbrecht`) or an org (`imbue-ai`). Do not guess.

If `$1` is missing, ask the user for it before doing anything.

## Preconditions to check (fail loudly if any are wrong)

1. `gh` CLI is installed and authenticated with scopes that allow creating repos in the target owner's namespace. Check with `gh auth status`. For a personal repo the default scopes are enough; for an org repo the token needs `admin:org` or the org must allow member repo creation.
2. `~/project/forever-claude-template` exists, has `origin` pointing at `git@github.com:imbue-ai/forever-claude-template.git`, is on `main`, has a clean working tree, and is up to date with `origin/main` (`git fetch origin main` then check). We are copying the history that is actually on `origin/main`, not whatever stale state a local checkout happens to have.
3. `~/project/<new-repo-name>` does NOT already exist. Do not overwrite an existing directory.
4. The target GitHub repo `<owner>/<new-repo-name>` does NOT already exist (`gh repo view <owner>/<new-repo-name>` returns non-zero). If it already exists, stop and ask -- we never push into a pre-existing repo the user didn't intend.

## Steps

### 1. Clone forever-claude-template into the new path

Use a fresh clone (not a cp of the existing checkout) so git state is clean and we pick up `origin/main` exactly:

```bash
cd ~/project
git clone git@github.com:imbue-ai/forever-claude-template.git "$1"
cd "$1"
git checkout main
```

This gives us the full commit history reachable from `main`.

### 2. Rewire the remote

Drop the template's `origin` so we don't accidentally push template-copy commits back upstream:

```bash
git remote remove origin
```

### 3. Create the private repo and push in one shot

```bash
gh repo create "<owner>/$1" --private --source=. --remote=origin --push
```

`--source=.` tells `gh` to use the current directory as the source repo; `--push` pushes the current branch (`main`) to the new remote after creation. This preserves the full history of `main`.

If the user wanted more than just `main` copied (e.g. all branches/tags), `--push` only pushes the current branch. In that case, replace the push with `git push --all origin && git push --tags origin` after `gh repo create` (omit `--push` from the `gh` invocation). Default behavior for this skill is `main` only; escalate to all-refs only if the user explicitly asks.

### 4. Verify

Run:

```bash
git remote -v            # origin should be the new private repo
git log --oneline -3     # should show the same recent commits as forever-claude-template main
gh repo view --web=false "<owner>/$1" | head
```

Confirm the repo is private (the `gh repo view` header shows visibility).

### 5. Print the GH_TOKEN creation URL

The user will want a `GH_TOKEN` scoped to the new private repo for CI / automation. We cannot create tokens programmatically, so print clickable URLs with the scopes they should pick:

- **Fine-grained PAT (recommended, least-privilege)**: https://github.com/settings/personal-access-tokens/new
  - Resource owner: `<owner>`
  - Repository access: "Only select repositories" -> `<owner>/$1`
  - Permissions: Repository permissions -> Contents: Read and write, Metadata: Read-only. Add Pull requests / Workflows / Actions if the user plans to automate PRs or CI.
- **Classic PAT (broader, simpler)**: https://github.com/settings/tokens/new
  - Scopes: `repo` (full). Optionally `workflow` for CI changes.

Tell the user which one to pick based on context, but default-recommend the fine-grained variant.

### 6. Report

Report back:
- Full clone URL of the new repo (`git@github.com:<owner>/$1.git`).
- Web URL (`https://github.com/<owner>/$1`).
- Local path (`~/project/$1`).
- Number of commits copied (from `git rev-list --count HEAD`) -- a quick sanity check that history came across.
- The token-creation URLs from step 5.

## Things not to do

- Do not use `gh repo fork` -- GitHub "forks" keep a parent pointer and can't easily be made fully private/independent. We want an independent repo.
- Do not simply `cp -r` the existing checkout. It would copy the existing `.git` state (which is fine) plus untracked files, editor swapfiles, `.venv`, `node_modules`, etc. (which is not). A fresh `git clone` is the right tool.
- Do not push force or touch `imbue-ai/forever-claude-template` in any way. This skill only reads from it.
- Do not create the repo under an owner the user did not explicitly name. Owner mistakes are annoying to undo.
- Do not rename `main` to something else, and do not squash/rewrite history. The request is a full-history copy.
- Do not attempt to create or manipulate a PAT via any API. Just print the URL and let the user do it.
