---
name: release-minds
argument-hint: <release-branch>
description: Cut a new "production" release of the minds app. Pushes a release branch in the mngr clone at ~/project/minds_prod, syncs vendor/mngr in ~/project/forever-claude-template to match, pushes the same-named branch there, and merges the release branch into FCT main. Use when the user asks to "release a new version of minds", "cut a minds release", "update the vendored mngr in forever-claude-template to track <branch>", or anything of that shape.
---

# Release a new version of the minds app

The user keeps a "production" clone of mngr at `~/project/minds_prod` and a consumer repo at `~/project/forever-claude-template` whose `vendor/mngr/` directory is a checked-in copy of mngr. A "release" means: pick a branch name in `minds_prod`, publish it, make the forever-claude-template's vendored copy point at exactly that commit on a matching branch, and merge that release branch into forever-claude-template's `main` so downstream clones (made with `new-forever-claude-clone` or any plain `git clone` of FCT) see the released vendor/mngr. Without that final merge, `main` falls behind and fresh clones get a stale vendor that fails to build inside Docker with confusing `uv` errors about conflicting URLs for `imbue-mngr`.

## Input — read this first

The Skill tool's template-substitution of `$1` is unreliable. Do NOT rely on literal `$1` appearing in this file and being replaced at invocation time. Instead:

1. Read the `args` string the user / caller passed to this Skill invocation. It should be the release branch name, e.g. `minds_v0.1.0`, `minds_v0.1.1`, etc.
2. If the invocation supplied no args, ask the user which branch name before doing anything.
3. Throughout the instructions below, every appearance of the literal string `<RELEASE>` means "substitute the release branch name from step 1." Do the substitution yourself when you run the commands — do not leave `<RELEASE>` in the shell commands you actually execute.

Both re-release (branch already exists) and first-release (branch does not exist yet) cases are supported — see step 2.

## Preconditions to check (fail loudly if any are wrong)

1. `~/project/minds_prod` exists and is a git checkout whose `origin` points at `git@github.com:imbue-ai/mngr.git`.
2. `~/project/forever-claude-template` exists and has `origin` pointing at `git@github.com:imbue-ai/forever-claude-template.git`.
3. Both checkouts have clean working trees (`git status --porcelain` empty). Do not start a release on top of uncommitted work -- surface the dirty state to the user and stop.
4. The current branch in `~/project/minds_prod` is `<RELEASE>`. If not, ask the user before switching -- they may have intended a different checkout. Never force-switch.
5. `~/project/forever-claude-template` is currently on `main` *or* on `<RELEASE>` — either is fine, the skill handles both. Run `git fetch origin` first, then `git status -sb` on whatever branch is checked out; there must be no local commits unpushed to `origin`. For `main` specifically, it must also not be behind `origin/main` (`git fetch origin main` should leave nothing to pull). If either tip has unpushed local commits or main has diverged from origin, stop and ask the user.
6. The user has network access to push (the skill assumes SSH keys are configured; if a push fails with auth, surface the real error rather than retrying).

## Steps

### 1. Push the mngr release branch

In `~/project/minds_prod`:

```bash
git push -u origin <RELEASE>
```

Git will reject a non-fast-forward push if `origin/<RELEASE>` exists at a SHA that isn't an ancestor of local HEAD. That rejection is the correct behavior — stop and ask the user before overwriting. A fast-forward (local is ahead of origin) succeeds silently; that's the expected shape of a re-release.

Record `HEAD` SHA: `git rev-parse HEAD`. Use the full SHA in the commit message below; use the short SHA in conversational references.

### 2. Create or reuse the release branch in forever-claude-template

Switch into `~/project/forever-claude-template` and reconcile the local `<RELEASE>` branch with origin. Handle both the first-release and re-release cases explicitly:

```bash
cd ~/project/forever-claude-template
git fetch origin "<RELEASE>" 2>/dev/null || true  # may fail if origin/<RELEASE> doesn't exist; that's fine
```

Then exactly one of the following applies — check in this order:

- **A. Local `<RELEASE>` does NOT exist AND `origin/<RELEASE>` does NOT exist** — first release of this branch:
  ```bash
  git checkout main
  git checkout -b <RELEASE>
  ```

- **B. Local `<RELEASE>` does NOT exist AND `origin/<RELEASE>` DOES exist** — fresh clone case, upstream is authoritative:
  ```bash
  git checkout -b <RELEASE> origin/<RELEASE>
  ```

- **C. Local `<RELEASE>` DOES exist** — re-release case:
  ```bash
  git checkout <RELEASE>
  # Fast-forward to origin if origin exists and is ahead; safe no-op otherwise.
  git pull --ff-only origin <RELEASE> 2>&1 || true
  ```
  If `git pull --ff-only` fails with "Not possible to fast-forward" (i.e. local has commits not on origin, or they've diverged), **stop and ask the user**. Local commits on the release branch that aren't on origin are unresolved work that belongs to some other flow, not this skill.

Never `-B` or delete an existing release branch.

### 3. Replace `vendor/mngr/` contents with the mngr HEAD

Use `git archive` from `minds_prod` -- this gives exactly the tracked files at HEAD with no `.git`, no `.venv`, no caches:

```bash
cd ~/project/minds_prod && git archive --format=tar HEAD > /tmp/mngr_sync.tar
cd ~/project/forever-claude-template/vendor/mngr
rm -rf ./* ./.[!.]*        # clear existing contents, including dotfiles, but keep the directory
tar -xf /tmp/mngr_sync.tar
rm /tmp/mngr_sync.tar
```

Do NOT use `rsync ... --delete` from a live mngr working tree for the release flow -- that would sweep in untracked files (`.venv`, editor swapfiles, etc.). `git archive` is the right tool because it's exactly-the-tracked-tree at the committed SHA.

### 4. Commit the sync

In `~/project/forever-claude-template` (on the `<RELEASE>` branch):

```bash
git add -A vendor/mngr/
```

If `git status --short -- vendor/mngr` is empty at this point, no commits are needed — `vendor/mngr` already matches mngr HEAD. Skip to step 6 (merge to main) in that case; the release is effectively a no-op for FCT's release branch.

Otherwise commit:

```bash
git commit -m "Sync vendor/mngr to <RELEASE> (<short-sha>)"
```

Include the full SHA in the body so the commit is self-describing. Example body: "Tracks the `<RELEASE>` release branch of mngr at commit `<full-sha>`." If this is a re-release, list the new mngr commits being picked up in the body — it makes the FCT log readable.

**Pre-commit gotcha**: the pre-commit hook in forever-claude-template is generated and references an absolute path under `~/.cache/uv/archive-v0/...` that can go stale. If `git commit` fails with `'pre-commit' not found. Did you forget to activate your virtualenv?`, run:

```bash
uv tool install pre-commit
(cd ~/project/forever-claude-template && uv tool run pre-commit install)
```

and retry the commit. Do not use `--no-verify` to work around this.

### 5. Push the forever-claude-template release branch

```bash
cd ~/project/forever-claude-template && git push -u origin <RELEASE>
```

Same rejection semantics as step 1: non-fast-forward is rejected and that's the correct guardrail. Stop and ask if the push is rejected.

### 6. Merge the release branch into forever-claude-template `main` and push

The release is not complete until `main` points at it. Downstream consumers (private clones made with the `new-forever-claude-clone` skill, any fresh `git clone` of FCT) use `main` as their starting point, so leaving `main` behind a release means those clones get a stale `vendor/mngr` and Docker builds may fail with `uv` resolution errors about conflicting URLs for `imbue-mngr`.

```bash
cd ~/project/forever-claude-template
git checkout main
git pull --ff-only origin main
git merge --no-ff <RELEASE> -m "Merge <RELEASE> release branch into main"
git push origin main
```

Use `--no-ff` so there's an explicit merge commit marking each release; that makes it easy to skim `git log main` and see where each release landed. If step 4 was a no-op because `vendor/mngr` was already up to date, this merge may also be a no-op ("Already up to date") — that's fine, proceed to step 7.

If the pull turns up unrelated work on `origin/main` that wasn't on this release branch, stop and ask the user before merging -- the release branch should be ahead of `main`, not diverged from it.

### 7. Report

Report back with, at minimum:
- The mngr SHA that was released.
- The forever-claude-template commit SHA that captured the sync (or note "no new sync commit; vendor/mngr already matched" if step 4 was a no-op).
- Both branch names (they should be identical -- `<RELEASE>`).
- Whether FCT main moved as a result (merge commit SHA) or was already up to date.

## Things not to do

- Do not amend existing commits in either repo. Always a new commit (per the user's repo-wide rule).
- Do not open PRs automatically. The user treats these release branches as long-lived pointers, not as PR sources, so leave them as plain branches unless asked.
- Do not run `uv sync`, `just test-offload`, or any verification in this skill -- the release is a sync-only operation. If the user wants tests, they will ask.
- Do not touch `~/.external_worktrees/forever-claude-template` or any worktree under minds_prod. This skill operates on the two primary checkouts only.
- Do not modify `pyproject.toml`, `uv.lock`, or anything outside `vendor/mngr/` in forever-claude-template. The sync is purely a content replacement of that directory.
- Do not `-B` or force-delete an existing release branch. Re-releases use `git checkout` + `git pull --ff-only`, not a recreation.

## If something goes wrong mid-flight

The release has several mutating actions: a push to the mngr remote, a commit and push of the release branch in forever-claude-template, and then a merge + push to forever-claude-template `main`. If you've already completed early steps but a later one fails, do not silently retry -- surface the partial state to the user. Recovery rules of thumb:
- mngr release branch pushed but FCT sync failed: mngr is authoritative; re-run from step 2. The re-release path in step 2 handles the existing local `<RELEASE>` branch correctly.
- FCT release branch pushed but the merge-to-main failed: the release branch on origin is authoritative. Re-running from step 6 (checkout main, merge the release branch, push) is safe and idempotent.
- The merge-to-main produced an unexpected conflict: stop. A clean release should always be a straightforward merge of FCT's release branch into FCT's `main` when main hasn't diverged. A conflict signals unrelated work on main; ask the user how to proceed rather than resolving it autonomously.
- `git push` rejected as non-fast-forward on either remote: stop. Someone else (or a past invocation) moved the branch to a SHA that isn't an ancestor of local HEAD. Ask the user how to reconcile rather than force-pushing.
